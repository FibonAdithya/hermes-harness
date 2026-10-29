"""Hermes plugin for the tuner: composed tools, result reducers and the
shadow-session guard.

- Registers each `$HERMES_HOME/tuner/tools/tuned_*/` (tool.json + program.py)
  as a tool in toolset `tuner`. The handler runs the program in the Docker
  sandbox through `relay.run_program`; its tool calls go back through
  `ctx.dispatch_tool`, limited to the tool's declared `calls` that are also on
  policy.CALLABLE_TOOLS or owner-approved (spec 5.2).
- `pre_tool_call` -> `shadow_guard`: in a shadow (dry-run trial) session,
  blocks tools that would change anything and records them (spec 4.5).
  `ctx.dispatch_tool` skips that hook, so the composed-tool handler forces a
  dry run itself in shadow sessions.
- A tool whose tool.json has `"shadow_only": true` and `"shadow_fix": "<fix id>"`
  (written when its shadow run is scheduled, before its quality check) runs only
  in that fix's shadow session; anywhere else, other fixes' shadows included, the
  handler refuses it. Without `shadow_fix` it is refused everywhere. Go-live
  rewrites tool.json without the flags. Hermes still lists the tool to every
  session that has the `tuner` toolset (or no toolset list): a refusal costs that
  session one wasted call.
- `on_session_start` -> re-scan the tools dir (spec 4.7).
- `transform_tool_result` -> `reduce_result`: applies a result reducer
  (`$HERMES_HOME/tuner/reducers/<fix id>.json`, re-listed on every call) to the
  raw result of its tool. A live file applies only in runs of its spec's job
  (scoped by the session's compression-lineage root); a shadow file only in its
  fix's shadow session. Two matching files: neither applies. Each application
  appends one line of lengths (never result text) to
  `$HERMES_HOME/tuner/reducer_events/<fix id>.jsonl`.
- `register` writes `$HERMES_HOME/tuner/plugin_heartbeat.json` =
  {"pid", "registered_at"}; the cycle schedules shadows and applies composed
  tools only when it is newer than the last gateway restart.

Never raises into Hermes's plugin loader. Never writes outside
`$HERMES_HOME/tuner/shadow/`, `$HERMES_HOME/tuner/reducer_events/` and
`$HERMES_HOME/tuner/plugin_heartbeat.json`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_TUNER_DIR = str(Path(__file__).resolve().parents[2])
if _TUNER_DIR not in sys.path:
    sys.path.insert(0, _TUNER_DIR)

from hermes_tuner import lineage, policy, proposals, reducers  # noqa: E402

from . import relay  # noqa: E402

logger = logging.getLogger(__name__)

BLOCK_MESSAGE = "dry run: recorded"
_CRON_SESSION = re.compile(r"cron_([0-9a-f]{12})_\d{8}_\d{6}")
# Session ids Hermes gives a conversation after compression.
_CHILD_SESSION = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}")
SHADOW_DUE_BEFORE_S = 2 * 3600
SHADOW_DUE_AFTER_S = 10 * 60
_ROOTS: dict[tuple[str, str], str] = {}
_ROOTS_MAX = 4096
_TOOL_NAME = policy.TOOL_NAME_RE


# --- paths (read at call time so tests and profiles can move HERMES_HOME) ---

def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def tuner_root() -> Path:
    return hermes_home() / "tuner"


def broker_state() -> Path:
    return hermes_home() / "broker" / "state"


def jobs_path() -> Path:
    return hermes_home() / "cron" / "jobs.json"


def state_db_path() -> Path:
    return hermes_home() / "state.db"


def runner_argv(container_name: str) -> list[str]:
    return relay.production_argv(container_name)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# --- shadow sessions --------------------------------------------------------

def _shadow_job_id(session_id: object) -> str | None:
    if not isinstance(session_id, str):
        return None
    m = _CRON_SESSION.fullmatch(session_id)
    return m.group(1) if m else None


def _root_session(session_id: str) -> str | None:
    """The cron session `session_id` descends from through compression.

    A `cron_…` id is its own root. Others are looked up in state.db; found
    roots are memoised (lineage never changes). Raises LineageError.
    """
    if _shadow_job_id(session_id) is not None:
        return session_id
    db = state_db_path()
    key = (str(db), session_id)
    if key in _ROOTS:
        return _ROOTS[key]
    root = lineage.root_session_id(db, session_id)
    if root is not None:
        if len(_ROOTS) >= _ROOTS_MAX:
            del _ROOTS[next(iter(_ROOTS))]
        _ROOTS[key] = root
    return root


def _epoch(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _shadow_due_now() -> bool:
    """True when shadow_jobs.json has a run in [now - 2 h, now + 10 min]."""
    ledger = _read_json(tuner_root() / "shadow_jobs.json")
    if not isinstance(ledger, dict):
        return False
    now = time.time()
    for entry in ledger.values():
        t = _epoch(entry.get("run_at")) if isinstance(entry, dict) else None
        if t is not None and now - SHADOW_DUE_BEFORE_S <= t <= now + SHADOW_DUE_AFTER_S:
            return True
    return False


def _shadow_fix_id(root: str) -> str | None:
    """The fix whose shadow job ran the cron session `root`: its shadow_jobs.json
    entry's fix_id, else the job's name after the shadow prefix. None if unknown."""
    job_id = _shadow_job_id(root)
    if job_id is None:
        return None
    ledger = _read_json(tuner_root() / "shadow_jobs.json")
    entry = ledger.get(job_id) if isinstance(ledger, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("fix_id"), str):
        return entry["fix_id"]
    jobs = _read_json(jobs_path())
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs")
    for job in jobs if isinstance(jobs, list) else []:
        if (isinstance(job, dict) and job.get("id") == job_id
                and isinstance(job.get("name"), str)
                and job["name"].startswith(policy.SHADOW_PREFIX)):
            return job["name"][len(policy.SHADOW_PREFIX):] or None
    return None


def _is_shadow_job(job_id: str) -> bool:
    ledger = _read_json(tuner_root() / "shadow_jobs.json")
    if isinstance(ledger, dict) and job_id in ledger:
        return True
    jobs = _read_json(jobs_path())
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs")
    if not isinstance(jobs, list):
        return False
    return any(isinstance(job, dict) and job.get("id") == job_id
               and isinstance(job.get("name"), str)
               and job["name"].startswith(policy.SHADOW_PREFIX)
               for job in jobs)


def shadow_root(session_id: object) -> str | None:
    """The id to record a shadow session under, or None if not shadow. Never raises.

    Usually the shadow job's `cron_…` session (the root of `session_id`'s
    compression lineage). When state.db cannot be read and a shadow run is due,
    fails closed and returns `session_id` itself.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        try:
            root = _root_session(session_id)
        except lineage.LineageError:
            logger.warning("hermes-tuner: lineage lookup failed for a session")
            return session_id if _shadow_due_now() else None
        job_id = _shadow_job_id(root)
        if job_id is None or not _is_shadow_job(job_id):
            return None
        return root
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: shadow-session check failed")
        return None


