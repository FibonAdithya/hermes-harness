"""Owner-approved new_reach proposals (spec 4.6).

The approvals listener records approvals in the broker's state directory as
`approved_proposals.json`: a list of {"proposal_id", "hash", "approved_at"}.
The tuner writes each proposal to `<root>/proposals/<id>.json` as
{"id", "change", "why", "hash"}. A proposal counts as approved only while the
hash of its current `change` equals the hash the owner approved, so editing a
proposal after approval voids it. The broker also records drops in
`dropped_proposals.json` (same shape, "dropped_at"). This module only reads the
broker's files; it writes the proposals and applies approved changes.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .bridge import Bridge, BridgeError
from .ledger import FixRecord, Ledger, _set_state, _write_json
from .model import Fix

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")


def change_hash(change: dict) -> str:
    blob = json.dumps(change, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _safe_id(pid: object) -> bool:
    return isinstance(pid, str) and bool(_SAFE_ID.fullmatch(pid)) and ".." not in pid


def write_proposal(fix: Fix, root: Path) -> str:
    """Write `<root>/proposals/<fix.id>.json` and return the proposal id."""
    if fix.kind != "new_reach":
        raise ValueError(f"only new_reach fixes become proposals, not {fix.kind!r}")
    if not _safe_id(fix.id):
        raise ValueError(f"unsafe proposal id {fix.id!r}")
    p = fix.payload if isinstance(fix.payload, dict) else {}
    change, why = p.get("change"), p.get("why", "")
    if not isinstance(change, dict) or not isinstance(why, str):
        raise ValueError("new_reach payload needs a change object and a why string")
    _write_json(Path(root) / "proposals" / f"{fix.id}.json",
                {"id": fix.id, "change": change, "why": why, "hash": change_hash(change)})
    return fix.id


def _matching(root: Path, broker_state: Path, filename: str) -> list[dict]:
    entries = _read_json(Path(broker_state) / filename)
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pid, want = entry.get("proposal_id"), entry.get("hash")
        if not _safe_id(pid) or not isinstance(want, str):
            continue
        doc = _read_json(Path(root) / "proposals" / f"{pid}.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("change"), dict):
            continue
        try:
            got = change_hash(doc["change"])
        except (TypeError, ValueError):
            continue
        if got == want:
            out.append(doc)
    return out


def approved(root: Path, broker_state: Path) -> list[dict]:
    return _matching(root, broker_state, "approved_proposals.json")


def dropped(root: Path, broker_state: Path) -> list[dict]:
    """Proposals the owner dropped, by the same hash rule as `approved`."""
    return _matching(root, broker_state, "dropped_proposals.json")


def approved_callables(root: Path, broker_state: Path) -> frozenset[str]:
    tools = set()
    for doc in approved(root, broker_state):
        change = doc["change"]
        tool = change.get("tool")
        if change.get("type") == "callable_add" and isinstance(tool, str) and tool:
            tools.add(tool)
    return frozenset(tools)


# --- applying an approved change ---------------------------------------------------

_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"unsafe {what} {value!r}")
    return value


def apply_approved(entry: dict, bridge: Bridge, ledger: Ledger, root: Path,
                   now: float) -> FixRecord:
    """Apply exactly the change in `entry` (a proposal doc returned by `approved`).

    The only code path allowed to write an `mcp_servers.` key (policy forbids it
    for ordinary config fixes). Re-applying an applied proposal does nothing.
    Raises ValueError for a malformed entry before anything is touched.
    """
    pid, change = entry.get("id"), entry.get("change")
    if not _safe_id(pid) or not isinstance(change, dict):
        raise ValueError("malformed proposal entry")
    kind = change.get("type")
    tool = _name(change.get("tool"), "tool")
    if kind == "mcp_include_add":
        key = f"mcp_servers.{_name(change.get('server'), 'server')}.tools.include"
    elif kind == "callable_add":
        key = None
    else:
        raise ValueError(f"unsupported change type {kind!r}")
    rec = ledger.get(pid)
    if rec is not None and rec.state == "applied":
        return rec
    if rec is None:
        fix = Fix(pid, "new_reach", tool, {"change": change, "why": entry.get("why", "")},
                  f"proposal:{pid}")
        rec = FixRecord(pid, "new_reach", tool, fix.candidate_key, "approved", now)
        ledger.add(rec, fix)
    if key is None:
        rec.applied_at, rec.needs_restart = now, False
        return _set_state(ledger, rec, "applied")
    vdir = ledger.version_dir(pid)
    try:
        old = bridge.config_get(key)
        if not isinstance(old, list) or not all(isinstance(t, str) for t in old):
            raise ValueError(f"{key} is not a list of strings: refusing to change it")
        new = old if tool in old else old + [tool]
        _write_json(vdir / "before.json", {"key": key, "value": old})
        _write_json(vdir / "after.json", {"key": key, "value": new})
        if new is not old:
            bridge.config_set_json(key, new)
    except (BridgeError, ValueError, OSError) as exc:
        return _set_state(ledger, rec, "bridge_error", f"{type(exc).__name__}: {exc}")
    rec.applied_at, rec.needs_restart = now, True
    return _set_state(ledger, rec, "applied")
