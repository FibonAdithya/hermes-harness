"""Owner-approved proposals: only the exact hashed change counts."""
from __future__ import annotations

import json
from pathlib import Path

from hermes_tuner import proposals


def setup(tmp_path: Path, pid: str, change: dict, approved_hash: str | None = None):
    root = tmp_path / "tuner"
    state = tmp_path / "broker" / "state"
    (root / "proposals").mkdir(parents=True)
    state.mkdir(parents=True)
    h = proposals.change_hash(change)
    (root / "proposals" / f"{pid}.json").write_text(json.dumps(
        {"id": pid, "change": change, "why": "needed", "hash": h}))
    (state / "approved_proposals.json").write_text(json.dumps(
        [{"proposal_id": pid, "hash": approved_hash or h, "approved_at": "2026-10-19T00:00:00Z"}]))
    return root, state


def test_change_hash_is_canonical():
    a = proposals.change_hash({"type": "callable_add", "tool": "web_fetch"})
    b = proposals.change_hash({"tool": "web_fetch", "type": "callable_add"})
    assert a == b
    assert a != proposals.change_hash({"type": "callable_add", "tool": "web_fetchx"})
    assert len(a) == 64


def test_approved_callables_happy_path(tmp_path):
    root, state = setup(tmp_path, "p1", {"type": "callable_add", "tool": "web_fetch"})
    assert [p["id"] for p in proposals.approved(root, state)] == ["p1"]
    assert proposals.approved_callables(root, state) == frozenset({"web_fetch"})


def test_non_callable_proposal_not_a_callable(tmp_path):
    root, state = setup(tmp_path, "p2",
                        {"type": "mcp_include_add", "server": "google", "tool": "x"})
    assert len(proposals.approved(root, state)) == 1
    assert proposals.approved_callables(root, state) == frozenset()


def test_edited_proposal_not_approved(tmp_path):
    root, state = setup(tmp_path, "p1", {"type": "callable_add", "tool": "web_fetch"})
    path = root / "proposals" / "p1.json"
    doc = json.loads(path.read_text())
    doc["change"]["tool"] = "terminal"
    # Stored hash left as approved: the proposal's own hash must not be trusted.
    path.write_text(json.dumps(doc))
    assert proposals.approved(root, state) == []
    assert proposals.approved_callables(root, state) == frozenset()
    # Stored hash updated to match the edit: still not the approved change.
    doc["hash"] = proposals.change_hash(doc["change"])
    path.write_text(json.dumps(doc))
    assert proposals.approved(root, state) == []


def test_path_traversal_proposal_id_ignored(tmp_path):
    change = {"type": "callable_add", "tool": "web_fetch"}
    root, state = setup(tmp_path, "p1", change)
    outside = tmp_path / "evil.json"
    outside.write_text(json.dumps({"id": "evil", "change": change, "why": "",
                                   "hash": proposals.change_hash(change)}))
    (state / "approved_proposals.json").write_text(json.dumps([
        {"proposal_id": "../../evil", "hash": proposals.change_hash(change),
         "approved_at": "x"}]))
    assert proposals.approved(root, state) == []


def test_missing_or_corrupt_state_is_empty(tmp_path):
    root = tmp_path / "tuner"
    state = tmp_path / "state"
    assert proposals.approved(root, state) == []
    state.mkdir()
    (state / "approved_proposals.json").write_text("{not json")
    assert proposals.approved(root, state) == []
    (state / "approved_proposals.json").write_text(json.dumps({"proposal_id": "p1"}))
    assert proposals.approved(root, state) == []
    (state / "approved_proposals.json").write_text(json.dumps(["p1", None, {"proposal_id": 3}]))
    assert proposals.approved(root, state) == []
