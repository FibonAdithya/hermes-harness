"""After a grant, the approvals bot tells Hermes, so Hermes continues unasked.

The tests inject `run` (and `environ`) instead of patching subprocess: nothing
here may reach a real Hermes install.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

OWNER = 111222333
HERMES_DIR = Path("/opt/hermes-agent")
HERMES_BIN = Path("/opt/bin/hermes")
SHA = "91a2e066c76837d9ac80243755ebc628993a0454"
TOLD = "\nHermes is being told and should continue shortly."
NOW = 2_000_000_000.0
OLD = 7200  # seconds: well past the age at which a prod task is archived
NOT_TOLD = "\nCould not tell Hermes. Message it yourself to continue."


def step_of(cmd):
    """Which prod step a recorded command is: create, or the kanban subcommand."""
    return "create" if cmd[1] == "-c" else cmd[2]


class FakeRun:
    """Stands in for subprocess.run. Records every call; `fail` names steps that
    exit 1, `raises` maps a step to the exception it throws."""

    def __init__(self, create_out="t_abc123\n", listed="[]", fail=(), raises=None):
        self.calls = []
        self.create_out = create_out
        self.listed = listed
        self.fail = set(fail)
        self.raises = raises or {}

    def __call__(self, cmd, **kwargs):
        cmd = [str(part) for part in cmd]
        self.calls.append((cmd, kwargs))
        step = step_of(cmd)
        if step in self.raises:
            raise self.raises[step]
        out = {"create": self.create_out, "list": self.listed}.get(step, "")
        return subprocess.CompletedProcess(cmd, 1 if step in self.fail else 0, out, "")

    @property
    def steps(self):
        return [step_of(cmd) for cmd, _ in self.calls]

    def cmd(self, step):
        return next(cmd for cmd, _ in self.calls if step_of(cmd) == step)


def make(run, environ=None, clock=lambda: NOW):
    from hermes_broker.prod import make_prod
    return make_prod(OWNER, hermes_dir=HERMES_DIR, hermes_bin=HERMES_BIN, run=run,
                     environ={} if environ is None else environ, clock=clock)


def row(task_id, age=OLD):
    """One task as `hermes kanban list --json` reports it, created `age` seconds ago."""
    return {"id": task_id, "created_at": int(NOW - age)}


def listing(*rows):
    return json.dumps(list(rows))


class Recorder:
    """A prod that records the titles it was given."""

    def __init__(self, result=True):
        self.titles = []
        self.result = result

    def __call__(self, title):
        self.titles.append(title)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def store_at(tmp_path):
    from hermes_broker.grants import GrantStore
    return GrantStore(tmp_path / "grants.json")


# ---- handle: when the bot prods, and what it says -------------------------


def test_approval_tells_hermes_the_box_and_minutes(tmp_path):
    from hermes_broker.approvals import handle
    store = store_at(tmp_path)
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    prod = Recorder()
    reply = handle(store, "approve", code, now=1001.0, prod=prod)
    assert prod.titles == ["Owner approved tig-server for 30 min. Continue what you requested it for."]
    assert reply == "Granted tig-server for 30 min.\nFor: fix parser" + TOLD


def test_rejected_approval_tells_hermes_nothing(tmp_path):
    from hermes_broker.approvals import handle
    store = store_at(tmp_path)
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    prod = Recorder()
    wrong = "0000" if code != "0000" else "0001"
    assert handle(store, "approve", wrong, now=1001.0, prod=prod) == "No pending request with that code."
    assert prod.titles == []


def test_expired_approval_tells_hermes_nothing(tmp_path):
    from hermes_broker.approvals import handle
    store = store_at(tmp_path)
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    prod = Recorder()
    assert handle(store, "approve", code, now=1000.0 + 121, prod=prod) == "No pending request with that code."
    assert prod.titles == []


@pytest.mark.parametrize("target,tool", [("harness", "deploy_harness"), ("fleet", "deploy_fleet")])
def test_deploy_approval_tells_hermes_which_tool_to_call(tmp_path, target, tool):
    from hermes_broker.approvals import handle
    store = store_at(tmp_path)
    store.create_deploy_request(SHA, now=1000.0, target=target)
    prod = Recorder()
    reply = handle(store, "deploy", f"{target}:91a2e06", now=1001.0, prod=prod)
    assert prod.titles == [f"Owner approved deploying {target} {SHA[:12]}. Call {tool} now."]
    assert reply.endswith("approved for 10 min." + TOLD)


def test_rejected_deploy_tells_hermes_nothing(tmp_path):
    from hermes_broker.approvals import handle
    store = store_at(tmp_path)
    store.create_deploy_request(SHA, now=1000.0)
    prod = Recorder()
    assert handle(store, "deploy", "0000000", now=1001.0, prod=prod).startswith("No pending harness deploy")
    assert prod.titles == []


def test_revoke_tells_hermes_nothing(tmp_path):
    from hermes_broker.approvals import handle
    prod = Recorder()
    assert handle(store_at(tmp_path), "revoke", "", now=1001.0, prod=prod) == "Revoked. All boxes locked."
    assert prod.titles == []


@pytest.mark.parametrize("result", [False, RuntimeError("kanban is down")])
def test_grant_stands_and_reply_says_so_when_hermes_could_not_be_told(tmp_path, result):
    from hermes_broker.approvals import handle
    store = store_at(tmp_path)
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    reply = handle(store, "approve", code, now=1001.0, prod=Recorder(result))
    assert reply == "Granted tig-server for 30 min.\nFor: fix parser" + NOT_TOLD
    assert store.is_active("tig-server", now=1002.0)


def test_titles_fit_in_the_part_of_a_title_hermes_shows(tmp_path):
    """The gateway's wake message shows the first 120 characters of a title."""
    from hermes_broker.approvals import handle
    from hermes_broker.grants import BOXES, MAX_GRANT_MINUTES
    from hermes_broker.prod import TITLE_CHARS
    store = store_at(tmp_path)
    prod = Recorder()
    for box in BOXES:
        code = store.create_request(box, MAX_GRANT_MINUTES, "x", now=1000.0)
        handle(store, "approve", code, now=1001.0, prod=prod)
    for target in ("harness", "fleet"):
        store.create_deploy_request(SHA, now=1000.0, target=target)
        handle(store, "deploy", f"{target}:{SHA}", now=1001.0, prod=prod)
    assert len(prod.titles) == len(BOXES) + 2
    assert max(len(title) for title in prod.titles) <= TITLE_CHARS


