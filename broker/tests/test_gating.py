import time

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


def test_run_talos_resume_sends_only_the_job_and_backend(store, monkeypatch):
    """A resume carries no challenge, direction or budget: Talos reads those from
    the job, and the box refuses them alongside resume."""
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    sent = []
    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: sent.append((v, a)) or {"id": "talos-x"})

    with pytest.raises(Locked):
        server.run_talos(resume="20260924-230101-knapsack")
    assert sent == []

    store.approve(store.create_request("tig-server", 30, "x", now=time.time()), now=time.time())
    out = server.run_talos(resume="20260924-230101-knapsack", backend="modal")
    assert sent == [("run_talos", {"resume": "20260924-230101-knapsack", "backend": "modal"})]
    assert "talos-x" in out and "20260924-230101-knapsack" in out

    sent.clear()
    server.run_talos("knapsack", "d", 3, "local")
    assert sent == [("run_talos", {"challenge": "knapsack", "direction": "d", "iterations": 3, "backend": "local",
                                   "compute_usd": 5.0, "mode": "single-shot"})]


def test_run_talos_refuses_new_run_arguments_alongside_resume(store, monkeypatch):
    """The box refuses these with resume; the broker must not drop them first and
    report a resume that ignored what the caller asked for."""
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    sent = []
    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: sent.append((v, a)) or {"id": "talos-x"})
    store.approve(store.create_request("tig-server", 30, "x", now=time.time()), now=time.time())

    job = "20260924-230101-knapsack"
    for extra in ({"challenge": "hypergraph"}, {"direction": "d"}, {"iterations": 30}, {"iterations": 0},
                  {"compute_usd": 10}, {"mode": "agentic"}):
        out = server.run_talos(resume=job, **extra)
        assert "resume" in out and next(iter(extra)) in out, (extra, out)
    assert sent == []

    server.run_talos("knapsack", "d")
    assert sent == [("run_talos", {"challenge": "knapsack", "direction": "d", "iterations": 30, "backend": "local",
                                   "compute_usd": 5.0, "mode": "single-shot"})]


def test_run_talos_c3_forwards_the_compute_cap(store, monkeypatch):
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    sent = []
    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: sent.append((v, a)) or {"id": "talos-x"})

    with pytest.raises(Locked):
        server.run_talos("knapsack", "d", 3, "c3", compute_usd=40)
    assert sent == []

    store.approve(store.create_request("tig-server", 30, "x", now=time.time()), now=time.time())
    out = server.run_talos("knapsack", "d", 3, "c3", compute_usd=40)
    assert sent == [("run_talos", {"challenge": "knapsack", "direction": "d", "iterations": 3, "backend": "c3",
                                   "compute_usd": 40.0, "mode": "single-shot"})]
    assert "talos-x" in out and "c3" in out and "40" in out


def test_pause_talos_is_ungated_and_forwards_the_night(store, monkeypatch):
    """Stopping only lowers spend, so it needs no grant; resuming still does."""
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    sent = []
    monkeypatch.setattr(server.box, "call",
                        lambda t, v, a, timeout=60: sent.append((v, a)) or {"id": a["id"]})
    out = server.pause_talos("talos-20260926-0100-abcd")
    assert sent == [("pause_talos", {"id": "talos-20260926-0100-abcd"})]
    assert "talos-20260926-0100-abcd" in out and "resume" in out

    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: {"error": "not running"})
    assert server.pause_talos("talos-20260926-0100-abcd") == "not running"


def test_run_talos_forwards_agentic_mode(store, monkeypatch):
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    sent = []
    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: sent.append((v, a)) or {"id": "talos-x"})
    with pytest.raises(Locked):
        server.run_talos("knapsack", "d", 3, "c3", mode="agentic")
    store.approve(store.create_request("tig-server", 30, "x", now=time.time()), now=time.time())
    out = server.run_talos("knapsack", "d", 3, "c3", mode="agentic")
    assert sent[0][1]["mode"] == "agentic"
    assert "agentic" in out