def is_shadow_session(session_id: object) -> bool:
    return shadow_root(session_id) is not None


def _record_shadow(record_id: str, entries: list[dict]) -> None:
    if not entries or not (_CRON_SESSION.fullmatch(record_id)
                           or _CHILD_SESSION.fullmatch(record_id)):
        return
    try:
        d = tuner_root() / "shadow"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(d / f"{record_id}.jsonl", "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.exception("hermes-tuner: could not record shadow intents")


def shadow_guard(tool_name, args, session_id="", **_):
    """pre_tool_call hook: in a shadow session, block and record every tool off
    policy.SHADOW_ALLOW."""
    if not isinstance(tool_name, str) or not policy.shadow_blocked(tool_name):
        return None
    root = shadow_root(session_id)
    if root is None:
        return None
    _record_shadow(root, [{"tool": tool_name, "args": args, "via": "guard",
                           "session_id": session_id}])
    return {"action": "block", "message": BLOCK_MESSAGE}


# --- composed tools ---------------------------------------------------------

def load_tool(tool_dir: Path) -> dict | None:
    """The validated spec of one tool dir, with its program, or None."""
    name = tool_dir.name
    if not _TOOL_NAME.fullmatch(name):
        logger.warning("hermes-tuner: skipping %s: name must match %s", name,
                       _TOOL_NAME.pattern)
        return None
    try:
        spec = json.loads((tool_dir / "tool.json").read_text())
        program = (tool_dir / "program.py").read_text()
        compile(program, f"{name}/program.py", "exec")
    except (OSError, ValueError, SyntaxError) as exc:
        logger.warning("hermes-tuner: skipping %s: %s", name, type(exc).__name__)
        return None
    if not isinstance(spec, dict) or spec.get("name") != name:
        logger.warning("hermes-tuner: skipping %s: tool.json name differs", name)
        return None
    desc, params = spec.get("description"), spec.get("parameters")
    calls, mutating = spec.get("calls"), spec.get("mutating")
    shadow_only = spec.get("shadow_only", False)
    shadow_fix = spec.get("shadow_fix")
    if (not isinstance(desc, str) or not isinstance(params, dict)
            or not isinstance(calls, list) or not all(isinstance(c, str) for c in calls)
            or not isinstance(mutating, bool) or not isinstance(shadow_only, bool)
            or not (shadow_fix is None or isinstance(shadow_fix, str))):
        logger.warning("hermes-tuner: skipping %s: tool.json fields invalid", name)
        return None
    # A tool that may call a mutating tool is mutating whatever it declares.
    mutating = mutating or any(c in policy.MUTATING_TOOLS for c in calls)
    return {"name": name, "description": desc, "parameters": params,
            "calls": frozenset(calls), "mutating": mutating, "program": program,
            "shadow_only": shadow_only, "shadow_fix": shadow_fix}


def _schema(spec: dict) -> dict:
    params = json.loads(json.dumps(spec["parameters"]))
    if spec["mutating"]:
        props = params.setdefault("properties", {})
        if isinstance(props, dict):
            props["dry_run"] = {
                "type": "boolean",
                "description": "Record the changes this tool would make instead of making them.",
            }
    return {"name": spec["name"], "description": spec["description"], "parameters": params}


def _make_handler(ctx, tool_dir: Path):
    def handler(args, task_id=None, session_id=None, user_task=None, **_):
        try:
            # Re-read at call time so a rollback or removal takes effect at once.
            spec = load_tool(tool_dir)
            if spec is None:
                return json.dumps({"error": f"{tool_dir.name} is no longer available"})
            args = dict(args) if isinstance(args, dict) else {}
            wants_dry = args.pop("dry_run", None) is True
            shadow = shadow_root(session_id)
            if spec["shadow_only"] and shadow is None:
                return json.dumps({"error": f"{tool_dir.name} is in a shadow trial and "
                                            "runs only in shadow sessions"})
            if spec["shadow_only"] and (spec["shadow_fix"] is None
                                        or _shadow_fix_id(shadow) != spec["shadow_fix"]):
                return json.dumps({"error": f"{tool_dir.name} is in another fix's shadow "
                                            "trial and runs only in that fix's shadow "
                                            "session"})
            dry_run = shadow is not None or (spec["mutating"] and wants_dry)
            callable_now = policy.CALLABLE_TOOLS | proposals.approved_callables(
                tuner_root(), broker_state())
            allowed = frozenset(c for c in spec["calls"] & callable_now
                                if not c.startswith(policy.TOOL_PREFIX))
            fwd = {}
            if task_id:
                fwd["task_id"] = task_id
            if session_id:
                fwd["session_id"] = session_id

            def call(name, call_args):
                return ctx.dispatch_tool(name, call_args, **fwd)

            container = f"tuned-{uuid.uuid4().hex}"
            intents: list[dict] = []
            out = relay.run_program(spec["program"], args, call, runner_argv(container),
                                    dry_run=dry_run, intents=intents, allowed=allowed,
                                    container_name=container)
            if shadow is not None:
                _record_shadow(shadow, [
                    {"tool": i["tool"], "args": i["args"], "via": spec["name"],
                     "session_id": session_id}
                    for i in intents])
            return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("hermes-tuner: %s failed", tool_dir.name)
            return json.dumps({"error": f"{tool_dir.name} failed: {type(exc).__name__}"})

    return handler


def scan(ctx) -> list[str]:
    """Register every valid tool dir. Returns the names registered. Never raises."""
    names: list[str] = []
    try:
        tools_dir = tuner_root() / "tools"
        if not tools_dir.is_dir():
            return names
        for tool_dir in sorted(tools_dir.iterdir()):
            if not tool_dir.is_dir():
                continue
            spec = load_tool(tool_dir)
            if spec is None:
                continue
            try:
                ctx.register_tool(name=spec["name"], toolset=policy.TOOLSET,
                                  schema=_schema(spec), handler=_make_handler(ctx, tool_dir),
                                  description=spec["description"])
                names.append(spec["name"])
            except Exception:  # noqa: BLE001
                logger.exception("hermes-tuner: could not register %s", spec["name"])
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: tool scan failed")
    return names


# --- result reducers ----------------------------------------------------------

_FIX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")  # ledger._ID
# (path, mtime_ns, inode, size) -> the parsed file, or None when it is invalid.
_REDUCER_CACHE: dict[tuple[str, int, int, int], dict | None] = {}
_REDUCER_CACHE_MAX = 1024


def _parse_reducer(path: Path) -> dict | None:
    """The reducer file at `path` if it is valid, else None. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fix_id, shadow_only, shadow_fix = (data.get("fix_id"), data.get("shadow_only"),
                                       data.get("shadow_fix"))
    if (not isinstance(fix_id, str) or not _FIX_ID.fullmatch(fix_id) or ".." in fix_id
            or fix_id != path.stem or not isinstance(shadow_only, bool)
            or not (shadow_fix is None or isinstance(shadow_fix, str))):
        return None
    try:
        if reducers.validate_spec(data.get("spec")):
            return None
    except Exception:  # noqa: BLE001
        return None
    return {"spec": data["spec"], "fix_id": fix_id, "shadow_only": shadow_only,
            "shadow_fix": shadow_fix}


def load_reducers(reducers_dir: Path) -> list[dict]:
    """The valid reducer files in `reducers_dir`, by name. Lists the directory on
    every call (new files apply without a restart); parsed files are cached by
    (name, mtime_ns). Invalid files are skipped."""
    try:
        paths = sorted(p for p in Path(reducers_dir).iterdir()
                       if p.suffix == ".json" and p.is_file())
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        key = (str(path), st.st_mtime_ns, st.st_ino, st.st_size)
        if key not in _REDUCER_CACHE:
            if len(_REDUCER_CACHE) >= _REDUCER_CACHE_MAX:
                _REDUCER_CACHE.clear()
            _REDUCER_CACHE[key] = _parse_reducer(path)
        entry = _REDUCER_CACHE[key]
        if entry is not None:
            out.append(entry)
    return out


def _reducer_event(events_dir: Path, fix_id: str, event: dict) -> None:
    """Append one JSON line to reducer_events/<fix id>.jsonl (0600). Never raises."""
    try:
        Path(events_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(Path(events_dir) / f"{fix_id}.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: could not record a reducer event")


def _scoped(entries: list[dict], session_id: str, root_of) -> list[dict]:
    """The entries that apply in `session_id`. Its lineage root (`root_of`) is a
    shadow job's session: only the file of that shadow's fix. Otherwise only live
    files of the root's job. No root (or LineageError): none.

    Same split as shadow_root: its fail-closed case (lineage unreadable, a shadow
    due) names no fix, so no file would match there either."""
    try:
        root = root_of(session_id)
    except lineage.LineageError:
        return []
    job_id = _shadow_job_id(root)
    if job_id is None:
        return []
    if _is_shadow_job(job_id):
        fix_id = _shadow_fix_id(root)
        return [e for e in entries
                if fix_id is not None and e["shadow_only"] and e["shadow_fix"] == fix_id]
    return [e for e in entries if not e["shadow_only"] and e["spec"]["job"] == job_id]


def reduce_scoped(tool_name, result, session_id, tool_call_id, status, *, root_of,
                  reducers_dir: Path, events_dir: Path, load, apply) -> str | None:
    """The reduced result, or None to leave it unchanged. Never raises.

    `root_of(session_id)` gives the compression-lineage root (may raise
    LineageError), `load(reducers_dir)` the valid reducer files, `apply(spec,
    result)` the reduced text."""
    if status == "error" or not isinstance(result, str) or not isinstance(tool_name, str):
        return None
    try:
        entries = [e for e in load(reducers_dir) if e["spec"]["tool"] == tool_name]
        if not entries or not isinstance(session_id, str) or not session_id:
            return None
        matches = _scoped(entries, session_id, root_of)
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: reducer lookup failed")
        return None
    base = {"session_id": session_id, "tool_call_id": tool_call_id}
    if len(matches) > 1:
        for e in matches:
            _reducer_event(events_dir, e["fix_id"], dict(base, error="ambiguous"))
        return None
    if not matches:
        return None
    entry = matches[0]
    try:
        out = apply(entry["spec"], result)
        if not isinstance(out, str):
            raise TypeError("reducer returned a non-string")
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-tuner: reducer %s failed: %s", entry["fix_id"],
                       type(exc).__name__)
        _reducer_event(events_dir, entry["fix_id"], dict(base, error=type(exc).__name__))
        return None
    _reducer_event(events_dir, entry["fix_id"],
                   dict(base, before=len(result), after=len(out)))
    return out


def reduce_result(tool_name=None, args=None, result=None, session_id="", tool_call_id="",
                  status=None, **_) -> str | None:
    """transform_tool_result hook. Sees the raw result (Hermes wraps it afterwards)."""
    try:
        root = tuner_root()
        return reduce_scoped(tool_name, result, session_id, tool_call_id, status,
                             root_of=_root_session, reducers_dir=root / "reducers",
                             events_dir=root / "reducer_events", load=load_reducers,
                             apply=reducers.apply_reducer)
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: reduce_result failed")
        return None


def write_heartbeat() -> None:
    """Atomically write {"pid", "registered_at"} to tuner/plugin_heartbeat.json.
    Never raises."""
    try:
        root = tuner_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".tmp-heartbeat-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "registered_at": time.time()}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, root / "plugin_heartbeat.json")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: could not write the heartbeat")


def register(ctx) -> None:
    write_heartbeat()
    scan(ctx)
    ctx.register_hook("pre_tool_call", shadow_guard)
    try:  # a Hermes without this hook must still load the rest of the plugin
        ctx.register_hook("transform_tool_result", reduce_result)
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: could not register transform_tool_result")

    def on_session_start(**_):
        scan(ctx)

    ctx.register_hook("on_session_start", on_session_start)