def test_dispatch_hands_the_prod_to_grant_replies(tmp_path):
    from hermes_broker.approvals import _dispatch
    store = store_at(tmp_path)
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    update = {"update_id": 9, "message": {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                          "text": f"approve {code}", "date": 1060}}
    prod = Recorder()
    reply = _dispatch(update, OWNER, store, None, None, (), now_fn=lambda: 1200.0, prod=prod)
    assert len(prod.titles) == 1
    assert reply.endswith(TOLD)


# ---- make_prod: the commands it runs --------------------------------------


def test_prod_creates_then_subscribes_the_owner_dm_then_completes():
    from hermes_broker.prod import SESSION
    run = FakeRun()
    assert make(run)("Owner approved tig-server for 30 min.") is True
    assert run.steps[:3] == ["create", "notify-subscribe", "complete"]

    create, create_kwargs = run.calls[0]
    assert create[0] == str(HERMES_DIR / "venv" / "bin" / "python")
    assert create[3:] == ["Owner approved tig-server for 30 min.", SESSION]
    assert Path(create_kwargs["cwd"]) == HERMES_DIR

    assert run.cmd("notify-subscribe") == [
        str(HERMES_BIN), "kanban", "notify-subscribe", "t_abc123",
        "--platform", "telegram", "--chat-id", str(OWNER), "--chat-type", "dm"]
    assert run.cmd("complete")[:4] == [str(HERMES_BIN), "kanban", "complete", "t_abc123"]


def test_prod_uses_the_last_line_the_create_script_prints():
    run = FakeRun(create_out="some warning from an import\nt_77aa\n")
    assert make(run)("x") is True
    assert run.cmd("complete")[3] == "t_77aa"


@pytest.mark.parametrize("create_out", ["", "\n", "Traceback (most recent call last):\n  boom\n", "t_ab cd\n"])
def test_prod_fails_without_a_task_id_and_runs_nothing_more(create_out):
    run = FakeRun(create_out=create_out)
    assert make(run)("x") is False
    assert run.steps == ["create"]


def test_prod_fails_when_create_exits_nonzero_even_if_it_printed_an_id():
    run = FakeRun(fail={"create"})
    assert make(run)("x") is False
    assert run.steps == ["create"]


