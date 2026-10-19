"""Hermes plugin for the tuner: composed tools and the shadow-session guard.

- Registers each `$HERMES_HOME/tuner/tools/tuned_*/` (tool.json + program.py)
  as a tool in toolset `tuner`. The handler runs the program in the Docker
  sandbox through `relay.run_program`; its tool calls go back through
  `ctx.dispatch_tool`, limited to the tool's declared `calls` that are also on
  policy.CALLABLE_TOOLS or owner-approved (spec 5.2).
- `pre_tool_call` -> `shadow_guard`: in a shadow (dry-run trial) session,
  blocks tools that would change anything and records them (spec 4.5).
  `ctx.dispatch_tool` skips that hook, so the composed-tool handler forces a
  dry run itself in shadow sessions.
- A tool whose tool.json has `"shadow_only": true` (written when its shadow run
  is scheduled, before its quality check) runs only in shadow sessions; anywhere
  else the handler refuses it. Go-live rewrites tool.json without the flag.
- `on_session_start` -> re-scan the tools dir (spec 4.7).
- `register` writes `$HERMES_HOME/tuner/plugin_heartbeat.json` =
  {"pid", "registered_at"}; the cycle schedules shadows and applies composed
  tools only when it is newer than the last gateway restart.

Never raises into Hermes's plugin loader. Never writes outside
`$HERMES_HOME/tuner/shadow/` and `$HERMES_HOME/tuner/plugin_heartbeat.json`.
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

from hermes_tuner import lineage, policy, proposals  # noqa: E402

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
_TOOL_NAME = re.compile(re.escape(policy.TOOL_PREFIX) + r"[A-Za-z0-9_]{1,58}")


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
    if (not isinstance(desc, str) or not isinstance(params, dict)
            or not isinstance(calls, list) or not all(isinstance(c, str) for c in calls)
            or not isinstance(mutating, bool) or not isinstance(shadow_only, bool)):
        logger.warning("hermes-tuner: skipping %s: tool.json fields invalid", name)
        return None
    # A tool that may call a mutating tool is mutating whatever it declares.
    mutating = mutating or any(c in policy.MUTATING_TOOLS for c in calls)
    return {"name": name, "description": desc, "parameters": params,
            "calls": frozenset(calls), "mutating": mutating, "program": program,
            "shadow_only": shadow_only}


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

    def on_session_start(**_):
        scan(ctx)

    ctx.register_hook("on_session_start", on_session_start)
