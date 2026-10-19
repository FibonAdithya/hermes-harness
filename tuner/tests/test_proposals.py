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


def applied_setup(tmp_path, include, change=None):
    change = change or VECTOR_CHANGE
    root, state = setup(tmp_path, "fix-reach-1", change)
    return root, state, FakeBridge(config={INC_KEY: include}), Ledger(root)


def vers(ledger):
    return ledger.version_dir("fix-reach-1")


def test_apply_mcp_include_add_appends_exactly_one_tool(tmp_path):
    root, state, bridge, ledger = applied_setup(tmp_path, ["search_gmail_messages", "get_events"])
    rec = proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=100.0)
    assert rec.state == "applied" and rec.needs_restart is True and rec.kind == "new_reach"
    want = ["search_gmail_messages", "get_events", "modify_gmail_labels"]
    assert bridge.config[INC_KEY] == want
    assert [c for c in bridge.calls if c[0] in FakeBridge.MUTATING] == [("config_set_json", (INC_KEY, want))]
    assert json.loads((vers(ledger) / "before.json").read_text()) == {
        "key": INC_KEY, "value": ["search_gmail_messages", "get_events"]}
    assert json.loads((vers(ledger) / "after.json").read_text())["value"][-1] == "modify_gmail_labels"
    assert Ledger.load(root).get("fix-reach-1").state == "applied"


def test_unapproved_proposal_is_not_applied(tmp_path):
    """Catches writing an mcp_servers key for a well-formed but unapproved proposal."""
    root, state, bridge, ledger = applied_setup(tmp_path, ["a"])
    (state / "approved_proposals.json").write_text("[]")
    assert proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=1.0) is None
    assert proposals.apply_approved("nope", bridge, ledger, root, state, now=1.0) is None
    assert bridge.calls == [] and ledger.records == []


def test_approved_then_edited_proposal_is_not_applied(tmp_path):
    root, state, bridge, ledger = applied_setup(tmp_path, ["a"])
    path = root / "proposals" / "fix-reach-1.json"
    doc = json.loads(path.read_text())
    doc["change"]["tool"] = "terminal"
    doc["hash"] = proposals.change_hash(doc["change"])
    path.write_text(json.dumps(doc))
    assert proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=1.0) is None
    assert bridge.calls == []


def test_apply_is_idempotent(tmp_path):
    root, state, bridge, ledger = applied_setup(tmp_path, ["a"])
    proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=100.0)
    n = len(bridge.calls)
    rec = proposals.apply_approved("fix-reach-1", bridge, Ledger.load(root), root, state, now=200.0)
    assert rec.state == "applied" and rec.applied_at == 100.0
    assert len(bridge.calls) == n


@pytest.mark.parametrize("state_name", ["applied", "trial", "kept", "rolled_back", "owner_changed",
                                        "dropped", "rejected", "shadow_pending", "shadow_failed"])
def test_other_states_are_never_reapplied(tmp_path, state_name):
    """Approvals persist forever; a rolled-back or kept fix must not be re-applied."""
    from hermes_tuner.ledger import FixRecord
    root, state, bridge, ledger = applied_setup(tmp_path, ["a"])
    rec = FixRecord("fix-reach-1", "new_reach", "modify_gmail_labels", "k", state_name, 1.0,
                    applied_at=5.0, needs_restart=False)
    ledger.add(rec, reach_fix())
    before_file = vers(ledger) / "before.json"
    before_file.write_text('{"keep": "me"}')
    out = proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=99.0)
    assert out.state == state_name and out.applied_at == 5.0 and out.needs_restart is False
    assert bridge.calls == [] and before_file.read_text() == '{"keep": "me"}'


def test_bridge_error_record_is_retried_and_keeps_before_json(tmp_path):
    from hermes_tuner.ledger import FixRecord
    root, state, bridge, ledger = applied_setup(tmp_path, ["a"])
    ledger.add(FixRecord("fix-reach-1", "new_reach", "t", "k", "bridge_error", 1.0), reach_fix())
    (vers(ledger) / "before.json").write_text('{"key": "orig", "value": ["a"]}')
    rec = proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=9.0)
    assert rec.state == "applied"
    assert json.loads((vers(ledger) / "before.json").read_text())["key"] == "orig"


def test_apply_tool_already_included_makes_no_write(tmp_path):
    root, state, bridge, ledger = applied_setup(tmp_path, ["modify_gmail_labels", "a"])
    rec = proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=1.0)
    assert rec.state == "applied"
    assert bridge.config[INC_KEY] == ["modify_gmail_labels", "a"]
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


@pytest.mark.parametrize("current", [None, "a", ["a", 3], {"a": 1}])
def test_apply_refuses_an_include_that_is_not_a_list_of_str(tmp_path, current):
    root, state, bridge, ledger = applied_setup(tmp_path, current)
    rec = proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=1.0)
    assert rec.state == "bridge_error"
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


def test_apply_bridge_failure_is_recorded(tmp_path):
    root, state, _b, ledger = applied_setup(tmp_path, ["a"])
    bridge = FakeBridge(config={INC_KEY: ["a"]}, fail={"config_set_json"})
    rec = proposals.apply_approved("fix-reach-1", bridge, ledger, root, state, now=1.0)
    assert rec.state == "bridge_error" and bridge.config[INC_KEY] == ["a"]


def test_apply_touches_only_the_approved_server_key(tmp_path):
    change = {"type": "mcp_include_add", "server": "g/../x", "tool": "t"}
    root, state = setup(tmp_path, "fix-reach-1", change)
    bridge = FakeBridge()
    with pytest.raises(ValueError):
        proposals.apply_approved("fix-reach-1", bridge, Ledger(root), root, state, now=1.0)
    assert bridge.calls == []


def test_apply_callable_add_is_ledger_only(tmp_path):
    root, state = setup(tmp_path, "p2", {"type": "callable_add", "tool": "web_fetch"})
    bridge = FakeBridge()
    rec = proposals.apply_approved("p2", bridge, Ledger(root), root, state, now=5.0)
    assert rec.state == "applied" and rec.needs_restart is False
    assert bridge.calls == []


def test_config_set_json_is_mutating_in_fake_bridge():
    assert "config_set_json" in FakeBridge.MUTATING
    b = FakeBridge()
    b.config_set_json("k.v", ["x"])
    assert b.config["k.v"] == ["x"] and b.calls == [("config_set_json", ("k.v", ["x"]))]
