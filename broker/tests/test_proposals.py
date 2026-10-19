"""New-reach proposals: codes bound to the hash of the exact change, approvals recorded."""
import json
import os
import stat

import pytest
from hermes_broker.proposals import ProposalStore, change_hash, record_approval, record_drop

# Same literal as tuner/tests/test_proposals.py: if either side's canonical form
# drifts, one of the two suites fails.
VECTOR_CHANGE = {"type": "mcp_include_add", "server": "google", "tool": "modify_gmail_labels"}
VECTOR_HASH = "f429e5600f10aa80623b0c46de4c9fc48f53bbb369e92d958e8609bfbd5351e7"
H1, H2 = "a" * 64, "b" * 64


@pytest.fixture
def store(tmp_path):
    return ProposalStore(tmp_path / "proposals.json")


def test_shared_hash_vector_matches_tuner():
    assert change_hash(VECTOR_CHANGE) == VECTOR_HASH
    assert change_hash({"tool": "modify_gmail_labels", "server": "google",
                        "type": "mcp_include_add"}) == VECTOR_HASH


def test_announce_once_then_take(store):
    code = store.announce("p1", H1, now=1.0, avoid=set())
    assert len(code) == 4 and code.isdigit()
    assert store.announce("p1", H1, now=2.0, avoid=set()) is None
    assert store.live_codes(2.0) == {code}
    assert store.take(code, now=3.0) == {"proposal_id": "p1", "hash": H1}
    assert store.take(code, now=3.0) is None


def test_changed_hash_is_a_new_announcement(store):
    a = store.announce("p1", H1, now=1.0, avoid=set())
    b = store.announce("p1", H2, now=2.0, avoid=set())
    assert b is not None and b != a
    assert store.take(a, now=3.0) is None  # the stale code no longer works
    assert store.take(b, now=3.0)["hash"] == H2


def test_expired_pending_proposal_is_reannounced_with_a_new_code(store):
    a = store.announce("p1", H1, now=1.0, avoid=set())
    later = 1.0 + 7 * 24 * 3600 + 1
    assert store.take(a, now=later) is None
    b = store.announce("p1", H1, now=later, avoid=set())
    assert b is not None


def test_settled_proposal_is_never_reannounced(store):
    """Catches re-announcing an approved or dropped proposal on every tick."""
    code = store.announce("p1", H1, now=1.0, avoid=set())
    assert store.settle(code, now=2.0) == {"proposal_id": "p1", "hash": H1}
    assert store.announce("p1", H1, now=3.0, avoid=set()) is None
    assert store.announce("p1", H1, now=1e9, avoid=set()) is None
    assert store.announce("p1", H2, now=3.0, avoid=set()) is not None  # edited: new change


def test_plain_take_gives_the_code_back_for_reannouncement(store):
    code = store.announce("p1", H1, now=1.0, avoid=set())
    store.take(code, now=2.0)
    assert store.announce("p1", H1, now=3.0, avoid=set()) is not None


def test_avoided_codes_are_never_issued(store, monkeypatch):
    import hermes_broker.proposals as mod
    draws = iter([1234, 1234, 5678])
    monkeypatch.setattr(mod.secrets, "randbelow", lambda n: next(draws))
    assert store.announce("p1", H1, now=1.0, avoid={"1234"}) == "5678"


def test_codes_survive_restart_and_file_is_private(tmp_path):
    code = ProposalStore(tmp_path / "p.json").announce("p1", H1, now=1.0, avoid=set())
    assert ProposalStore(tmp_path / "p.json").peek(code, now=2.0) == {"proposal_id": "p1", "hash": H1}
    assert stat.S_IMODE(os.stat(tmp_path / "p.json").st_mode) == 0o600


def test_record_approval_appends_atomically_and_privately(tmp_path):
    path = tmp_path / "state" / "approved_proposals.json"
    record_approval(path, "p1", H1, now=1_800_000_000.0)
    record_approval(path, "p2", H2, now=1_800_000_001.0)
    rows = json.loads(path.read_text())
    assert [(r["proposal_id"], r["hash"]) for r in rows] == [("p1", H1), ("p2", H2)]
    assert all(set(r) == {"proposal_id", "hash", "approved_at"} for r in rows)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [f.name for f in path.parent.iterdir()] == ["approved_proposals.json"]


def test_record_approval_twice_does_not_duplicate(tmp_path):
    path = tmp_path / "a.json"
    record_approval(path, "p1", H1, now=1.0)
    record_approval(path, "p1", H1, now=2.0)
    assert len(json.loads(path.read_text())) == 1


def test_record_drop_uses_dropped_at(tmp_path):
    path = tmp_path / "d.json"
    record_drop(path, "p1", H1, now=1.0)
    (row,) = json.loads(path.read_text())
    assert set(row) == {"proposal_id", "hash", "dropped_at"}


def test_corrupt_record_file_is_not_overwritten(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        record_approval(path, "p1", H1, now=1.0)
    assert path.read_text() == "{not json"
