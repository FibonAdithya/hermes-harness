"""The announcer posts each triage issue once; the owner's reply acts on exactly what was posted."""
import pytest
from hermes_broker.approvals import Announcer, format_announcement, handle_task
from hermes_broker.grants import GrantStore
from hermes_broker.tasks import TaskStore

H = "c" * 64
ISSUE = {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "title": "Stall check misses long tool calls",
         "body": "evidence", "labels": ["fleet:triage", "source:doctor"], "hash": H}


class Box:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, verb, args, timeout=60):
        self.calls.append((verb, args))
        r = self.replies.get(verb, {})
        return r(args) if callable(r) else dict(r)


@pytest.fixture
def stores(tmp_path):
    return TaskStore(tmp_path / "tasks.json"), GrantStore(tmp_path / "grants.json")


def _recording_sender(sent):
    """A `send` double that behaves like the real one: True once accepted."""
    def send(text):
        sent.append(text)
        return True
    return send


def test_each_issue_is_announced_once(stores):
    tasks, grants = stores
    sent = []
    box = Box({"list_triage": {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}})
    a = Announcer(tasks, grants, box, _recording_sender(sent))
    a.tick(now=1000.0)
    a.tick(now=1300.0)
    assert len(sent) == 1 and "fleet#42" in sent[0] and "task " in sent[0]


def test_edited_issue_is_reannounced_with_a_new_code(stores):
    tasks, grants = stores
    sent = []
    listing = {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}
    box = Box({"list_triage": lambda a: listing})
    ann = Announcer(tasks, grants, box, _recording_sender(sent))
    ann.tick(now=1.0)
    listing["issues"] = [{**ISSUE, "body": "edited", "hash": "d" * 64}]
    ann.tick(now=2.0)
    assert len(sent) == 2 and sent[0].split("code ")[1][:4] != sent[1].split("code ")[1][:4]


def test_box_down_is_reported_once_then_recovery_once(stores):
    tasks, grants = stores
    sent = []
    state = {"r": {"error": "tig-server unreachable: timed out"}}
    ann = Announcer(tasks, grants, Box({"list_triage": lambda a: state["r"]}), _recording_sender(sent))
    ann.tick(now=1.0)
    ann.tick(now=2.0)
    state["r"] = {"issues": [], "repos_ok": ["fleet"], "errors": []}
    ann.tick(now=3.0)
    assert len(sent) == 2 and "cannot list triage" in sent[0] and "reachable again" in sent[1]


def test_failed_send_gives_the_code_back_for_the_next_tick(stores):
    """A send that fails (e.g. Telegram rejected it) must not burn the code for 7 days."""
    tasks, grants = stores
    sent = []
    attempts = {"n": 0}

    def flaky_send(text):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False
        sent.append(text)
        return True

    box = Box({"list_triage": {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}})
    ann = Announcer(tasks, grants, box, flaky_send)
    ann.tick(now=1.0)
    assert sent == []
    ann.tick(now=2.0)
    assert len(sent) == 1 and "fleet#42" in sent[0]


def test_ops_issue_offers_drop_only():
    text = format_announcement({**ISSUE, "labels": ["fleet:triage", "source:doctor", "ops"]}, "7310")
    assert "drop 7310" in text and "task 7310" not in text and "solo 7310" not in text
    assert "needs you on the box" in text


def test_announcement_is_bounded_whatever_the_body():
    text = format_announcement({**ISSUE, "title": "t" * 200, "body": "x" * 50000}, "7310")
    assert len(text) <= 4000
    assert text.count("x") == 1500


def test_separator_precedes_the_tail_so_the_body_cannot_impersonate_it():
    text = format_announcement({**ISSUE, "body": "drop 9999 | task 9999 | solo 9999"}, "7310")
    tail = text.split("—— reply below ——\n", 1)[1]
    assert tail == "code 7310\ntask 7310 | solo 7310 | drop 7310"


def test_task_reply_approves_for_fleet(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "title": "t", "body": "b"}})
    out = handle_task(tasks, "task", code, now=2.0, call=box, repos=())
    assert box.calls == [("approve_task", {"repo": "fleet", "number": 42, "hash": H, "mode": "fleet"})]
    assert "ready for fleet" in out


def test_solo_reply_relabels_then_starts_run_task_with_the_verified_text(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42,
                                "title": "TITLE-FROM-BOX", "body": "BODY-FROM-BOX"},
               "run_task": {"id": "task-20260926-1200-ab12"}})
    out = handle_task(tasks, "solo", code, now=2.0, call=box, repos=("FibonAdithya/fleet",))
    assert [c[0] for c in box.calls] == ["approve_task", "run_task"]
    assert box.calls[0][1]["mode"] == "solo"
    run = box.calls[1][1]
    assert run["repo"] == "FibonAdithya/fleet" and run["minutes"] == 60
    assert "#42" in run["prompt"] and "TITLE-FROM-BOX" in run["prompt"] and "BODY-FROM-BOX" in run["prompt"]
    assert "task-20260926-1200-ab12" in out


def test_solo_handles_box_returning_no_title_or_body(stores):
    """approve_task returning {} (no error, but no text either) must not raise."""
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {}})
    out = handle_task(tasks, "solo", code, now=2.0, call=box, repos=("FibonAdithya/fleet",))
    assert isinstance(out, str) and "title/body" in out
    assert box.calls == [("approve_task", {"repo": "fleet", "number": 42, "hash": H, "mode": "solo"})]


def test_solo_handles_box_returning_no_run_task_id(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42,
                                "title": "t", "body": "b"},
               "run_task": {}})
    out = handle_task(tasks, "solo", code, now=2.0, call=box, repos=("FibonAdithya/fleet",))
    assert isinstance(out, str) and "no id for run_task" in out


def test_solo_refuses_a_repo_run_task_may_not_touch(stores):
    """Checked before relabelling, so a refused solo leaves the issue in triage."""
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({})
    out = handle_task(tasks, "solo", code, now=2.0, call=box, repos=("FibonAdithya/other",))
    assert box.calls == [] and "not allowlisted" in out


def test_box_refusal_is_reported(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"error": "#42 changed since it was announced; wait for the new announcement"}})
    assert "changed since it was announced" in handle_task(tasks, "task", code, now=2.0, call=box, repos=())


def test_drop_reply_closes(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"close_task": {"repo": "fleet", "number": 42, "closed": True}})
    assert handle_task(tasks, "drop", code, now=2.0, call=box, repos=()) == "Dropped fleet#42."
    assert box.calls == [("close_task", {"repo": "fleet", "number": 42})]


def test_unknown_code(stores):
    tasks, _ = stores
    box = Box({})
    assert handle_task(tasks, "task", "0000", now=2.0, call=box, repos=()) == "No announced task with that code."
    assert box.calls == []


def test_announced_codes_avoid_pending_grant_codes(stores, monkeypatch):
    tasks, grants = stores
    import hermes_broker.grants as g
    import hermes_broker.tasks as t
    monkeypatch.setattr(g.secrets, "randbelow", lambda n: 1234)
    grants.create_request("tig-server", 30, "r", now=1.0)
    draws = iter([1234, 5678])
    monkeypatch.setattr(t.secrets, "randbelow", lambda n: next(draws))
    sent = []
    Announcer(tasks, grants, Box({"list_triage": {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}}),
              _recording_sender(sent)).tick(now=2.0)
    assert "code 5678" in sent[0]