def test_prod_fails_when_subscribe_fails_and_does_not_complete():
    """Completing a task nobody is subscribed to wakes nobody."""
    run = FakeRun(fail={"notify-subscribe"})
    assert make(run)("x") is False
    assert "complete" not in run.steps


def test_prod_fails_when_complete_fails():
    run = FakeRun(fail={"complete"})
    assert make(run)("x") is False


@pytest.mark.parametrize("step", ["create", "notify-subscribe", "complete"])
@pytest.mark.parametrize("error", [FileNotFoundError("no hermes"),
                                   subprocess.TimeoutExpired("hermes", 30),
                                   UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid")])
def test_prod_fails_quietly_when_a_command_cannot_run(step, error):
    run = FakeRun(raises={step: error})
    assert make(run)("x") is False


def test_prod_archives_earlier_prod_tasks_but_not_the_one_it_just_made():
    """The new task is delivered by the gateway up to 5s later; it must stay, even
    if the listing were to report it as old."""
    from hermes_broker.prod import SESSION
    run = FakeRun(listed=listing(row("t_0a1b2c3d"), row("t_abc123"), row("t_4e5f6a7b")))
    assert make(run)("x") is True
    assert run.steps == ["create", "notify-subscribe", "complete", "list", "archive"]
    assert run.cmd("list") == [str(HERMES_BIN), "kanban", "list", "--session", SESSION, "--json"]
    assert run.cmd("archive") == [str(HERMES_BIN), "kanban", "archive", "t_0a1b2c3d", "t_4e5f6a7b"]


def test_prod_archives_nothing_when_only_the_new_task_is_listed():
    run = FakeRun(listed=listing(row("t_abc123")))
    assert make(run)("x") is True
    assert "archive" not in run.steps


@pytest.mark.parametrize("run", [
    FakeRun(fail={"list"}, listed=listing(row("t_0a1b2c3d"))),
    FakeRun(listed="not json"),
    FakeRun(listed=json.dumps(row("t_0a1b2c3d"))),
    FakeRun(listed=listing(row("t_0a1b2c3d")), fail={"archive"}),
    FakeRun(raises={"list": subprocess.TimeoutExpired("hermes", 30)}),
    FakeRun(raises={"list": UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid")}),
    FakeRun(listed=listing(row("t_0a1b2c3d")),
            raises={"archive": UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid")}),
])
def test_tidying_up_never_fails_a_prod_that_was_delivered(run):
    assert make(run)("x") is True
    assert "complete" in run.steps


def test_prod_archives_only_well_formed_ids():
    """A listing is data from another program: only task ids go on a command line."""
    run = FakeRun(listed=listing(row("--rm"), row("t_0a1b2c3d"), {"title": "no id", "created_at": 1}, "t_str", row(7)))
    assert make(run)("x") is True
    assert run.cmd("archive")[3:] == ["t_0a1b2c3d"]


def test_commands_do_not_inherit_the_brokers_python_environment():
    """The listener runs under `uv run`; Hermes must start in its own virtualenv."""
    run = FakeRun()
    base = {"HOME": "/home/hermes", "VIRTUAL_ENV": "/broker/.venv",
            "PYTHONPATH": "/broker", "PYTHONHOME": "/broker/py"}
    assert make(run, environ=base)("x") is True
    assert len(run.calls) >= 3
    for _, kwargs in run.calls:
        assert kwargs["env"] == {"HOME": "/home/hermes"}


def test_every_command_has_a_timeout():
    run = FakeRun(listed=listing(row("t_0a1b2c3d")))
    assert make(run)("x") is True
    assert len(run.calls) == 5
    for _, kwargs in run.calls:
        assert 0 < kwargs["timeout"] <= 60


class Board(FakeRun):
    """A stand-in kanban board that remembers its tasks: `create` adds one stamped
    with the clock, `list` reports the unarchived ones, `archive` removes them."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.tasks = {}
        self.archived = []

    def __call__(self, cmd, **kwargs):
        cmd = [str(part) for part in cmd]
        step = step_of(cmd)
        if step == "create":
            self.create_out = f"t_{len(self.tasks) + len(self.archived) + 1:08x}\n"
            self.tasks[self.create_out.strip()] = int(self.clock())
        elif step == "list":
            self.listed = json.dumps([{"id": t, "created_at": at} for t, at in self.tasks.items()])
        elif step == "archive":
            for task_id in cmd[3:]:
                del self.tasks[task_id]
                self.archived.append(task_id)
        return super().__call__(cmd, **kwargs)


def test_a_prod_right_after_another_does_not_archive_it():
    """Two approvals sent back to back: the gateway has not delivered the first
    prod's task when the second prod tidies up."""
    now = [NOW]
    board = Board(clock=lambda: now[0])
    prod = make(board, clock=lambda: now[0])
    assert prod("first") is True
    now[0] += 3
    assert prod("second") is True
    assert board.archived == []
    now[0] += 3600
    assert prod("third") is True
    assert board.archived == ["t_00000001", "t_00000002"]


@pytest.mark.parametrize("age,archived", [(3599, False), (3600, True), (3601, True), (0, False), (-5, False)])
def test_prod_tasks_are_archived_once_an_hour_old(age, archived):
    run = FakeRun(listed=listing(row("t_0a1b2c3d", age)))
    assert make(run)("x") is True
    assert ("archive" in run.steps) is archived


@pytest.mark.parametrize("created_at", [None, "2026-11-12T13:00:00", True, [1], {}])
def test_a_task_whose_age_cannot_be_read_is_left_alone(created_at):
    run = FakeRun(listed=listing({"id": "t_0a1b2c3d", "created_at": created_at}, {"id": "t_4e5f6a7b"}))
    assert make(run)("x") is True
    assert "archive" not in run.steps


def test_commands_started_without_an_injected_environment_drop_it_too(monkeypatch):
    """The listener passes no `environ`; it must still not hand Hermes the broker's virtualenv."""
    from hermes_broker.prod import make_prod
    monkeypatch.setenv("VIRTUAL_ENV", "/broker/.venv")
    monkeypatch.setenv("PYTHONPATH", "/broker")
    monkeypatch.setenv("HERMES_PROD_TEST_MARKER", "kept")
    run = FakeRun()
    assert make_prod(OWNER, hermes_dir=HERMES_DIR, hermes_bin=HERMES_BIN, run=run)("x") is True
    assert len(run.calls) >= 3
    for _, kwargs in run.calls:
        assert kwargs["env"]["HERMES_PROD_TEST_MARKER"] == "kept"
        assert "VIRTUAL_ENV" not in kwargs["env"] and "PYTHONPATH" not in kwargs["env"]


# ---- the create script, run for real against a stand-in kanban_db ----------

STAND_IN_KANBAN_DB = '''
import contextlib, json

@contextlib.contextmanager
def connect_closing():
    yield "conn"

def create_task(conn, **kwargs):
    with open("created.json", "w") as fh:
        json.dump({"conn": conn, **kwargs}, fh)
    print("a line some import printed")
    return "t_standin"
'''


def test_create_script_stamps_the_session_and_prints_the_task_id(tmp_path):
    """Without a session id on the task the gateway sends a notice but wakes nobody."""
    from hermes_broker.prod import CREATE_SCRIPT, SESSION
    (tmp_path / "hermes_cli").mkdir()
    (tmp_path / "hermes_cli" / "__init__.py").write_text("")
    (tmp_path / "hermes_cli" / "kanban_db.py").write_text(STAND_IN_KANBAN_DB)
    done = subprocess.run([sys.executable, "-c", CREATE_SCRIPT, "Owner approved tig-gpu.", SESSION],
                          cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "t_standin"
    created = json.loads((tmp_path / "created.json").read_text())
    assert created["title"] == "Owner approved tig-gpu."
    assert created["session_id"] == SESSION == "approvals-prod"
    assert created.get("assignee") is None  # an assigned task would be run as work


# ---- listener wiring -------------------------------------------------------


def test_listener_prods_the_owner_from_the_broker_config():
    from hermes_broker.config import BrokerConfig
    from hermes_broker.listen import _prod_wiring
    cfg = BrokerConfig(owner_telegram_id=424242, approvals_bot_token="t", repos=(),
                       ssh_targets={}, state_dir=Path("/tmp/hermes-broker-test-prod"))
    run = FakeRun()
    assert _prod_wiring(cfg, run=run)("x") is True
    subscribe = run.cmd("notify-subscribe")
    assert subscribe[subscribe.index("--chat-id") + 1] == "424242"


class Stop(BaseException):
    """Ends the listener loop from inside a test transport. Not an Exception, so
    the loop's own retry handling does not swallow it."""


def test_listener_loop_prods_when_the_owner_approves(tmp_path):
    import httpx
    from hermes_broker.approvals import run_listener
    store = store_at(tmp_path)
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    update = {"update_id": 9, "message": {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                          "text": f"approve {code}", "date": 1060}}
    polls, sent = [], []

    def telegram(request):
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            if len(polls) > 1:
                raise Stop()
            return httpx.Response(200, json={"ok": True, "result": [update]})
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    prod = Recorder()
    with pytest.raises(Stop):
        run_listener(store, "123:abc", OWNER, poll_seconds=0, prod=prod,
                     transport=httpx.MockTransport(telegram))
    assert prod.titles == ["Owner approved tig-server for 30 min. Continue what you requested it for."]
    assert sent == ["Granted tig-server for 30 min.\nFor: fix parser" + TOLD]


def test_main_starts_the_listener_with_a_prod_for_the_configured_owner(tmp_path, monkeypatch):
    import logging
    from hermes_broker import listen
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({
        "owner_telegram_id": 515151, "approvals_bot_token": "123:abc", "repos": [],
        "ssh_targets": {}, "state_dir": str(tmp_path / "state")}))
    monkeypatch.setenv("HERMES_BROKER_CONFIG", str(config))
    started = {}

    def listener(store, token, owner_id, **kwargs):
        started.update(kwargs, owner_id=owner_id)

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    run = FakeRun()
    try:
        listen.main(listener=listener, run=run)
    finally:
        root.handlers[:], root.level = handlers, level
    assert started["owner_id"] == 515151
    assert started["prod"]("x") is True
    subscribe = run.cmd("notify-subscribe")
    assert subscribe[subscribe.index("--chat-id") + 1] == "515151"


def test_listener_deploys_an_auto_request_and_relays_the_result(tmp_path):
    import threading
    import time
    import httpx
    from hermes_broker.approvals import run_listener
    from hermes_broker.tasks import TaskStore
    store = store_at(tmp_path)
    store.create_deploy_request(SHA, now=1000.0, ttl_seconds=86400, origin="auto")
    update = {"update_id": 9, "message": {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                          "text": f"deploy {SHA[:7]}", "date": 1060}}
    results = {"deploy": {"deployed": SHA}, "deployed": {"harness": SHA, "fleet": None},
               "list_triage": {"issues": [], "repos_ok": [], "errors": []}}

    class Call:
        def __init__(self):
            self.calls = []

        def __call__(self, verb, args, timeout=60):
            self.calls.append((verb, args, timeout))
            return results[verb]

    fake = Call()
    polls, sent = [], []
    done = threading.Event()

    def telegram(request):
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            if len(polls) > 1:
                # The worker thread sends the result; wait for it, bounded.
                done.wait(5)
                raise Stop()
            return httpx.Response(200, json={"ok": True, "result": [update]})
        text = json.loads(request.content)["text"]
        sent.append(text)
        if text.startswith("tig-server: deployed"):
            done.set()
        return httpx.Response(200, json={"ok": True})

    prod = Recorder()
    with pytest.raises(Stop):
        run_listener(store, "123:abc", OWNER, poll_seconds=0, tasks=TaskStore(tmp_path / "tasks.json"),
                     call=fake, state_dir=tmp_path, harness_checkout=tmp_path / "nocheckout",
                     prod=prod, transport=httpx.MockTransport(telegram))
    assert done.is_set(), sent
    assert prod.titles == []
    # The worker may send its result before the poll loop sends the acknowledgement.
    assert sum(t.startswith(f"Deploy of {SHA} to tig-server approved.") for t in sent) == 1
    assert f"tig-server: deployed {SHA[:12]}" in sent
    assert [c for c in fake.calls if c[0] == "deploy"] == [
        ("deploy", {"sha": SHA, "target": "harness"}, 900)]


def test_main_passes_harness_checkout(tmp_path, monkeypatch):
    import logging
    from hermes_broker import listen
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({
        "owner_telegram_id": 515151, "approvals_bot_token": "123:abc", "repos": [],
        "ssh_targets": {}, "state_dir": str(tmp_path / "state"),
        "harness_checkout": str(tmp_path / "hh")}))
    monkeypatch.setenv("HERMES_BROKER_CONFIG", str(config))
    started = {}

    def listener(store, token, owner_id, **kwargs):
        started.update(kwargs)

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        listen.main(listener=listener, run=FakeRun())
    finally:
        root.handlers[:], root.level = handlers, level
    assert started["harness_checkout"] == tmp_path / "hh"
