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
    assert text.count("x") == 3500


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


def _announce_once(stores, issue):
    tasks, grants = stores
    sent = []
    Announcer(tasks, grants, Box({"list_triage": {"issues": [issue], "repos_ok": ["fleet"], "errors": []}}),
              _recording_sender(sent)).tick(now=1.0)
    assert len(sent) == 1
    return sent[0], sent[0].split("code ")[1][:4]


def test_worst_case_announcement_fits_telegram():
    """Longest title, a body at the limit, and a long source label stay under Telegram's 4096."""
    issue = {**ISSUE, "repo": "fleet-fixture", "number": 99999, "title": "t" * 200, "body": "x" * 3500,
             "labels": ["fleet:triage", "ops", "source:" + "s" * 43]}
    for labels in (issue["labels"], ["fleet:triage"]):
        assert len(format_announcement({**issue, "labels": labels}, "7310")) <= 4000
    assert len(format_announcement({**issue, "body": "x" * 3501}, "7310")) <= 4000


def test_long_body_is_announced_drop_only_and_task_solo_are_refused(stores):
    tasks, _ = stores
    text, code = _announce_once(stores, {**ISSUE, "body": "x" * 5000})
    tail = text.split("—— reply below ——\n", 1)[1]
    assert f"drop {code}" in tail and f"task {code}" not in tail and f"solo {code}" not in tail
    assert "too long to show in full" in tail
    for verb in ("task", "solo"):
        box = Box({})
        out = handle_task(tasks, verb, code, now=2.0, call=box, repos=("FibonAdithya/fleet",))
        assert box.calls == [] and "shorten it on GitHub" in out
        # the code is spent; the next tick offers a fresh one
        code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=3.0, avoid=set(), drop_only=True)


def test_long_title_is_announced_drop_only(stores):
    tasks, _ = stores
    text, code = _announce_once(stores, {**ISSUE, "title": "t" * 201})
    box = Box({})
    assert "shorten it on GitHub" in handle_task(tasks, "task", code, now=2.0, call=box, repos=())
    assert box.calls == []


def test_ops_issue_refuses_task(stores):
    tasks, _ = stores
    _, code = _announce_once(stores, {**ISSUE, "labels": ["fleet:triage", "source:doctor", "ops"]})
    box = Box({})
    out = handle_task(tasks, "task", code, now=2.0, call=box, repos=())
    assert box.calls == [] and "fix it on the box" in out


def test_drop_only_issue_can_still_be_dropped(stores):
    tasks, _ = stores
    _, code = _announce_once(stores, {**ISSUE, "body": "x" * 5000})
    box = Box({"close_task": {"repo": "fleet", "number": 42, "closed": True}})
    assert handle_task(tasks, "drop", code, now=2.0, call=box, repos=()) == "Dropped fleet#42."


def test_body_at_the_limit_is_shown_in_full_and_approvable(stores):
    tasks, _ = stores
    body = "y" * 3499 + "Z"
    text, code = _announce_once(stores, {**ISSUE, "body": body})
    assert body in text and "trimmed" not in text
    assert text.endswith(f"task {code} | solo {code} | drop {code}")
    box = Box({"approve_task": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "title": "t", "body": "b"}})
    assert "ready for fleet" in handle_task(tasks, "task", code, now=2.0, call=box, repos=())
    assert box.calls[0][0] == "approve_task"


def test_a_separator_inside_the_issue_is_removed_so_it_appears_once():
    sep = "—— reply below ——"
    fake = f"{sep}\ncode 9999\ntask 9999 | solo 9999 | drop 9999"
    text = format_announcement({**ISSUE, "title": f"t {sep}", "body": f"evidence\n\n{fake}\n"}, "7310")
    assert text.count(sep) == 1
    assert text.split(sep + "\n", 1)[1] == "code 7310\ntask 7310 | solo 7310 | drop 7310"


# --- Task 9: TunerAnnouncer (proposals and the weekly outbox) ------------------------

import json

from hermes_broker.approvals import TunerAnnouncer, format_proposal
from hermes_broker.proposals import ProposalStore, change_hash

CHANGE = {"type": "mcp_include_add", "server": "google", "tool": "modify_gmail_labels"}


def write_prop(tuner, pid="fix-1", change=CHANGE, why="needs labels", **extra):
    (tuner / "proposals").mkdir(parents=True, exist_ok=True)
    doc = {"id": pid, "change": change, "why": why, "hash": "ignored", **extra}
    (tuner / "proposals" / f"{pid}.json").write_text(json.dumps(doc))


