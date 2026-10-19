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
- `on_session_start` -> re-scan the tools dir (spec 4.7).

Never raises into Hermes's plugin loader. Never writes outside
`$HERMES_HOME/tuner/shadow/`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

_TUNER_DIR = str(Path(__file__).resolve().parents[2])
if _TUNER_DIR not in sys.path:
    sys.path.insert(0, _TUNER_DIR)

from hermes_tuner import policy, proposals  # noqa: E402

from . import relay  # noqa: E402

logger = logging.getLogger(__name__)

BLOCK_MESSAGE = "dry run: recorded"
_CRON_SESSION = re.compile(r"cron_([0-9a-f]{12})_\d{8}_\d{6}")
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


def is_shadow_session(session_id: object) -> bool:
    """True when the session is a run of a shadow job. Never raises."""
    try:
        job_id = _shadow_job_id(session_id)
        if job_id is None:
            return False
        ledger = _read_json(tuner_root() / "shadow_jobs.json")
        if isinstance(ledger, dict) and job_id in ledger:
            return True
        jobs = _read_json(jobs_path())
        if isinstance(jobs, dict):
            jobs = jobs.get("jobs")
        if not isinstance(jobs, list):
            return False
        for job in jobs:
            if (isinstance(job, dict) and job.get("id") == job_id
                    and isinstance(job.get("name"), str)
                    and job["name"].startswith(policy.SHADOW_PREFIX)):
                return True
        return False
    except Exception:  # noqa: BLE001
        logger.exception("hermes-tuner: shadow-session check failed")
        return False


def _record_shadow(session_id: str, entries: list[dict]) -> None:
    if not entries or _shadow_job_id(session_id) is None:
        return
    try:
        d = tuner_root() / "shadow"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(d / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.exception("hermes-tuner: could not record shadow intents")


def shadow_guard(tool_name, args, session_id="", **_):
    """pre_tool_call hook: block and record state-changing tools in shadow sessions."""
    if not isinstance(tool_name, str) or not policy.shadow_blocked(tool_name):
        return None
    if not is_shadow_session(session_id):
        return None
    _record_shadow(session_id, [{"tool": tool_name, "args": args, "via": "guard"}])
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
    if (not isinstance(desc, str) or not isinstance(params, dict)
            or not isinstance(calls, list) or not all(isinstance(c, str) for c in calls)
            or not isinstance(mutating, bool)):
        logger.warning("hermes-tuner: skipping %s: tool.json fields invalid", name)
        return None
    # A tool that may call a mutating tool is mutating whatever it declares.
    mutating = mutating or any(c in policy.MUTATING_TOOLS for c in calls)
    return {"name": name, "description": desc, "parameters": params,
            "calls": frozenset(calls), "mutating": mutating, "program": program}


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
            shadow = is_shadow_session(session_id)
            dry_run = shadow or (spec["mutating"] and wants_dry)
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
            if shadow:
                _record_shadow(session_id, [
                    {"tool": i["tool"], "args": i["args"], "via": spec["name"]}
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


def register(ctx) -> None:
    scan(ctx)
    ctx.register_hook("pre_tool_call", shadow_guard)

    def on_session_start(**_):
        scan(ctx)

    ctx.register_hook("on_session_start", on_session_start)
