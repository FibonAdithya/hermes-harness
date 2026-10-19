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


# --- Task 9: writing proposals, dropped(), applying approved changes -------------

import os
import stat

import pytest

from hermes_tuner.bridge import BridgeError, FakeBridge
from hermes_tuner.ledger import Ledger
from hermes_tuner.model import Fix

VECTOR_CHANGE = {"type": "mcp_include_add", "server": "google", "tool": "modify_gmail_labels"}
VECTOR_HASH = "f429e5600f10aa80623b0c46de4c9fc48f53bbb369e92d958e8609bfbd5351e7"
INC_KEY = "mcp_servers.google.tools.include"


def test_shared_hash_vector_matches_broker():
    """The same literal is asserted in broker/tests/test_proposals.py; drift in either
    canonical form breaks one of the two."""
    assert proposals.change_hash(VECTOR_CHANGE) == VECTOR_HASH


def reach_fix(change=None, fid="fix-reach-1"):
    change = change or VECTOR_CHANGE
    return Fix(fid, "new_reach", change["tool"], {"change": change, "why": "needs labels"}, "k1")


def test_write_proposal_shape_and_mode(tmp_path):
    pid = proposals.write_proposal(reach_fix(), tmp_path)
    assert pid == "fix-reach-1"
    path = tmp_path / "proposals" / "fix-reach-1.json"
    assert json.loads(path.read_text()) == {
        "id": "fix-reach-1", "change": VECTOR_CHANGE, "why": "needs labels", "hash": VECTOR_HASH}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_proposal_rejects_other_kinds_and_unsafe_ids(tmp_path):
    bad_kind = Fix("f1", "config", "t", {"change": VECTOR_CHANGE, "why": ""}, "k")
    with pytest.raises(ValueError):
        proposals.write_proposal(bad_kind, tmp_path)
    with pytest.raises(ValueError):
        proposals.write_proposal(reach_fix(fid="../evil"), tmp_path)
    assert not (tmp_path / "proposals").exists()


def test_dropped_uses_same_hash_rule(tmp_path):
    root, state = setup(tmp_path, "p1", {"type": "callable_add", "tool": "web_fetch"})
    h = proposals.change_hash({"type": "callable_add", "tool": "web_fetch"})
    (state / "dropped_proposals.json").write_text(json.dumps(
        [{"proposal_id": "p1", "hash": h, "dropped_at": "x"}]))
    assert [p["id"] for p in proposals.dropped(root, state)] == ["p1"]
    (state / "dropped_proposals.json").write_text(json.dumps(
        [{"proposal_id": "p1", "hash": "0" * 64, "dropped_at": "x"}]))
    assert proposals.dropped(root, state) == []
    assert proposals.dropped(root, tmp_path / "nowhere") == []


def applied_setup(tmp_path, include):
    root = tmp_path / "tuner"
    proposals.write_proposal(reach_fix(), root)
    entry = json.loads((root / "proposals" / "fix-reach-1.json").read_text())
    bridge = FakeBridge(config={INC_KEY: include})
    return root, entry, bridge, Ledger(root)


def test_apply_mcp_include_add_appends_exactly_one_tool(tmp_path):
    root, entry, bridge, ledger = applied_setup(tmp_path, ["search_gmail_messages", "get_events"])
    rec = proposals.apply_approved(entry, bridge, ledger, root, now=100.0)
    assert rec.state == "applied" and rec.needs_restart is True and rec.kind == "new_reach"
    assert bridge.config[INC_KEY] == ["search_gmail_messages", "get_events", "modify_gmail_labels"]
    writes = [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]
    assert writes == [("config_set_json", (INC_KEY, ["search_gmail_messages", "get_events",
                                                     "modify_gmail_labels"]))]
    vdir = ledger.version_dir("fix-reach-1")
    assert json.loads((vdir / "before.json").read_text()) == {
        "key": INC_KEY, "value": ["search_gmail_messages", "get_events"]}
    assert json.loads((vdir / "after.json").read_text())["value"][-1] == "modify_gmail_labels"
    assert Ledger.load(root).get("fix-reach-1").state == "applied"


def test_apply_is_idempotent(tmp_path):
    root, entry, bridge, ledger = applied_setup(tmp_path, ["a"])
    proposals.apply_approved(entry, bridge, ledger, root, now=100.0)
    n = len(bridge.calls)
    rec = proposals.apply_approved(entry, bridge, Ledger.load(root), root, now=200.0)
    assert rec.state == "applied" and rec.applied_at == 100.0
    assert len(bridge.calls) == n


def test_apply_tool_already_included_makes_no_write(tmp_path):
    root, entry, bridge, ledger = applied_setup(tmp_path, ["modify_gmail_labels", "a"])
    rec = proposals.apply_approved(entry, bridge, ledger, root, now=1.0)
    assert rec.state == "applied"
    assert bridge.config[INC_KEY] == ["modify_gmail_labels", "a"]
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


@pytest.mark.parametrize("current", [None, "a", ["a", 3], {"a": 1}])
def test_apply_refuses_an_include_that_is_not_a_list_of_str(tmp_path, current):
    root, entry, bridge, ledger = applied_setup(tmp_path, current)
    rec = proposals.apply_approved(entry, bridge, ledger, root, now=1.0)
    assert rec.state == "bridge_error"
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


def test_apply_bridge_failure_is_recorded(tmp_path):
    root, entry, _b, ledger = applied_setup(tmp_path, ["a"])
    bridge = FakeBridge(config={INC_KEY: ["a"]}, fail={"config_set_json"})
    rec = proposals.apply_approved(entry, bridge, ledger, root, now=1.0)
    assert rec.state == "bridge_error" and bridge.config[INC_KEY] == ["a"]


def test_apply_touches_only_the_approved_server_key(tmp_path):
    change = {"type": "mcp_include_add", "server": "g/../x", "tool": "t"}
    root = tmp_path / "tuner"
    entry = {"id": "p9", "change": change, "why": "", "hash": proposals.change_hash(change)}
    bridge = FakeBridge()
    with pytest.raises(ValueError):
        proposals.apply_approved(entry, bridge, Ledger(root), root, now=1.0)
    assert bridge.calls == []


def test_apply_callable_add_is_ledger_only(tmp_path):
    change = {"type": "callable_add", "tool": "web_fetch"}
    root = tmp_path / "tuner"
    entry = {"id": "p2", "change": change, "why": "w", "hash": proposals.change_hash(change)}
    bridge = FakeBridge()
    rec = proposals.apply_approved(entry, bridge, Ledger(root), root, now=5.0)
    assert rec.state == "applied" and rec.needs_restart is False
    assert bridge.calls == []


def test_config_set_json_is_mutating_in_fake_bridge():
    assert "config_set_json" in FakeBridge.MUTATING
    b = FakeBridge()
    b.config_set_json("k.v", ["x"])
    assert b.config["k.v"] == ["x"] and b.calls == [("config_set_json", ("k.v", ["x"]))]
