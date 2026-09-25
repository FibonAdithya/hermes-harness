import subprocess

import pytest
from hermes_broker.grants import GrantStore
from hermes_broker.server import Locked


@pytest.fixture
def store(tmp_path):
    return GrantStore(tmp_path / "grants.json")


@pytest.fixture
def fakes(store, monkeypatch, tmp_path):
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))
    calls, box_reply, proc = [], {"deployed": True}, subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(server.box, "call",
                        lambda t, v, a, timeout=60: calls.append(("box", t, v, a, timeout)) or dict(box_reply))
    monkeypatch.setattr(server.subprocess, "run",
                        lambda argv, **kw: calls.append(("run", argv, kw.get("timeout"))) or proc)
    return server, calls, box_reply, proc


def _grant(store):
    import time
    code = store.create_request("tig-server", 30, "deploy", now=time.time())
    store.approve(code, now=time.time())


def test_deploy_harness_is_gated(fakes):
    server, calls, _, _ = fakes
    with pytest.raises(Locked):
        server.deploy_harness()
    assert calls == []


def test_deploy_harness_deploys_the_box_then_the_droplet(fakes, store, tmp_path):
    server, calls, _, _ = fakes
    _grant(store)
    out = server.deploy_harness()
    assert calls == [
        ("box", "tig-server", "deploy", {}, 600),
        ("run", [str(tmp_path / "hermes-harness" / "droplet" / "harness-pull.sh")], 600),
    ]
    assert out == "box: deployed\ndroplet: deployed"


def test_deploy_harness_reports_each_failure(fakes, store):
    server, calls, box_reply, proc = fakes
    _grant(store)
    box_reply["error"] = "deploy failed: install.sh exited 1"
    proc.returncode, proc.stderr = 1, "uv sync failed\n"
    out = server.deploy_harness()
    assert [c[0] for c in calls] == ["box", "run"]  # a box failure does not skip the droplet
    assert out == "deploy failed: install.sh exited 1\ndroplet deploy failed: uv sync failed"
