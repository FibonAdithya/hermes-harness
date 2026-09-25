"""deploy_harness: needs an owner-approved deploy of one commit; deploys the box only."""
import pytest
from hermes_broker.grants import GrantStore
from hermes_broker.server import Locked

SHA = "91a2e066c76837d9ac80243755ebc628993a0454"


@pytest.fixture
def store(tmp_path):
    return GrantStore(tmp_path / "grants.json")


@pytest.fixture
def fakes(store, monkeypatch):
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    calls, box_reply = [], {"deployed": SHA}
    monkeypatch.setattr(server.box, "call",
                        lambda t, v, a, timeout=60: calls.append(("box", t, v, a, timeout)) or dict(box_reply))
    monkeypatch.setattr(server.subprocess, "run",
                        lambda argv, **kw: calls.append(("run", argv)) or pytest.fail("ran a local command"))
    return server, calls, box_reply


def _approve(store, sha=SHA):
    store.create_deploy_request(sha, now=__import__("time").time())
    assert store.approve_deploy(sha[:7], now=__import__("time").time()) == sha


def test_deploy_harness_is_locked_without_a_deploy_grant(fakes, store):
    server, calls, _ = fakes
    with pytest.raises(Locked):
        server.deploy_harness()
    assert calls == []


def test_a_tig_server_grant_does_not_unlock_deploy(fakes, store):
    server, calls, _ = fakes
    import time
    code = store.create_request("tig-server", 30, "run a night", now=time.time())
    store.approve(code, now=time.time())
    with pytest.raises(Locked):
        server.deploy_harness()
    assert calls == []


def test_deploy_harness_deploys_the_approved_commit_to_the_box_only(fakes, store):
    server, calls, _ = fakes
    _approve(store)
    out = server.deploy_harness()
    assert calls == [("box", "tig-server", "deploy", {"sha": SHA}, 900)]
    assert out.startswith(f"tig-server: deployed {SHA[:12]}")


def test_deploy_grant_is_single_use(fakes, store):
    server, calls, _ = fakes
    _approve(store)
    server.deploy_harness()
    with pytest.raises(Locked):
        server.deploy_harness()
    assert len(calls) == 1


def test_deploy_harness_reports_the_box_refusal(fakes, store):
    server, _, box_reply = fakes
    _approve(store)
    box_reply["error"] = "master is at 35b4e9e00000, not the approved 91a2e066c768; request a new deploy"
    assert server.deploy_harness() == box_reply["error"]


def test_request_deploy_needs_a_full_sha(fakes, store):
    server, _, _ = fakes
    for bad in ("", "91a2e06", "master", SHA + "0", "g" * 40):
        assert server.request_deploy(bad).startswith("sha must be")
    assert store.approve_deploy("0000000", now=__import__("time").time()) is None


def test_request_deploy_tells_the_owner_what_to_type(fakes, store):
    server, _, _ = fakes
    out = server.request_deploy(" " + SHA.upper() + " ")
    assert f"`deploy {SHA[:12]}`" in out and SHA in out
    assert store.approve_deploy(SHA[:12], now=__import__("time").time()) == SHA
