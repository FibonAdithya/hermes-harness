import json
import os
import stat
import threading
import time

import pytest

from hermes_broker.approvals import _dispatch
from hermes_broker.deploy_ask import STATE_FILE, DeployAskState, DeployRunner, format_deploy_ask
from hermes_broker.grants import GrantStore

OWNER = 111222333
SHA = "4774e2314f46" + "0" * 28
SHA2 = "5555e2314f46" + "0" * 28


def owner_update(text, date):
    return {"update_id": 1, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER},
                                        "text": text, "date": date}}


def test_twenty_commits_show_fifteen_and_a_count():
    lines = [f"{i:07x} subject {i}" for i in range(20)]
    text = format_deploy_ask("b" * 40, "a" * 40, 0.0, lines, ancestor=True)
    shown = [l for l in text.splitlines() if l.startswith("  ") and "more" not in l]
    assert len(shown) == 15 and "  and 5 more" in text.splitlines() and "20 commits:" in text


@pytest.mark.parametrize("n, more", [(15, None), (16, "  and 1 more")])
def test_more_line_boundary(n, more):
    lines = [f"{i:07x} subject {i}" for i in range(n)]
    out = format_deploy_ask("b" * 40, "a" * 40, 0.0, lines, ancestor=True).splitlines()
    assert [l for l in out if "more" in l] == ([more] if more else [])


def test_long_subject_is_cut_to_100():
    text = format_deploy_ask("b" * 40, "a" * 40, 0.0, ["abcdef0 " + "x" * 150], ancestor=True)
    (line,) = [l for l in text.splitlines() if l.startswith("  abcdef0")]
    assert len(line) == 2 + 100


def test_exact_layout():
    text = format_deploy_ask("195a042c401f" + "0" * 28, "4774e2314f46" + "0" * 28,
                             1763034240.0, ["4774e23 Owner todo list (#55)"], ancestor=True)
    assert text == ("Box is behind master.\nbox:    195a042c401f\n"
                    "master: 4774e2314f46 (merged 11:44 UTC)\n1 commit:\n  4774e23 Owner todo list (#55)\n"
                    "Read them, then reply: deploy 4774e2314f46\nValid 24 h. Ignore it to skip.")


def test_null_box_and_not_ancestor_lines():
    commits = ["4774e23 Owner todo list (#55)"]
    master = "4774e2314f46" + "0" * 28
    null = format_deploy_ask(None, master, 1763034240.0, commits, ancestor=True)
    assert null == ("Box is behind master.\nbox:    unknown (no deploy stamp)\n"
                    "master: 4774e2314f46 (merged 11:44 UTC)\n"
                    "Read them, then reply: deploy 4774e2314f46\nValid 24 h. Ignore it to skip.")
    assert "commit" not in null.replace("Box is behind", "")
    box = "195a042c401f" + "0" * 28
    orphan = format_deploy_ask(box, master, 1763034240.0, commits, ancestor=False)
    assert orphan == ("Box is behind master.\nbox:    195a042c401f (not an ancestor of master)\n"
                      "master: 4774e2314f46 (merged 11:44 UTC)\n"
                      "Read them, then reply: deploy 4774e2314f46\nValid 24 h. Ignore it to skip.")
    assert "commit" not in orphan


def test_state_survives_a_new_instance(tmp_path):
    DeployAskState(tmp_path / STATE_FILE).record_asked("a" * 40)
    s = DeployAskState(tmp_path / STATE_FILE)
    assert s.asked_sha() == "a" * 40
    s.set_running("b" * 40, 5.0)
    assert DeployAskState(tmp_path / STATE_FILE).deploy_running() == {"sha": "b" * 40, "started_at": 5.0}
    s.clear_running()
    assert s.deploy_running() is None and s.asked_sha() == "a" * 40
    s.set_running("b" * 40, 6.0)
    s.clear_asked()
    assert s.asked_sha() is None and s.deploy_running() == {"sha": "b" * 40, "started_at": 6.0}


def test_state_file_shape_and_mode(tmp_path):
    path = tmp_path / STATE_FILE
    s = DeployAskState(path)
    s.record_asked("a" * 40)
    s.set_running("b" * 40, 5.0)
    assert json.loads(path.read_text()) == {
        "asked_sha": "a" * 40, "deploy_running": {"sha": "b" * 40, "started_at": 5.0}}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    s.clear_asked()
    s.clear_running()
    assert json.loads(path.read_text()) == {}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "null", ""])
def test_corrupt_state_reads_as_empty(tmp_path, content):
    (tmp_path / STATE_FILE).write_text(content)
    s = DeployAskState(tmp_path / STATE_FILE)
    assert s.asked_sha() is None and s.deploy_running() is None
    s.record_asked("a" * 40)
    assert s.asked_sha() == "a" * 40


