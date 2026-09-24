import pytest
from hermes_broker.grants import GrantStore
from hermes_broker.server import Locked, require_grant


@pytest.fixture
def store(tmp_path):
    return GrantStore(tmp_path / "grants.json")


def test_locked_without_a_grant(store):
    with pytest.raises(Locked) as exc:
        require_grant(store, "tig-server", now=1000.0)
    assert "LOCKED" in str(exc.value)
    assert "tig-server" in str(exc.value)


def test_unlocked_with_a_live_grant(store):
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    store.approve(code, now=1000.0)
    require_grant(store, "tig-server", now=1001.0)


def test_grant_for_one_box_does_not_unlock_another(store):
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    store.approve(code, now=1000.0)
    with pytest.raises(Locked):
        require_grant(store, "tig-gpu", now=1001.0)


def test_expired_grant_locks_again(store):
    code = store.create_request("tig-gpu", 1, "quick job", now=1000.0)
    store.approve(code, now=1000.0)
    require_grant(store, "tig-gpu", now=1030.0)
    with pytest.raises(Locked):
        require_grant(store, "tig-gpu", now=1061.0)


def test_agent_cannot_self_approve(store):
    """Nothing the agent calls may issue a grant; only the approvals bot does."""
    code = store.create_request("tig-gpu", 30, "x", now=1000.0)
    with pytest.raises(Locked):
        require_grant(store, "tig-gpu", now=1001.0)
    assert store.approve(code, now=1001.0) == "tig-gpu"


def test_box_tools_are_gated_and_reads_are_not(store, monkeypatch):
    """Every box tool that starts something needs a grant; every read does not."""
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    calls = []
    monkeypatch.setattr(
        server.box, "call",
        lambda t, v, a, timeout=60: calls.append(v) or {"id": "x", "nights": [], "lines": [], "repos": []},
    )

    with pytest.raises(Locked):
        server.run_fleet("fleet-fixture", 8)
    with pytest.raises(Locked):
        server.run_talos("knapsack", "d", 3, "local")
    with pytest.raises(Locked):
        server.add_repo("fleet")
    with pytest.raises(Locked):
        server.run_task("o/r", "p", 5)
    assert calls == []

    server.night_status()
    server.night_log("fleet-20260924-2300-abcd", 10)
    server.list_repos()
    assert calls == ["status", "log", "list_repos"]
