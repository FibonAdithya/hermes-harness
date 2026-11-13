import json
import logging
import os
import stat
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_broker.approvals import _dispatch
from hermes_broker.deploy_ask import (STATE_FILE, DeployAnnouncer, DeployAskState, DeployRunner,
                                      format_deploy_ask)
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


# ---- DeployAnnouncer -------------------------------------------------

T0 = 1_763_000_000
GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(*args, offset=None):
    """The fixture's own git calls; never recorded, never through the announcer's `run`."""
    env = {**os.environ, **GIT_ENV}
    if offset is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{T0 + offset} +0000"
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=master", *args],
                          check=True, capture_output=True, text=True, env=env).stdout.strip()


class FakeRunner:
    def __init__(self):
        self.is_busy = False

    def busy(self):
        return self.is_busy


class FakeBox:
    def __init__(self):
        self.replies, self.calls = {}, []

    def __call__(self, verb, args, timeout=60):
        self.calls.append((verb, args, timeout))
        return dict(self.replies.get(verb, {}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    # The announcer's real git calls must not read the user's git config either.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin, up, checkout = tmp_path / "origin.git", tmp_path / "up", tmp_path / "checkout"
    _git("init", "-q", "--bare", str(origin))
    _git("clone", "-q", str(origin), str(up))
    e = SimpleNamespace(checkout=checkout, sent=[], send_attempts=[], send_results=None, argvs=[], kwargs=[],
                        runner=FakeRunner(), box=FakeBox(), fail=None, offset=100)

    def commit(files, offset, moves=()):
        for src, dst in moves:
            (up / dst).parent.mkdir(parents=True, exist_ok=True)
            _git("-C", str(up), "mv", src, dst)
        for name, text in files.items():
            (up / name).parent.mkdir(parents=True, exist_ok=True)
            (up / name).write_text(text)
        _git("-C", str(up), "add", *files)
        _git("-C", str(up), "commit", "-q", "--allow-empty", "-m", f"change {offset}", offset=offset)
        return _git("-C", str(up), "rev-parse", "HEAD")

    def fetch():
        _git("-C", str(checkout), "fetch", "-q")

    def add_master_commit(files, moves=()):
        """Commit on master `files` ({path: text}) and `moves` ((src, dst) for `git mv`); it becomes
        env.master with commit time env.ct."""
        e.offset += 100
        e.master = commit(files, e.offset, moves)
        e.ct = T0 + e.offset
        _git("-C", str(up), "push", "-q", "origin", "master")
        fetch()
        return e.master

    def add_side_commit(files):
        """A commit on a branch `side` off `first`, pushed to origin and fetched, not merged."""
        e.offset += 100
        _git("-C", str(up), "checkout", "-q", "-b", "side", e.first)
        sha = commit(files, e.offset)
        _git("-C", str(up), "push", "-q", "origin", "side")
        _git("-C", str(up), "checkout", "-q", "master")
        fetch()
        return sha

    e.first = commit({"app.py": "v1\n"}, 0)
    e.master = commit({"app.py": "v2\n"}, 100)
    e.ct = T0 + 100
    _git("-C", str(up), "push", "-q", "origin", "master")
    _git("clone", "-q", str(origin), str(checkout))
    fetch()
    e.add_master_commit, e.add_side_commit = add_master_commit, add_side_commit

    e.grants_path = tmp_path / "grants.json"
    e.grants = GrantStore(e.grants_path)
    e.state = DeployAskState(tmp_path / STATE_FILE)
    e.grants_json = lambda: json.loads(e.grants_path.read_text()) if e.grants_path.exists() else {}

    def box_reply(reply):
        e.box.replies["deployed"] = reply

    e.box_reply = box_reply
    e.box_at = lambda sha: box_reply({"harness": sha, "fleet": None})

    def send(text):
        e.send_attempts.append(text)
        ok = True if e.send_results is None else e.send_results[len(e.send_attempts) - 1]
        if ok:
            e.sent.append(text)
        return ok

    def run(argv, **kwargs):
        e.argvs.append(list(argv))
        e.kwargs.append(kwargs)
        if e.fail is not None and argv[3] == e.fail[0]:
            sub, failure = e.fail
            e.fail = None
            if failure == "rc1":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal: boom")
            if failure == "garbage":
                return subprocess.CompletedProcess(argv, 0, stdout="not-a-time\n", stderr="")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 30)
            raise OSError("no git")
        return subprocess.run(argv, **kwargs)

    def fail_git(sub, failure):
        e.fail = (sub, failure)

    e.fail_git = fail_git
    e.new_announcer = lambda: DeployAnnouncer(GrantStore(e.grants_path), DeployAskState(tmp_path / STATE_FILE),
                                              e.runner, e.box, send, checkout, run=run)
    e.ann = e.new_announcer()

    def make_blocker(kind, now):
        if kind == "agent_harness":
            e.grants.create_deploy_request(SHA, now, "harness")
        elif kind == "agent_fleet":
            e.grants.create_deploy_request(SHA, now, "fleet")
        else:
            e.grants.create_deploy_request(SHA, now - 1, "harness")
            assert e.grants.approve_deploy(SHA[:7], now, "harness") == SHA

    e.make_blocker = make_blocker
    return e


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "hermes_broker.deploy_ask"]