def test_missing_file_reads_as_empty(tmp_path):
    s = DeployAskState(tmp_path / "sub" / STATE_FILE)
    assert s.asked_sha() is None and s.deploy_running() is None
    s.clear_running()
    s.record_asked("c" * 40)
    assert s.asked_sha() == "c" * 40


def test_runner_reports_success_and_clears(tmp_path):
    state, sent, calls = DeployAskState(tmp_path / STATE_FILE), [], []

    def call(verb, args, timeout=60):
        calls.append((verb, args, timeout))
        return {"deployed": SHA}

    r = DeployRunner(call, lambda t: sent.append(t) or True, state, clock=lambda: 7.0, spawn=lambda fn: fn())
    reply = r.start(SHA)
    assert reply == f"Deploying {SHA[:12]} to tig-server. The result follows when it finishes."
    assert calls == [("deploy", {"sha": SHA, "target": "harness"}, 900)]
    assert sent == [f"tig-server: deployed {SHA[:12]}"] and state.deploy_running() is None and not r.busy()


def test_runner_records_running_before_the_call(tmp_path):
    state, seen = DeployAskState(tmp_path / STATE_FILE), []

    def call(verb, args, timeout=60):
        seen.append(state.deploy_running())
        return {"deployed": SHA}

    DeployRunner(call, lambda t: True, state, clock=lambda: 7.0, spawn=lambda fn: fn()).start(SHA)
    assert seen == [{"sha": SHA, "started_at": 7.0}]


@pytest.mark.parametrize("behaviour", ["error", "raise"])
def test_runner_failure_is_reported_and_frees_the_runner(tmp_path, behaviour):
    state, sent = DeployAskState(tmp_path / STATE_FILE), []

    def call(verb, args, timeout=60):
        if behaviour == "raise":
            raise RuntimeError("boom")
        return {"error": "tig-server unreachable: timed out"}

    r = DeployRunner(call, lambda t: sent.append(t) or True, state, spawn=lambda fn: fn())
    r.start(SHA)
    tail = "boom" if behaviour == "raise" else "tig-server unreachable: timed out"
    assert sent == [f"tig-server: deploy of {SHA[:12]} failed: {tail}"]
    assert state.deploy_running() is None and r.busy() is False


def test_box_refusal_text_is_relayed(tmp_path):
    err = "master is at 5555, not the approved 4774; request a new deploy"
    sent = []
    r = DeployRunner(lambda v, a, t=60: {"error": err}, lambda t: sent.append(t) or True,
                     DeployAskState(tmp_path / STATE_FILE), spawn=lambda fn: fn())
    r.start(SHA)
    assert sent[0].endswith(err)


def test_send_failure_still_frees_the_runner(tmp_path):
    state = DeployAskState(tmp_path / STATE_FILE)

    def send(text):
        raise OSError("telegram down")

    r = DeployRunner(lambda v, a, t=60: {"deployed": SHA}, send, state, spawn=lambda fn: fn())
    r.start(SHA)
    assert state.deploy_running() is None and not r.busy()


def test_second_start_while_busy_is_refused(tmp_path):
    state, spawned = DeployAskState(tmp_path / STATE_FILE), []
    r = DeployRunner(lambda v, a, t=60: {}, lambda t: True, state, spawn=spawned.append)
    r.start(SHA)
    assert r.busy() and r.start(SHA2) == "a deploy is already running" and len(spawned) == 1


def test_spawn_failure_clears_and_releases(tmp_path):
    state = DeployAskState(tmp_path / STATE_FILE)

    def spawn(fn):
        raise RuntimeError("no threads")

    r = DeployRunner(lambda v, a, t=60: {}, lambda t: True, state, spawn=spawn)
    with pytest.raises(RuntimeError):
        r.start(SHA)
    assert state.deploy_running() is None and not r.busy()


def test_deploy_does_not_block_other_updates(tmp_path):
    store = GrantStore(tmp_path / "grants.json")
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    release, entered, sent = threading.Event(), threading.Event(), []

    def call(verb, args, timeout=60):
        entered.set()
        assert release.wait(10)
        return {"deployed": SHA}

    runner = DeployRunner(call, lambda t: sent.append(t) or True, DeployAskState(tmp_path / STATE_FILE))
    first = _dispatch(owner_update("deploy 4774e23", date=10), OWNER, store, None, None, (), lambda: 10.0,
                      runner=runner)
    assert entered.wait(5) and "Deploying" in first
    store.create_deploy_request(SHA2, now=11.0, ttl_seconds=86400, origin="auto")
    second = _dispatch(owner_update("deploy " + SHA2[:7], date=12), OWNER, store, None, None, (), lambda: 12.0,
                       runner=runner)
    revoke = _dispatch(owner_update("revoke", date=13), OWNER, store, None, None, (), lambda: 13.0,
                       runner=runner)
    assert second == "a deploy is already running" and revoke == "Revoked. All boxes locked."
    assert sent == []
    release.set()
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sent == [f"tig-server: deployed {SHA[:12]}"]
