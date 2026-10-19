"""Owner-approved new_reach proposals (spec 4.6).

The approvals listener records approvals in the broker's state directory as
`approved_proposals.json`: a list of {"proposal_id", "hash", "approved_at"}.
The tuner writes each proposal to `<root>/proposals/<id>.json` as
{"id", "change", "why", "hash"}. A proposal counts as approved only while the
hash of its current `change` equals the hash the owner approved, so editing a
proposal after approval voids it. This module only reads both files.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

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


def approved(root: Path, broker_state: Path) -> list[dict]:
    entries = _read_json(Path(broker_state) / "approved_proposals.json")
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


def approved_callables(root: Path, broker_state: Path) -> frozenset[str]:
    tools = set()
    for doc in approved(root, broker_state):
        change = doc["change"]
        tool = change.get("tool")
        if change.get("type") == "callable_add" and isinstance(tool, str) and tool:
            tools.add(tool)
    return frozenset(tools)