def test_up_to_date_sends_nothing(env):
    env.box_at(env.master)
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and env.send_attempts == [] and "pending_deploy" not in env.grants_json()
    assert [a[3] for a in env.argvs] == ["rev-parse"]  # returns before the settle and diff checks


def test_settle_time(env):
    env.box_at(env.first)
    env.ann.tick(env.ct + 1799)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()
    env.ann.tick(env.ct + 1800)
    assert len(env.sent) == 1
    assert env.grants.pending_deploy_origin(env.ct + 1800) == "auto"
    req = env.grants_json()["pending_deploy"]
    assert req["sha"] == env.master and req["expires_at"] == env.ct + 1800 + 86400


def test_ask_text_lists_the_commit(env):
    env.box_at(env.first)
    env.ann.tick(env.ct + 3600)
    (text,) = env.sent
    short = _git("-C", str(env.checkout), "log", "-1", "--format=%h", env.master)
    assert text == format_deploy_ask(env.first, env.master, env.ct, [f"{short} change 100"], ancestor=True)
    assert env.state.asked_sha() == env.master


def test_once_per_commit_across_restart(env):
    env.box_at(env.first)
    env.ann.tick(env.ct + 3600)
    env.ann.tick(env.ct + 3900)
    env.new_announcer().tick(env.ct + 4200)
    assert len(env.sent) == 1 and len(env.send_attempts) == 1


def test_failed_send_is_retried(env):
    env.box_at(env.first)
    env.send_results = [False, True, True]
    env.ann.tick(env.ct + 3600)
    assert env.state.asked_sha() is None
    for t in (3900, 4200):
        env.ann.tick(env.ct + t)
    assert len(env.send_attempts) == 2 and env.state.asked_sha() == env.master


def test_docs_only_is_skipped(env):
    box = env.master
    env.box_at(box)
    env.add_master_commit({"docs/a.md": "a\n", "docs/b/c.md": "c\n"})
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and "pending_deploy" not in env.grants_json() and env.state.asked_sha() is None
    env.add_master_commit({"README.md": "r\n", "docs/a.md": "a2\n"})
    env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1 and "2 commits:" in env.sent[0]


def test_move_out_of_code_into_docs_is_asked(env):
    """`git mv app.py docs/app.py` deletes a file the box runs; rename detection would list only
    docs/app.py and read it as docs-only."""
    env.box_at(env.master)
    env.add_master_commit({}, moves=[("app.py", "docs/app.py")])
    env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1 and "1 commit:" in env.sent[0]
    (diff,) = [a for a in env.argvs if a[3] == "diff"]
    assert "--no-renames" in diff


def test_empty_diff_counts_as_docs_only(env):
    env.box_at(env.master)
    env.add_master_commit({})  # an empty commit: nothing outside docs/ changed
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()


