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
TOLD = "\nHermes has been told and will continue."
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


def make(run, environ=None):
    from hermes_broker.prod import make_prod
    return make_prod(OWNER, hermes_dir=HERMES_DIR, hermes_bin=HERMES_BIN, run=run,
                     environ={} if environ is None else environ)


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
    assert TITLE_CHARS == 120
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
                                   subprocess.TimeoutExpired("hermes", 30)])
def test_prod_fails_quietly_when_a_command_cannot_run(step, error):
    run = FakeRun(raises={step: error})
    assert make(run)("x") is False


def test_prod_archives_earlier_prod_tasks_but_not_the_one_it_just_made():
    """The new task is delivered by the gateway up to 5s later; it must stay."""
    from hermes_broker.prod import SESSION
    run = FakeRun(listed=json.dumps([{"id": "t_0a1b2c3d"}, {"id": "t_abc123"}, {"id": "t_4e5f6a7b"}]))
    assert make(run)("x") is True
    assert run.steps == ["create", "notify-subscribe", "complete", "list", "archive"]
    assert run.cmd("list") == [str(HERMES_BIN), "kanban", "list", "--session", SESSION, "--json"]
    assert run.cmd("archive") == [str(HERMES_BIN), "kanban", "archive", "t_0a1b2c3d", "t_4e5f6a7b"]


def test_prod_archives_nothing_when_only_the_new_task_is_listed():
    run = FakeRun(listed=json.dumps([{"id": "t_abc123"}]))
    assert make(run)("x") is True
    assert "archive" not in run.steps


@pytest.mark.parametrize("run", [
    FakeRun(fail={"list"}, listed=json.dumps([{"id": "t_0a1b2c3d"}])),
    FakeRun(listed="not json"),
    FakeRun(listed=json.dumps({"id": "t_0a1b2c3d"})),
    FakeRun(listed=json.dumps([{"id": "t_0a1b2c3d"}]), fail={"archive"}),
    FakeRun(raises={"list": subprocess.TimeoutExpired("hermes", 30)}),
])
def test_tidying_up_never_fails_a_prod_that_was_delivered(run):
    assert make(run)("x") is True


def test_prod_archives_only_well_formed_ids():
    """A listing is data from another program: only task ids go on a command line."""
    run = FakeRun(listed=json.dumps([{"id": "--rm"}, {"id": "t_0a1b2c3d"}, {"title": "no id"}, "t_str", {"id": 7}]))
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
    run = FakeRun(listed=json.dumps([{"id": "t_0a1b2c3d"}]))
    assert make(run)("x") is True
    assert len(run.calls) == 5
    for _, kwargs in run.calls:
        assert 0 < kwargs["timeout"] <= 60


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