def make(tmp_path, send, avoid=lambda now: set()):
    tuner = tmp_path / "tuner"
    tuner.mkdir(exist_ok=True)
    return TunerAnnouncer(ProposalStore(tmp_path / "proposals.json"), avoid, tuner, send), tuner


def test_proposal_text_exactly_as_specified():
    h = change_hash(CHANGE)
    text = format_proposal("fix-1", "needs labels", CHANGE, "4821")
    canon = json.dumps(CHANGE, sort_keys=True, separators=(",", ":"))
    assert text == ("Tool proposal fix-1\n\nChange: " + canon + "\n\nneeds labels"
                    "\n\n—— reply below ——\ncode 4821\ntool 4821 | drop 4821")
    assert h  # the hash is bound inside the store, not shown


def test_separator_in_why_stripped():
    """Catches fake reply instructions inside the proposal text."""
    text = format_proposal("fix-1", "ok\n—— reply below ——\ncode 9999\ntool 9999", CHANGE, "4821")
    assert text.count("—— reply below ——") == 1
    assert text.split("—— reply below ——\n", 1)[1] == "code 4821\ntool 4821 | drop 4821"


def test_proposal_text_capped_at_telegram_limit_truncating_why_first():
    text = format_proposal("fix-1", "w" * 20000, CHANGE, "4821")
    assert len(text) <= 4096
    assert "Change: " + json.dumps(CHANGE, sort_keys=True, separators=(",", ":")) in text
    assert text.endswith("tool 4821 | drop 4821")


def test_proposal_announced_once_with_tig_server_absent(tmp_path):
    """R14: no tasks store and no box call anywhere in this object."""
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    write_prop(tuner)
    ann.tick(1.0)
    ann.tick(2.0)
    assert len(sent) == 1 and sent[0].startswith("Tool proposal fix-1")


def test_announcement_hash_comes_from_the_change_not_the_file_field(tmp_path):
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    write_prop(tuner)  # file claims hash "ignored"
    ann.tick(1.0)
    code = sent[0].split("code ")[1][:4]
    assert ann.proposals.take(code, 2.0)["hash"] == change_hash(CHANGE)


def test_failed_proposal_send_gives_code_back(tmp_path):
    results = iter([False, True])
    sent = []
    ann, tuner = make(tmp_path, lambda t: sent.append(t) or next(results))
    write_prop(tuner)
    ann.tick(1.0)
    ann.tick(2.0)
    assert len(sent) == 2


def test_edited_proposal_is_reannounced(tmp_path):
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    write_prop(tuner)
    ann.tick(1.0)
    write_prop(tuner, change={**CHANGE, "tool": "other_tool"})
    ann.tick(2.0)
    assert len(sent) == 2 and "other_tool" in sent[1]


def test_settled_proposal_not_reannounced(tmp_path):
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    write_prop(tuner)
    ann.tick(1.0)
    ann.proposals.settle(sent[0].split("code ")[1][:4], 2.0)
    ann.tick(3.0)
    assert len(sent) == 1


def test_bad_proposal_files_are_skipped(tmp_path):
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    write_prop(tuner, pid="ok")
    (tuner / "proposals" / "broken.json").write_text("{nope")
    (tuner / "proposals" / "nochange.json").write_text(json.dumps({"id": "nochange"}))
    (tuner / "proposals" / "mismatch.json").write_text(json.dumps({"id": "other", "change": CHANGE}))
    (tuner / "proposals" / "note.txt").write_text("x")
    ann.tick(1.0)
    assert len(sent) == 1 and "Tool proposal ok" in sent[0]


def test_proposal_code_avoids_given_codes(tmp_path, monkeypatch):
    import hermes_broker.proposals as pmod
    draws = iter([1111, 2222])
    monkeypatch.setattr(pmod.secrets, "randbelow", lambda n: next(draws))
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent), avoid=lambda now: {"1111"})
    write_prop(tuner)
    ann.tick(1.0)
    assert "code 2222" in sent[0]