def test_not_an_ancestor(env):
    side = env.add_side_commit({"side.py": "s\n"})
    env.box_at(side)
    env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1
    assert f"box:    {side[:12]} (not an ancestor of master)" in env.sent[0]
    assert "commit" not in env.sent[0]
    assert not any(a[3] == "diff" for a in env.argvs)


def test_box_sha_unknown_to_checkout(env):
    env.box_at("e" * 40)
    env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1 and "(not an ancestor of master)" in env.sent[0]
    assert env.grants.pending_deploy_origin(env.ct + 3600) == "auto"


def test_deployed_failure_warns_once_an_hour(env, caplog):
    caplog.set_level(logging.WARNING, logger="hermes_broker.deploy_ask")
    env.box_reply({"error": "tig-server unreachable: timed out"})
    for i in range(20):
        env.ann.tick(env.ct + 3600 + 60 * i)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()
    assert len(_warnings(caplog)) == 1
    assert "tig-server unreachable: timed out" in _warnings(caplog)[0].getMessage()
    env.ann.tick(env.ct + 3600 + 3600)
    assert len(_warnings(caplog)) == 2


@pytest.mark.parametrize("sub, failure", [("rev-parse", "rc1"), ("log", "rc1"), ("diff", "rc1"),
                                          ("log", "timeout"), ("merge-base", "timeout"),
                                          ("log", "garbage"), ("rev-parse", "oserror"),
                                          ("diff", "timeout")])
def test_git_failure_warns_and_skips(env, caplog, sub, failure):
    caplog.set_level(logging.WARNING, logger="hermes_broker.deploy_ask")
    env.box_at(env.first)
    env.fail_git(sub, failure)
    env.ann.tick(env.ct + 3600)
    assert env.fail is None  # the failure was injected
    assert env.sent == [] and "pending_deploy" not in env.grants_json() and env.state.asked_sha() is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    env.ann.tick(env.ct + 3660)  # the next tick works again
    assert len(env.sent) == 1


def test_commit_list_failure_warns_and_skips(env, caplog):
    """The second `log` (the commit list) failing is a git failure too."""
    caplog.set_level(logging.WARNING, logger="hermes_broker.deploy_ask")
    env.box_at(env.first)
    calls = {"log": 0}
    inner = env.ann._run

    def run(argv, **kwargs):
        if argv[3] == "log":
            calls["log"] += 1
            if calls["log"] == 2:
                env.argvs.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal")
        return inner(argv, **kwargs)

    ann = DeployAnnouncer(env.grants, env.state, env.runner, env.box,
                          lambda t: env.sent.append(t) or True, env.checkout, run=run)
    ann.tick(env.ct + 3600)
    assert calls["log"] == 2 and env.sent == [] and "pending_deploy" not in env.grants_json()
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("reply", [{}, {"harness": "195a042"}, {"harness": 5}, {"harness": "g" * 40},
                                   {"fleet": None}])
def test_malformed_deployed_reply_is_a_failure(env, caplog, reply):
    caplog.set_level(logging.WARNING, logger="hermes_broker.deploy_ask")
    env.box_reply(reply)
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()
    (warning,) = _warnings(caplog)
    assert "malformed reply" in warning.getMessage() and repr(reply) in warning.getMessage()


@pytest.mark.parametrize("blocker", ["agent_harness", "agent_fleet", "grant"])
def test_never_displaces_another_request(env, blocker):
    env.box_at(env.first)
    now = env.ct + 3600
    env.make_blocker(blocker, now)
    before = env.grants_path.read_bytes()
    env.ann.tick(now + 1)
    assert env.sent == [] and env.grants_path.read_bytes() == before and env.state.asked_sha() is None
    assert [a[3] for a in env.argvs] == ["rev-parse"]  # step 4 returns before the other git calls
    env.ann.tick(now + 700)  # past the 120 s request and the 600 s grant
    assert len(env.sent) == 1 and env.grants.pending_deploy_origin(now + 700) == "auto"


