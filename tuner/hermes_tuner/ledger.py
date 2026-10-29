"""Versioned fix ledger: apply a fix, remember how to undo it, undo it exactly
unless the owner has since changed the value (spec 4.5).

Layout under `root` (the tuner directory):
    ledger.json                      all FixRecords
    versions/<id>/fix.json           the full Fix
    versions/<id>/before.json        the live values before apply
    versions/<id>/after.json         the values as written
    tools/<name>/{tool.json,program.py}   composed tools (read by the plugin)
    reducers/<id>.json               result reducers (read by the plugin's
                                     transform_tool_result hook)
Every write is atomic (temp file in the same directory + os.replace), files 0600,
directories 0700.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from . import policy
from .bridge import Bridge, BridgeError, encode_config_value, hermes_coerce
from .model import Fix
from .reducers import validate_spec

State = Literal["rejected", "shadow_pending", "shadow_failed", "trial", "kept",
                "rolled_back", "owner_changed", "proposed", "approved", "applied",
                "dropped", "bridge_error", "rollback_pending"]
# rollback_pending: an apply failed after a write may have reached Hermes, and the
# revert has not succeeded yet. Active, so nothing else touches the target meanwhile.
ACTIVE_STATES = frozenset({"shadow_pending", "trial", "applied", "rollback_pending"})
APPLIED_STATES = frozenset({"trial", "applied", "kept"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")
_TOOL_NAME = policy.TOOL_NAME_RE
# The fix payload key holding the job as drafted against (cycle_draft.BASE_KEY, which
# imports this module). Never part of what a fix writes.
BASE_KEY = "_base"


@dataclass
class FixRecord:
    id: str
    kind: str
    target: str
    candidate_key: str
    state: State
    created_at: float
    applied_at: float | None = None
    trial_runs: int = 0
    baseline_cost: float | None = None
    reason: str = ""
    spend_usd: float = 0.0
    needs_restart: bool = False
    decided_at: float | None = None  # when a trial was kept or rolled back
    # When the fix took effect: applied_at, or for a fix that needs a gateway restart
    # the restart time (spec 4.7). None until then.
    live_from: float | None = None
    # Token breakdowns [input, cache read, cache write, output] of the runs the trial
    # is compared against, stored at go-live (trial.baseline_runs). Verdicts and
    # savings price them under the CURRENT list, so a price change never compares two
    # lists. None on records saved before it existed: judged "continue" until the
    # trial deadline, no saving. baseline_cost is the display value at go-live.
    baseline_runs: list[list[int]] | None = None
    # A reducer fix: the replay search's estimated saving per run (USD, ESTIMATE).
    # None for other kinds and for records saved before it existed.
    est_saving_per_run: float | None = None


# --- atomic file helpers ------------------------------------------------------

def _mkdirs(path: Path) -> None:
    """mkdir -p with mode 0700 on every directory created."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)
        d.chmod(0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    _mkdirs(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- ledger ---------------------------------------------------------------------

def _check_id(fix_id: str) -> None:
    if not isinstance(fix_id, str) or not _ID.fullmatch(fix_id) or ".." in fix_id:
        raise ValueError(f"unsafe fix id {fix_id!r}")


def _compat(r: dict) -> dict:
    """Records saved before live_from existed: live from applied_at unless the fix
    needed a restart (then unknown, so None)."""
    if "live_from" not in r:
        r = dict(r, live_from=None if r.get("needs_restart") else r.get("applied_at"))
    return r


class Ledger:
    def __init__(self, root: Path, records: list[FixRecord] | None = None):
        self.root = Path(root)
        self.records: list[FixRecord] = records if records is not None else []

    @classmethod
    def load(cls, root: Path) -> Ledger:
        path = Path(root) / "ledger.json"
        if not path.exists():
            return cls(root)
        return cls(root, [FixRecord(**_compat(r)) for r in _read_json(path)["records"]])

    def save(self) -> None:
        _write_json(self.root / "ledger.json", {"records": [asdict(r) for r in self.records]})

    def version_dir(self, fix_id: str) -> Path:
        _check_id(fix_id)
        return self.root / "versions" / fix_id

    def get(self, fix_id: str) -> FixRecord | None:
        return next((r for r in self.records if r.id == fix_id), None)

    def add(self, record: FixRecord, fix: Fix) -> None:
        if self.get(record.id) is not None:
            raise ValueError(f"fix {record.id} is already in the ledger")
        _write_json(self.version_dir(record.id) / "fix.json", asdict(fix))
        self.records.append(record)
        self.save()

    def fix(self, fix_id: str) -> Fix:
        return Fix(**_read_json(self.version_dir(fix_id) / "fix.json"))

    def active_for(self, target: str) -> FixRecord | None:
        return next((r for r in self.records
                     if r.target == target and r.state in ACTIVE_STATES), None)


# --- helpers shared by apply and rollback -----------------------------------------

class _Failed(Exception):
    """Apply/rollback could not complete; the message becomes the record's reason."""


def needs_restart(fix: Fix) -> bool:
    if fix.kind == "config":
        return True
    if fix.kind == "new_reach":
        change = fix.payload.get("change")
        return isinstance(change, dict) and change.get("type") == "mcp_include_add"
    if fix.kind == "composed_tool":
        return policy.COMPOSED_TOOL_NEEDS_RESTART
    return False


def _norm(v: Any) -> Any:
    """Hermes coerces config strings (on/off/true/false/digits); compare after the same."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "on"):
            return True
        if s in ("false", "no", "off"):
            return False
        if s.isdigit():
            return int(s)
        if s.replace(".", "", 1).isdigit():
            return float(s)
    return v


def _hermes_coerce(value_str: str, default_is_str: bool) -> Any:
    """What `hermes config set` stores for `value_str` (see bridge.hermes_coerce)."""
    return hermes_coerce(value_str, default_is_str)


def _round_trips(value: Any, default_is_str: bool) -> bool:
    """True when `value` is a scalar that setting it via its string form stores
    unchanged, for a key whose default is (or is not) a string."""
    if not isinstance(value, (bool, int, float, str)):
        return False
    try:
        back = _hermes_coerce(encode_config_value(value), default_is_str)
    except ValueError:
        return False
    return type(back) is type(value) and back == value


def _strict_equal(a: Any, b: Any) -> bool:
    """Same type and value, no normalising: "off" is not False."""
    return type(a) is type(b) and a == b


def _same_config(a: Any, b: Any) -> bool:
    return type(_norm(a)) is type(_norm(b)) and _norm(a) == _norm(b)


def _job_after(live: dict, prompt: str, add: list[str]) -> dict:
    """The prompt and toolsets to write. A None toolsets list means all tools and
    stays None: adding a list would narrow the job."""
    cur = live.get("enabled_toolsets")
    toolsets = cur
    if add and isinstance(cur, list):
        toolsets = list(cur) + [t for t in dict.fromkeys(add) if t not in cur]
    return {"prompt": prompt, "enabled_toolsets": toolsets}


def _add_toolsets(payload: dict, default: list[str]) -> list[str]:
    add = payload.get("add_toolsets", default)
    if not isinstance(add, list) or any(t != policy.TOOLSET for t in add):
        raise ValueError(f"add_toolsets may only contain {policy.TOOLSET!r}")
    return add


def job_after_for(fix: Fix, live: dict) -> dict | None:
    """The job's `prompt` and `enabled_toolsets` after `fix` is applied to `live`.

    The one place that decides this; `apply` and the shadow run both use it.
    Returns None for a composed_tool fix without a job_prompt (the job is not
    touched). A reducer changes tool results, not the job: its shadow runs the live
    prompt and toolsets unchanged. Raises ValueError for a fix kind with no job
    change, a bad prompt or a bad add_toolsets.
    """
    p = fix.payload if isinstance(fix.payload, dict) else {}
    if fix.kind == "reducer":
        return {"prompt": live.get("prompt"), "enabled_toolsets": live.get("enabled_toolsets")}
    if fix.kind == "job_prompt":
        prompt, add = p.get("prompt"), None
        if not isinstance(prompt, str):
            raise ValueError("job_prompt payload needs a prompt string")
        add = _add_toolsets(p, [])
    elif fix.kind == "composed_tool":
        prompt = p.get("job_prompt")
        if prompt is None:
            return None
        if not isinstance(prompt, str):
            raise ValueError("job_prompt must be a string or null")
        add = _add_toolsets(p, [policy.TOOLSET])
    else:
        raise ValueError(f"{fix.kind!r} fixes do not change a job")
    return _job_after(live, prompt, add)


def _read_job(bridge: Bridge, job_id: str) -> dict:
    job = bridge.get_job(job_id)
    if job is None:
        raise _Failed(f"job {job_id} not found")
    return job


def _resolve_skill(skills_root: Path, rel: Any) -> Path:
    if not isinstance(rel, str):
        raise _Failed("skill payload needs a string path")
    root = Path(skills_root).resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or p.name != "SKILL.md" or p.parent == root:
        raise _Failed(f"skill path {rel!r} is not a skill file under the skills directory")
    return p


def _tool_files(p: dict) -> tuple[dict, str]:
    spec = {k: p.get(k) for k in ("name", "description", "parameters", "calls", "mutating")}
    program = p.get("program")
    name = spec["name"]
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ValueError(f"tool name must match {_TOOL_NAME.pattern}")
    if not isinstance(program, str):
        raise ValueError("composed_tool payload needs a program string")
    return spec, program


def _write_tool_dir(tools: Path, name: str, spec: dict, program: str) -> None:
    final = tools / name
    if final.exists():
        raise _Failed(f"tool {name} already exists")
    _mkdirs(tools)
    tmp = Path(tempfile.mkdtemp(dir=tools, prefix=".tmp-"))
    try:
        tmp.chmod(0o700)
        atomic_write(tmp / "tool.json", json.dumps(spec, indent=2).encode())
        atomic_write(tmp / "program.py", program.encode("utf-8"))
        os.rename(tmp, final)  # same filesystem; fails rather than replacing a dir
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def shadow_tool_spec(spec: dict, fix_id: str) -> dict:
    """tool.json as written at shadow scheduling: the plugin runs the tool only in the
    shadow session of fix `fix_id` until go-live rewrites it without the flags (F3)."""
    return dict(spec, shadow_only=True, shadow_fix=fix_id)


def write_shadow_tool(tools: Path, payload: dict, fix_id: str) -> str:
    """Write a composed_tool payload's tool dir for fix `fix_id`'s shadow run, flagged
    shadow_only. Returns the tool name. Raises ValueError for a bad payload and
    _Failed if the tool already exists."""
    spec, program = _tool_files(payload)
    _write_tool_dir(Path(tools), spec["name"], shadow_tool_spec(spec, fix_id), program)
    return spec["name"]


def is_shadow_tool(tools: Path, payload: dict, fix_id: str) -> bool:
    """True when `tools` holds exactly this payload's shadow-only tool dir for fix
    `fix_id`, or the same dir as written before tool.json named its fix (shadow_only
    alone)."""
    try:
        spec, program = _tool_files(payload)
    except ValueError:
        return False
    return any(_tool_matches(Path(tools), {"spec": s, "program": program})
               for s in (shadow_tool_spec(spec, fix_id), dict(spec, shadow_only=True)))


def _tool_matches(tools: Path, after: dict) -> bool:
    d = tools / after["spec"]["name"]
    try:
        return (_read_json(d / "tool.json") == after["spec"]
                and (d / "program.py").read_text(encoding="utf-8") == after["program"])
    except (OSError, ValueError):
        return False


def reducer_spec(payload: Any) -> dict:
    """The reducer spec in a reducer fix payload: the payload without BASE_KEY.
    Raises ValueError if it fails validate_spec."""
    if not isinstance(payload, dict):
        raise ValueError("reducer payload must be an object")
    spec = {k: v for k, v in payload.items() if k != BASE_KEY}
    errs = validate_spec(spec)
    if errs:
        raise ValueError("invalid reducer spec: " + "; ".join(errs))
    return spec


def _reducer_path(root: Path, fix_id: str) -> Path:
    _check_id(fix_id)
    return Path(root) / "reducers" / f"{fix_id}.json"


def _write_reducer(root: Path, fix_id: str, spec: dict, shadow: bool) -> None:
    """reducers/<fix id>.json. A shadow file applies only in fix `fix_id`'s shadow
    session; a live one only in the runs of spec["job"]."""
    data = {"spec": spec, "fix_id": fix_id, "shadow_only": shadow,
            "shadow_fix": fix_id if shadow else None}
    atomic_write(_reducer_path(root, fix_id),
                 json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


def write_shadow_reducer(root: Path, payload: dict, fix_id: str) -> None:
    """Write fix `fix_id`'s reducer file for its shadow run, flagged shadow_only.
    `root` is the tuner directory. Raises ValueError for a bad payload or id."""
    _write_reducer(root, fix_id, reducer_spec(payload), shadow=True)


def remove_reducer(root: Path, fix_id: str) -> None:
    """Delete fix `fix_id`'s reducer file, if there is one."""
    _reducer_path(root, fix_id).unlink(missing_ok=True)


def _set_state(ledger: Ledger, rec: FixRecord, state: State, reason: str = "") -> FixRecord:
    rec.state, rec.reason = state, reason
    ledger.save()
    return rec


# --- apply ------------------------------------------------------------------------

def apply(fix: Fix, ledger: Ledger, bridge: Bridge, root: Path, now: float,
          skills_root: Path | None = None) -> FixRecord:
    """Apply `fix`, saving before.json first and after.json. Returns the record
    (state `trial`, or `bridge_error` with the reason). `skills_root` defaults to
    `root.parent / "skills"`. `new_reach` is applied elsewhere and raises ValueError."""
    root = Path(root)
    skills_root = Path(skills_root) if skills_root is not None else root.parent / "skills"
    if fix.kind not in ("job_prompt", "config", "skill", "composed_tool", "reducer"):
        raise ValueError(f"apply does not handle {fix.kind!r} fixes")
    if not isinstance(fix.payload, dict):
        raise ValueError("payload must be an object")
    p = fix.payload
    # Validate everything that can be validated before any state changes.
    if fix.kind == "job_prompt":
        if not isinstance(p.get("prompt"), str):
            raise ValueError("job_prompt payload needs a prompt string")
        job_after_for(fix, {})  # validate before any state changes
    elif fix.kind == "config":
        if not isinstance(p.get("key"), str) or "value" not in p:
            raise ValueError("config payload needs a key and a value")
        encode_config_value(p["value"])  # ValueError for None/list/dict, before any write
        try:
            default_is_str = bridge.config_default_is_str(p["key"])
        except BridgeError as exc:  # fail closed: recorded as bridge_error below
            lookup_error, default_is_str = exc, False
        else:
            lookup_error = None
            if not _round_trips(p["value"], default_is_str):  # -1, NaN, 1e-05, "on" on a bool key
                raise ValueError(f"config value {p['value']!r} does not survive "
                                 "`hermes config set` unchanged")
    elif fix.kind == "composed_tool":
        spec, program = _tool_files(p)
        job_prompt = p.get("job_prompt")
        if job_prompt is not None and not isinstance(job_prompt, str):
            raise ValueError("job_prompt must be a string or null")
        job_after_for(fix, {})
    elif fix.kind == "reducer":
        rspec = reducer_spec(p)
        if rspec["job"] != fix.target:
            raise ValueError("reducer job must be the fix's target")
        _reducer_path(root, fix.id)  # ValueError for an unsafe id
    rec = ledger.get(fix.id)
    if rec is None:
        rec = FixRecord(fix.id, fix.kind, fix.target, fix.candidate_key,
                        "shadow_pending", now)
        ledger.add(rec, fix)
    elif rec.state in APPLIED_STATES:
        raise ValueError(f"fix {fix.id} is already applied")
    vdir = ledger.version_dir(fix.id)
    after_path = vdir / "after.json"
    undo = None  # called to revert partial work on failure
    wrote = False  # a write to Hermes was attempted: it may have landed
    try:
        if fix.kind == "job_prompt":
            live = _read_job(bridge, fix.target)
            before = {"prompt": live.get("prompt"),
                      "enabled_toolsets": live.get("enabled_toolsets")}
            after = job_after_for(fix, live)
            _write_json(vdir / "before.json", before)
            _write_json(after_path, after)
            wrote = True
            bridge.update_job(fix.target, _job_updates(before, after))
        elif fix.kind == "config":
            key = p["key"]
            if lookup_error is not None:
                raise lookup_error
            old = bridge.config_get(key)
            if old is None:
                raise _Failed(f"config key {key!r} has no current value to roll back to")
            if not _round_trips(old, default_is_str):
                raise _Failed(f"config key {key!r} current value cannot be restored "
                              "through `hermes config set`")
            _write_json(vdir / "before.json", {"key": key, "value": old})
            _write_json(after_path, {"key": key, "value": p["value"]})
            wrote = True
            bridge.config_set(key, p["value"])
            stored = _verify_config_write(bridge, key, p["value"], old)
            _write_json(after_path, {"key": key, "value": stored})  # what Hermes stored
        elif fix.kind == "skill":
            path = _resolve_skill(skills_root, p.get("path"))
            text = p.get("text")
            if not isinstance(text, str):
                raise _Failed("skill payload needs a text string")
            old_bytes = path.read_bytes()
            mode = path.stat().st_mode & 0o777
            _write_json(vdir / "before.json", {
                "file": str(path), "b64": base64.b64encode(old_bytes).decode(), "mode": mode})
            _write_json(after_path, {"file": str(path), "text": text})
            atomic_write(path, text.encode("utf-8"), mode)
        elif fix.kind == "reducer":
            # Never touches Hermes: the plugin's hook reads the file on each call.
            _write_json(vdir / "before.json", {"reducer": None})
            _write_json(after_path, {"file": f"reducers/{fix.id}.json", "spec": rspec})
            undo = lambda: remove_reducer(root, fix.id)  # noqa: E731
            _write_reducer(root, fix.id, rspec, shadow=False)
        else:
            tools = root / "tools"
            before: dict = {"tool": None, "job": None}
            after = {"spec": spec, "program": program, "job": None}
            if job_prompt is not None:
                live = _read_job(bridge, fix.target)
                before["job"] = {"prompt": live.get("prompt"),
                                 "enabled_toolsets": live.get("enabled_toolsets")}
                after["job"] = job_after_for(fix, live)
            _write_json(vdir / "before.json", before)
            _write_json(after_path, after)
            if is_shadow_tool(tools, p, fix.id):  # written at shadow scheduling: drop the flags
                atomic_write(tools / spec["name"] / "tool.json",
                             json.dumps(spec, indent=2).encode())
            else:
                _write_tool_dir(tools, spec["name"], spec, program)
            undo = lambda: shutil.rmtree(tools / spec["name"], ignore_errors=True)  # noqa: E731
            if job_prompt is not None:
                wrote = True
                bridge.update_job(fix.target, _job_updates(before["job"], after["job"]))
    except (BridgeError, _Failed, OSError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        # _Failed after a write comes from _verify_config_write, which has already
        # restored (or named what is left); only a lost reply leaves the outcome unknown.
        if not wrote or isinstance(exc, _Failed):
            if undo:
                undo()
            after_path.unlink(missing_ok=True)
            return _set_state(ledger, rec, "bridge_error", reason)
        # The write may have landed (e.g. Hermes stored it, then the reply timed
        # out). Revert it the way a rollback does, which checks what is live.
        _set_state(ledger, rec, "rollback_pending", f"apply failed after a write: {reason}")
        out = rollback(rec, ledger, bridge, root, now, skills_root=skills_root)
        if out.state == "rolled_back":
            after_path.unlink(missing_ok=True)
            return _set_state(ledger, rec, "bridge_error", reason)
        if out.state == "rollback_pending":  # the revert failed too: the cycle retries it
            return _set_state(ledger, rec, "rollback_pending",
                              f"apply failed after a write: {reason}; {out.reason}")
        return out  # owner_changed
    rec.applied_at, rec.needs_restart = now, needs_restart(fix)
    rec.live_from = None if rec.needs_restart else now
    return _set_state(ledger, rec, "trial")


def _verify_config_write(bridge: Bridge, key: str, value: Any, old: Any) -> Any:
    """Read `key` back after a write of `value` and return what was read, which is
    exactly `value` (same type, no normalising). Otherwise put `old` back, check
    that, and raise _Failed."""
    stored = bridge.config_get(key)
    if _strict_equal(stored, value):
        return stored
    bridge.config_set(key, old)
    left = bridge.config_get(key)
    if _strict_equal(left, old):
        raise _Failed(f"config {key} did not store as written")
    raise _Failed(f"config {key} did not store as written, and the restore of the old value "
                  f"did not stick: {left!r} is left in place")


def _job_updates(before: dict, after: dict) -> dict:
    """Only the job fields this fix changes."""
    return {k: after[k] for k in ("prompt", "enabled_toolsets") if after[k] != before[k]}


# --- rollback -----------------------------------------------------------------------

def rollback(record: FixRecord, ledger: Ledger, bridge: Bridge, root: Path, now: float,
             skills_root: Path | None = None) -> FixRecord:
    """Undo an applied fix. Anything the owner changed since apply is left alone
    (state `owner_changed`); otherwise before.json is restored exactly (`rolled_back`)."""
    root = Path(root)
    skills_root = Path(skills_root) if skills_root is not None else root.parent / "skills"
    if record.state not in APPLIED_STATES and record.state != "rollback_pending":
        raise ValueError(f"fix {record.id} is {record.state}, not applied")
    vdir = ledger.version_dir(record.id)
    before, after = _read_json(vdir / "before.json"), _read_json(vdir / "after.json")
    changed: list[str] = []  # parts left alone because the owner changed them
    try:
        if record.kind == "job_prompt":
            _rollback_job(bridge, record.target, before, after, changed)
        elif record.kind == "config":
            key = before["key"]
            default_is_str = bridge.config_default_is_str(key)
            cur = bridge.config_get(key)
            if _strict_equal(cur, before["value"]):
                pass  # already restored (a retry)
            elif _strict_equal(cur, after["value"]):
                if not _round_trips(before["value"], default_is_str):
                    raise _Failed(f"config {key} old value cannot be restored through "
                                  "`hermes config set`")
                bridge.config_set(key, before["value"])
                back = bridge.config_get(key)
                if not _strict_equal(back, before["value"]):
                    raise _Failed(f"config {key} did not restore: reads {back!r}, "
                                  f"expected {before['value']!r}")
            else:
                changed.append(f"config {key}")
        elif record.kind == "skill":
            path = _resolve_skill(skills_root, after["file"])  # absolute path; re-checked
            original = base64.b64decode(before["b64"])
            try:
                cur = path.read_bytes()
            except OSError:
                cur = None
            if cur == original:
                pass  # already restored (a retry)
            elif cur == after["text"].encode("utf-8"):
                atomic_write(path, original, before["mode"])
            else:
                changed.append("skill file")
        elif record.kind == "composed_tool":
            tools = root / "tools"
            d = tools / after["spec"]["name"]
            # Job first: if it fails the tool dir is still there for the job to use.
            if after["job"] is not None:
                _rollback_job(bridge, record.target, before["job"], after["job"], changed)
            if not d.exists():
                pass
            elif changed:  # the owner kept editing the job, which may still use the tool
                changed.append("tool dir (the job may still use it)")
            elif _tool_matches(tools, after):
                shutil.rmtree(d)
            else:
                changed.append("tool dir")
        elif record.kind == "reducer":
            remove_reducer(root, record.id)
        else:
            raise ValueError(f"cannot roll back {record.kind!r}")
    except (BridgeError, _Failed, OSError) as exc:
        # Stay applied (and active) so the rollback can be retried; parts already
        # restored are detected and skipped next time.
        record.reason = f"rollback failed: {type(exc).__name__}: {exc}"
        ledger.save()
        return record
    if changed:
        return _set_state(ledger, record, "owner_changed",
                          "owner changed since apply, left alone: " + ", ".join(changed))
    return _set_state(ledger, record, "rolled_back")


def _rollback_job(bridge: Bridge, job_id: str, before: dict, after: dict,
                  changed: list[str]) -> None:
    mine = _job_updates(before, after)  # the fields apply wrote
    live = _read_job(bridge, job_id)
    if all(live.get(k) == before[k] for k in mine):
        return  # already restored (a retry)
    if any(live.get(k) != after[k] for k in mine):
        changed.append(f"job {job_id}")
    elif mine:
        bridge.update_job(job_id, {k: before[k] for k in mine})