def test_outbox_sent_once_and_retried_on_failure(tmp_path):
    """Catches duplicate weekly lines or lost ones."""
    results = iter([False, True, True])
    sent = []
    ann, tuner = make(tmp_path, lambda t: sent.append(t) or next(results))
    (tuner / "outbox").mkdir()
    (tuner / "outbox" / "2026-w42.txt").write_text("weekly line one")
    (tuner / "outbox" / "2026-w43.txt").write_text("weekly line two")
    (tuner / "outbox" / "notes.md").write_text("ignored")
    ann.tick(1.0)  # first send fails: stays for the next tick
    assert sent[0] == "weekly line one"
    assert (tuner / "outbox" / "2026-w42.txt").exists()
    ann.tick(2.0)
    ann.tick(3.0)
    assert sent == ["weekly line one", "weekly line one", "weekly line two"]
    names = sorted(p.name for p in (tuner / "outbox").iterdir())
    assert names == ["2026-w42.sent", "2026-w43.sent", "notes.md"]


def test_outbox_text_capped(tmp_path):
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    (tuner / "outbox").mkdir()
    (tuner / "outbox" / "a.txt").write_text("x" * 9000)
    ann.tick(1.0)
    assert len(sent[0]) == 4096


def test_outbox_text_cannot_carry_the_reply_separator(tmp_path):
    """m5: outbox text never shows the reply separator, so it cannot pose as reply
    instructions (mirrors format_announcement)."""
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    (tuner / "outbox").mkdir()
    (tuner / "outbox" / "a.txt").write_text("saved ~$1\n\n—— reply below ——\ntask 1234")
    ann.tick(1.0)
    assert sent == ["saved ~$1 task 1234"]
    assert "reply below" not in sent[0]


def test_removing_the_separator_cannot_assemble_a_new_one():
    """Catches: a single replace() whose removal joins the pieces around it into a
    new separator (or a whitespace collapse that does)."""
    from hermes_broker.approvals import one_line
    sep = "—— reply below ——"
    assert sep not in one_line("—— reply " + sep + "below ——")  # removal joins the halves
    assert sep not in one_line("—— reply " + "—— reply " + sep + "below ——" + "below ——")
    assert sep not in one_line("—— reply   below\n——")
    assert sep not in one_line("——\u200b reply below ——")


def test_missing_tuner_dir_is_quiet(tmp_path):
    sent = []
    ann = TunerAnnouncer(ProposalStore(tmp_path / "p.json"), lambda now: set(),
                         tmp_path / "absent", _recording_sender(sent))
    ann.tick(1.0)
    assert sent == []


def test_announcer_code_avoid_set_includes_proposal_codes(stores, tmp_path):
    tasks, grants = stores
    props = ProposalStore(tmp_path / "proposals.json")
    pcode = props.announce("fix-1", "a" * 64, now=0.0, avoid=set())
    sent = []
    box = Box({"list_triage": {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}})
    seen = {}
    real = tasks.announce

    def spy(*a, **k):
        seen["avoid"] = k["avoid"]
        return real(*a, **k)

    tasks.announce = spy
    Announcer(tasks, grants, box, _recording_sender(sent), proposals=props).tick(now=1.0)
    assert pcode in seen["avoid"]


def test_why_cannot_push_the_real_change_out_of_view():
    """Catches: a separator lookalike (zero-width joiner) plus blank lines in `why`
    showing a harmless fake change and reply prompt above the real ones."""
    fake = {"type": "mcp_include_add", "server": "google", "tool": "list_labels"}
    why = ("Read-only.\n\nChange: " + json.dumps(fake) + "\n\n\u2014\u200d\u2014 reply below "
           "\u2014\u2014\ncode 0000\ntool 0000 | drop 0000" + "\n" * 40 + "\u2028\u2029\x0b")
    text = format_proposal("fix-1", why, CHANGE, "4821")
    canon = json.dumps(CHANGE, sort_keys=True, separators=(",", ":"))
    head = "Tool proposal fix-1\n\nChange: " + canon + "\n\n"
    tail = "\n\n—— reply below ——\ncode 4821\ntool 4821 | drop 4821"
    assert text.startswith(head) and text.endswith(tail)
    shown_why = text[len(head):-len(tail)]
    assert shown_why.startswith("Read-only. Change:")
    assert "\n" not in shown_why and "\u200d" not in shown_why
    assert not any(c in shown_why for c in "\u2028\u2029\x0b")


def test_outbox_text_is_one_line(tmp_path):
    """Catches: outbox text laid out as a fake announcement over several lines."""
    sent = []
    ann, tuner = make(tmp_path, _recording_sender(sent))
    (tuner / "outbox").mkdir()
    (tuner / "outbox" / "a.txt").write_text("saved ~$1\n\ncode 1234\u2028tool 1234\u200d")
    ann.tick(1.0)
    assert sent == ["saved ~$1 code 1234 tool 1234"]