def test_request_landing_during_the_git_calls_is_not_displaced(env):
    """The re-check right before writing: an agent request arriving after step 4."""
    env.box_at(env.first)
    now = env.ct + 3600
    inner = env.ann._run

    def run(argv, **kwargs):
        if argv[3] == "merge-base":
            env.grants.create_deploy_request(SHA, now, "harness")
        return inner(argv, **kwargs)

    ann = DeployAnnouncer(env.grants, env.state, env.runner, env.box,
                          lambda t: env.sent.append(t) or True, env.checkout, run=run)
    ann.tick(now)
    assert env.sent == [] and env.grants.pending_deploy_origin(now) == "agent"
    assert env.grants_json()["pending_deploy"]["sha"] == SHA and env.state.asked_sha() is None


def test_deploy_running_blocks_the_ask(env):
    env.box_at(env.first)
    env.runner.is_busy = True
    env.state.set_running(env.master, env.ct + 3500)
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()
    assert env.state.deploy_running() == {"sha": env.master, "started_at": env.ct + 3500}


def test_null_box_stamp(env):
    env.box_reply({"harness": None, "fleet": None})
    env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1 and "box:    unknown (no deploy stamp)" in env.sent[0]
    assert "commit" not in env.sent[0].split("master:")[1].split("Read them")[0]
    assert not any(a[3] in ("diff", "merge-base") or (a[3] == "log" and any(".." in x for x in a))
                   for a in env.argvs)
    assert env.grants.pending_deploy_origin(env.ct + 3600) == "auto"


@pytest.mark.parametrize("stamp, expect", [("same", "tig-server: deployed "), ("other", "did not finish")])
def test_restart_recovery(env, stamp, expect):
    env.state.record_asked(env.master)
    env.state.set_running(env.master, env.ct)
    env.box_at(env.master if stamp == "same" else env.first)
    ann = env.new_announcer()
    ann.tick(env.ct + 60)  # inside the settle time: recovery only
    assert len(env.sent) == 1 and expect in env.sent[0] and env.state.deploy_running() is None
    if stamp == "same":
        assert env.sent[0] == f"tig-server: deployed {env.master[:12]}"
    else:
        assert env.sent[0] == (f"deploy of {env.master[:12]} did not finish; "
                               "the next ask will offer it again")
    ann.tick(env.ct + 3600)
    if stamp == "same":
        assert len(env.sent) == 1
    else:
        assert len(env.sent) == 2 and "Read them, then reply: deploy " + env.master[:12] in env.sent[1]


def test_recovery_deployed_failure_keeps_the_record(env, caplog):
    caplog.set_level(logging.WARNING, logger="hermes_broker.deploy_ask")
    env.state.set_running(env.master, env.ct)
    env.box_reply({"error": "tig-server unreachable: timed out"})
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and env.state.deploy_running() == {"sha": env.master, "started_at": env.ct}
    assert len(_warnings(caplog)) == 1
    assert not env.argvs  # returned before reading master


def test_recovery_skipped_while_this_process_is_deploying(env):
    env.state.record_asked(env.first)
    env.state.set_running(env.master, env.ct)
    env.runner.is_busy = True
    env.box_at(env.master)
    env.ann.tick(env.ct + 3600)
    assert env.sent == [] and env.state.deploy_running() == {"sha": env.master, "started_at": env.ct}
    assert env.state.asked_sha() == env.first


def test_only_read_only_git(env):
    env.box_at(env.first)  # behind, ancestor
    env.ann.tick(env.ct + 3600)
    env.box_reply({"harness": None, "fleet": None})  # null stamp
    env.ann.tick(env.ct + 3700)
    side = env.add_side_commit({"side.py": "s\n"})  # not an ancestor
    env.box_at(side)
    env.ann.tick(env.ct + 3800)
    box = env.master
    env.add_master_commit({"docs/a.md": "a\n"})  # docs-only
    env.box_at(box)
    env.ann.tick(env.ct + 3600)
    subs = {a[3] for a in env.argvs}
    assert subs == {"rev-parse", "log", "diff", "merge-base"}
    assert all(a[:3] == ["git", "-C", str(env.checkout)] for a in env.argvs)
    assert all(k == {"capture_output": True, "text": True, "timeout": 30, "check": False}
               for k in env.kwargs)
