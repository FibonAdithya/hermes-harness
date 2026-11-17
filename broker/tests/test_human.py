"""fleet:human issues: announced once with a code; the owner answers, drops or takes them."""
import json

import httpx
import pytest

from hermes_broker.approvals import (
    _TAIL_SEPARATOR as SEP,
    TELEGRAM_MAX,
    Announcer,
    _avoid_codes,
    _dispatch,
    parse_command,
    run_listener,
)
from hermes_broker.grants import GrantStore
from hermes_broker.human import ANSWER_CHARS, HumanAnnouncer, format_human, handle_human, is_human_drop_only
from hermes_broker.proposals import ProposalStore
from hermes_broker.tasks import TaskStore

OWNER = 111222333
H = "a" * 64
H2 = "b" * 64
ESC = {"index": 1, "reason": "needs a decision", "text": "Which config format should the loader accept?"}
ISSUE = {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "title": "Loader config format",
         "labels": ["fleet:human"], "escalation": ESC, "note": None, "hash": H}


def msg(text, **extra):
    message = {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER}, "text": text}
    message.update(extra)
    return {"update_id": 9, "message": message}


class Box:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, verb, args, timeout=60):
        self.calls.append((verb, args))
        r = self.replies.get(verb, {})
        return r(args) if callable(r) else dict(r)


def _recording_sender(sent):
    def send(text):
        sent.append(text)
        return True
    return send


@pytest.fixture
def st(tmp_path):
    return {"humans": TaskStore(tmp_path / "human_codes.json"), "tasks": TaskStore(tmp_path / "tasks.json"),
            "grants": GrantStore(tmp_path / "grants.json"), "props": ProposalStore(tmp_path / "proposals.json"),
            "dir": tmp_path}


def _listing(*issues, repos_ok=("fleet",)):
    return {"issues": list(issues), "repos_ok": list(repos_ok), "errors": []}


def _announcer(st, box, send):
    return HumanAnnouncer(st["humans"], st["grants"], st["tasks"], st["props"], box, send)


def _code(text):
    return text.split("\ncode ")[1][:4]


def _announce(st, issue=ISSUE):
    sent = []
    _announcer(st, Box({"list_human": _listing(issue)}), _recording_sender(sent)).tick(now=1.0)
    (text,) = sent
    return _code(text)


# --- parsing ------------------------------------------------------------------------------

def test_parse_answer_multiline():
    assert parse_command(msg("Answer 1234   use TOML.\nKeep JSON as a fallback.\n  "), OWNER) == (
        "answer", "1234\nuse TOML.\nKeep JSON as a fallback.")
    assert parse_command(msg("answer 1234\nfirst line on its own"), OWNER) == (
        "answer", "1234\nfirst line on its own")


def test_parse_take():
    assert parse_command(msg("take 0420"), OWNER) == ("take", "0420")
    assert parse_command(msg("TAKE 0420"), OWNER) == ("take", "0420")
    assert parse_command(msg("take 042"), OWNER) is None
    assert parse_command(msg("take 04200"), OWNER) is None
    assert parse_command(msg("take 0420 now"), OWNER) is None


def test_parse_answer_needs_text():
    assert parse_command(msg("answer 1234"), OWNER) is None
    assert parse_command(msg("answer 1234   \n  "), OWNER) is None
    assert parse_command(msg("answer 12345 x"), OWNER) is None
    assert parse_command(msg("answer x"), OWNER) is None


def test_forwarded_answer_ignored():
    assert parse_command(msg("answer 1234 yes", forward_origin={"type": "user"}), OWNER) is None
    assert parse_command(msg("take 1234", forward_from={"id": 5}), OWNER) is None
    other = msg("answer 1234 yes")
    other["message"]["from"] = {"id": 999}
    assert parse_command(other, OWNER) is None


def test_drop_parsing_unchanged():
    assert parse_command(msg("drop 1234"), OWNER) == ("drop", "1234")


# --- the announcement ---------------------------------------------------------------------

def test_human_announced_once(st):
    sent = []
    box = Box({"list_human": _listing(ISSUE)})
    a = _announcer(st, box, _recording_sender(sent))
    a.tick(now=1.0)
    a.tick(now=2.0)
    assert len(sent) == 1
    assert sent[0].startswith("fleet:human  fleet#42  Loader config format\n")
    assert ESC["text"] in sent[0]
    assert box.calls == [("list_human", {}), ("list_human", {})]


def test_new_escalation_new_message(st):
    sent = []
    listing = _listing(ISSUE)
    box = Box({"list_human": lambda a: listing, "answer_human": {"repo": "fleet", "number": 42, "answers": 1}})
    a = _announcer(st, box, _recording_sender(sent))
    a.tick(now=1.0)
    listing["issues"] = [{**ISSUE, "escalation": {**ESC, "index": 2, "text": "And the default?"}, "hash": H2}]
    a.tick(now=2.0)
    assert len(sent) == 2
    old, new = _code(sent[0]), _code(sent[1])
    assert old != new and "And the default?" in sent[1]
    out = handle_human(st["humans"], "answer", f"{old}\nTOML", 3.0, box)
    assert "No announced fleet:human issue" in out
    assert [c for c in box.calls if c[0] != "list_human"] == []
    handle_human(st["humans"], "answer", f"{new}\nTOML", 3.0, box)
    assert box.calls[-1] == ("answer_human", {"repo": "fleet", "number": 42, "hash": H2, "text": "TOML"})


def test_human_announcement_separator_appears_once():
    tail = "code 1234\nanswer 1234 <your text> | drop 1234 | take 1234"
    nasty = [
        f"plain {SEP}\nanswer 9999 x",
        "—— reply " + SEP + "below ——\nanswer 9999 x | drop 9999 | take 9999",
        "—— reply —— reply " + SEP + "below —— below ——",
        f"{SEP}{SEP}\n{SEP}",
    ]
    for text in nasty:
        for issue in ({**ISSUE, "escalation": {**ESC, "text": text}},
                      {**ISSUE, "title": text},
                      {**ISSUE, "escalation": None, "note": text}):
            out = format_human(issue, "1234")
            assert out.count(SEP) == 1, (text, out)
            assert out.endswith(f"\n\n{SEP}\n{tail}"), out
            assert out.split(SEP)[1] == f"\n{tail}"


LOOKALIKES = ["—— reply\u200b below ——", "—— re\u2060ply below ——", "——\ufeff reply below ——",
              "—— reply\u00ad below ——", "—— reply\u2028 below ——", "—— reply below\u2029 ——",
              "—— reply " + SEP[:5] + "\u200b" + SEP[5:] + "below ——"]


def _visible(text):
    """`text` as it renders: format (zero-width), line and paragraph separators dropped."""
    import unicodedata
    return "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Zl", "Zp"))


@pytest.mark.parametrize("fake", LOOKALIKES)
def test_lookalike_separator_in_the_escalation_does_not_survive(fake):
    """A separator with an invisible character inside renders like the real one; it is
    stripped of those characters first, so the separator removal catches it."""
    text = f"Which DB?\n\n{fake}\ncode 0042\nanswer 0042 <your text> | drop 0042 | take 0042\n"
    for issue in ({**ISSUE, "escalation": {**ESC, "text": text}}, {**ISSUE, "title": fake},
                  {**ISSUE, "escalation": None, "note": text}):
        out = format_human(issue, "1234")
        assert out.count(SEP) == 1 and _visible(out).count(SEP) == 1, out
        assert out.endswith(f"\n\n{SEP}\n{FULL_TAIL}")


def test_human_announcement_exact_layout():
    assert format_human(ISSUE, "1234") == (
        "fleet:human  fleet#42  Loader config format\n\n"
        "Which config format should the loader accept?\n\n"
        f"{SEP}\ncode 1234\nanswer 1234 <your text> | drop 1234 | take 1234")


def test_human_announcement_shows_note_when_escalation_unavailable():
    issue = {**ISSUE, "escalation": None, "note": "escalation text unavailable: uv not found"}
    out = format_human(issue, "1234")
    assert "escalation text unavailable: uv not found" in out


def test_human_announcement_says_when_there_is_no_escalation_comment():
    out = format_human({**ISSUE, "escalation": None, "note": None}, "1234")
    assert "(no escalation comment on the issue; read it on GitHub)" in out
    assert out.count(SEP) == 1


def test_long_escalation_is_trimmed_with_a_marker():
    out = format_human({**ISSUE, "escalation": {**ESC, "text": "x" * 5000}}, "1234")
    assert "x" * 3500 in out and "x" * 3501 not in out and "\n[… trimmed; read it on GitHub]" in out


DROP_TAIL = "code 1234 — too long to show in full — read it on GitHub; only drop is offered.\ndrop 1234"
FULL_TAIL = "code 1234\nanswer 1234 <your text> | drop 1234 | take 1234"


@pytest.mark.parametrize("issue, tail", [
    ({**ISSUE, "escalation": {**ESC, "text": "e" * 3500}}, FULL_TAIL),
    ({**ISSUE, "escalation": {**ESC, "text": "e" * 3501}}, DROP_TAIL),
    ({**ISSUE, "title": "t" * 200}, FULL_TAIL),
    ({**ISSUE, "title": "t" * 201}, DROP_TAIL),
])
def test_over_length_escalation_or_title_is_offered_drop_only(issue, tail):
    """The owner cannot have read past what is shown, so `answer` and `take` (which bind
    the hash of the whole escalation and title) are not offered; triage does the same."""
    out = format_human(issue, "1234")
    assert out.endswith(f"\n\n{SEP}\n{tail}"), out[-200:]
    assert is_human_drop_only(issue) == (tail == DROP_TAIL)


def test_over_length_escalation_answer_and_take_are_refused(st):
    long = {**ISSUE, "escalation": {**ESC, "text": "A" * 3600 + "\nthe real question is hidden here"}}
    sent = []
    _announcer(st, Box({"list_human": _listing(long)}), _recording_sender(sent)).tick(now=1.0)
    (text,) = sent
    assert "hidden here" not in text
    for verb, arg in (("answer", "\nyes"), ("take", "")):
        code = _code(text)
        box = Box({})
        out = handle_human(st["humans"], verb, code + arg, 2.0, box)
        assert box.calls == [], verb
        assert out == ("fleet#42 was offered drop only: its escalation or title is too long to show here in full. "
                       "Read it on GitHub and handle it there, or drop it. This code is spent; if the issue still "
                       "waits on you it will be announced again with a new code."), out
        sent.clear()
        _announcer(st, Box({"list_human": _listing(long)}), _recording_sender(sent)).tick(now=3.0)
        (text,) = sent
    box = Box({"close_task": {"repo": "fleet", "number": 42, "closed": True}})
    assert handle_human(st["humans"], "drop", _code(text), 4.0, box) == "Dropped fleet#42."
    assert box.calls == [("close_task", {"repo": "fleet", "number": 42})]


def test_worst_case_human_announcement_fits_telegram():
    issue = {**ISSUE, "title": "t" * 200 + "T" * 300, "escalation": {**ESC, "text": "e" * 9000}}
    out = format_human(issue, "1234")
    assert len(out) <= TELEGRAM_MAX
    assert "t" * 200 in out and "T" not in out.split("\n")[0]


# --- the announcer ------------------------------------------------------------------------

def test_failed_send_gives_code_back(st):
    sent, attempts = [], {"n": 0}

    def flaky(text):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False
        sent.append(text)
        return True

    a = _announcer(st, Box({"list_human": _listing(ISSUE)}), flaky)
    a.tick(now=1.0)
    assert sent == [] and st["humans"].live_codes(1.0) == set()
    a.tick(now=2.0)
    assert len(sent) == 1 and st["humans"].live_codes(2.0) == {_code(sent[0])}


def test_human_box_down_reported_once_then_recovery_once(st):
    sent = []
    state = {"r": {"error": "tig-server unreachable: timed out"}}
    a = _announcer(st, Box({"list_human": lambda a: state["r"]}), _recording_sender(sent))
    a.tick(now=1.0)
    a.tick(now=2.0)
    assert len(sent) == 1 and "cannot list fleet:human on tig-server" in sent[0] and "timed out" in sent[0]
    state["r"] = _listing()
    a.tick(now=3.0)
    a.tick(now=4.0)
    assert len(sent) == 2 and "reachable again" in sent[1]


def test_prune_keeps_codes_for_repos_that_failed_to_list(st):
    listing = _listing(ISSUE)
    a = _announcer(st, Box({"list_human": lambda a: listing}), _recording_sender([]))
    a.tick(now=1.0)
    (code,) = st["humans"].live_codes(1.0)
    listing.update(issues=[], repos_ok=[])  # fleet's listing failed this time
    a.tick(now=2.0)
    assert st["humans"].live_codes(2.0) == {code}
    listing.update(issues=[], repos_ok=["fleet"])  # listed, and the issue is gone
    a.tick(now=3.0)
    assert st["humans"].live_codes(3.0) == set()


# --- replies ------------------------------------------------------------------------------

def test_answer_reply_calls_answer_human(st):
    code = _announce(st)
    box = Box({"answer_human": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "answers": 1}})
    update = msg(f"answer {code}  Use TOML.\n\nKeep JSON readable for one release.  ")
    out = _dispatch(update, OWNER, st["grants"], st["tasks"], box, (), now_fn=lambda: 2.0,
                    proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
    assert box.calls == [("answer_human", {"repo": "fleet", "number": 42, "hash": H,
                                           "text": "Use TOML.\n\nKeep JSON readable for one release."})]
    assert "fleet#42" in out and "answer" in out.lower()
    assert st["humans"].live_codes(2.0) == set()


def test_answer_too_long_refused_without_call(st):
    code = _announce(st)
    box = Box({})
    out = handle_human(st["humans"], "answer", f"{code}\n" + "y" * (ANSWER_CHARS + 1), 2.0, box)
    assert box.calls == []
    assert "fleet#42" in out and str(ANSWER_CHARS) in out and "announced again" in out
    assert st["humans"].live_codes(2.0) == set()  # spent


def test_answer_at_the_limit_is_sent(st):
    code = _announce(st)
    box = Box({"answer_human": {"repo": "fleet", "number": 42, "answers": 1}})
    handle_human(st["humans"], "answer", f"{code}\n" + "y" * ANSWER_CHARS, 2.0, box)
    assert [v for v, _ in box.calls] == ["answer_human"]


def test_answer_box_error_is_reported_and_code_spent(st):
    code = _announce(st)
    box = Box({"answer_human": {"error": "the escalation changed since it was announced"}})
    out = handle_human(st["humans"], "answer", f"{code}\nTOML", 2.0, box)
    assert out.startswith("fleet#42: ") and "escalation changed" in out
    assert st["humans"].live_codes(2.0) == set()


def test_drop_routes_to_human_store(st):
    code = _announce(st)
    assert st["tasks"].live_codes(2.0) == set() and st["props"].live_codes(2.0) == set()
    box = Box({"close_task": {"repo": "fleet", "number": 42, "closed": True}})
    out = _dispatch(msg(f"drop {code}"), OWNER, st["grants"], st["tasks"], box, (), now_fn=lambda: 2.0,
                    proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
    assert box.calls == [("close_task", {"repo": "fleet", "number": 42})]
    assert "fleet#42" in out and "No announced task" not in out
    assert st["humans"].live_codes(2.0) == set()


def test_drop_box_error_says_the_issue_comes_back(st):
    code = _announce(st)
    box = Box({"close_task": {"error": "gh: simulated failure"}})
    out = _dispatch(msg(f"drop {code}"), OWNER, st["grants"], st["tasks"], box, (), now_fn=lambda: 2.0,
                    proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
    assert out == ("fleet#42: gh: simulated failure\nThis code is spent; if the issue still waits on you "
                   "it will be announced again with a new code.")


def test_drop_still_reaches_tasks_when_code_is_not_human(st):
    _announce(st)
    tcode = st["tasks"].announce("fleet", "FibonAdithya/fleet", 7, H, now=1.0,
                                 avoid=st["humans"].live_codes(1.0))
    box = Box({"close_task": {"repo": "fleet", "number": 7, "closed": True}})
    out = _dispatch(msg(f"drop {tcode}"), OWNER, st["grants"], st["tasks"], box, (), now_fn=lambda: 2.0,
                    proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
    assert out == "Dropped fleet#7." and box.calls == [("close_task", {"repo": "fleet", "number": 7})]
    assert len(st["humans"].live_codes(2.0)) == 1


def test_take_reply_calls_take_human(st):
    code = _announce(st)
    box = Box({"take_human": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42,
                              "title": "Loader config format", "assignee": "tig-bot"}})
    out = _dispatch(msg(f"take {code}"), OWNER, st["grants"], st["tasks"], box, (), now_fn=lambda: 2.0,
                    proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
    assert box.calls == [("take_human", {"repo": "fleet", "number": 42, "hash": H})]
    assert "fleet#42" in out
    assert st["humans"].live_codes(2.0) == set()


def test_take_box_error_is_reported(st):
    code = _announce(st)
    out = handle_human(st["humans"], "take", code, 2.0, Box({"take_human": {"error": "issue is closed"}}))
    assert out.startswith("fleet#42: ") and "issue is closed" in out


def test_unknown_human_code(st):
    box = Box({})
    out = handle_human(st["humans"], "take", "0001", 2.0, box)
    assert "No announced fleet:human issue" in out and box.calls == []


def test_take_and_answer_do_not_reach_the_task_store(st):
    tcode = st["tasks"].announce("fleet", "FibonAdithya/fleet", 7, H, now=1.0, avoid=set())
    box = Box({})
    for text in (f"take {tcode}", f"answer {tcode} yes"):
        out = _dispatch(msg(text), OWNER, st["grants"], st["tasks"], box, (), now_fn=lambda: 2.0,
                        proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
        assert "No announced fleet:human issue" in out
    assert box.calls == []
    assert st["tasks"].live_codes(2.0) == {tcode}


def test_human_reply_exception_is_caught_and_reported(st):
    code = _announce(st)

    def boom(*a, **k):
        raise RuntimeError("ssh exploded")

    out = _dispatch(msg(f"take {code}"), OWNER, st["grants"], st["tasks"], boom, (), now_fn=lambda: 2.0,
                    proposals=st["props"], state_dir=st["dir"], humans=st["humans"])
    assert "failed" in out and "ssh exploded" in out


# --- code uniqueness ----------------------------------------------------------------------

def test_codes_unique_across_stores(st, monkeypatch):
    """(a) the human store avoids grants, tasks and proposals; (b) triage tasks and the
    tuner's proposals avoid the human store."""
    import hermes_broker.tasks as tmod
    g = st["grants"].create_request("tig-server", 30, "r", now=1.0)
    t = st["tasks"].announce("fleet", "FibonAdithya/fleet", 1, H, now=1.0, avoid={g})
    p = st["props"].announce("fix-0", H, now=1.0, avoid={g, t})
    draws = iter([int(g), int(t), int(p), 4242])
    monkeypatch.setattr(tmod.secrets, "randbelow", lambda n: next(draws))
    sent = []
    _announcer(st, Box({"list_human": _listing(ISSUE)}), _recording_sender(sent)).tick(now=1.0)
    assert _code(sent[0]) == "4242"
    assert st["humans"].live_codes(1.0) == {"4242"}

    # (b) the triage announcer never reuses a live human code
    draws2 = iter([4242, 5151])
    monkeypatch.setattr(tmod.secrets, "randbelow", lambda n: next(draws2))
    triage = {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 9, "title": "t", "body": "b",
              "labels": ["fleet:triage"], "hash": H2}
    tsent = []
    Announcer(st["tasks"], st["grants"], Box({"list_triage": _listing(triage)}), _recording_sender(tsent),
              proposals=st["props"], humans=st["humans"]).tick(now=1.0)
    assert "code 5151" in tsent[0]

    # ... and the avoid set the listener gives the tuner includes the human store
    assert "4242" in _avoid_codes(st["grants"], st["tasks"], st["props"], 1.0, humans=st["humans"])


class Stop(BaseException):
    """Ends the listener loop from inside a test transport."""


def _telegram(updates_per_poll, sent):
    polls = []

    def handler(request):
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            if len(polls) > len(updates_per_poll):
                raise Stop()
            return httpx.Response(200, json={"ok": True, "result": updates_per_poll[len(polls) - 1]})
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})
    return handler, polls


def test_listener_tuner_avoids_human_codes(st, monkeypatch):
    """The tuner's avoid_fn, as the listener wires it, includes live human codes."""
    import time

    import hermes_broker.proposals as pmod
    hcode = st["humans"].announce("fleet", "FibonAdithya/fleet", 42, H, now=time.time(), avoid=set())
    tuner = st["dir"] / "tuner"
    (tuner / "proposals").mkdir(parents=True)
    (tuner / "proposals" / "fix-1.json").write_text(json.dumps(
        {"id": "fix-1", "change": {"type": "mcp_include_add", "server": "g", "tool": "x"}, "why": "w"}))
    draws = iter([int(hcode), 4343])
    monkeypatch.setattr(pmod.secrets, "randbelow", lambda n: next(draws))
    sent = []
    handler, _ = _telegram([[]], sent)
    with pytest.raises(Stop):
        run_listener(st["grants"], "123:abc", OWNER, poll_seconds=0, proposals=st["props"], tuner_dir=tuner,
                     state_dir=st["dir"], humans=st["humans"], transport=httpx.MockTransport(handler))
    assert st["props"].live_codes(time.time()) == {"4343"}
    assert any("code 4343" in s for s in sent)


def test_listener_survives_human_tick_error(st):
    calls = []

    def call(verb, args, timeout=60):
        calls.append(verb)
        if verb == "list_human":
            raise RuntimeError("list_human exploded")
        return {"issues": [], "repos_ok": [], "errors": []}

    code = st["grants"].create_request("tig-server", 30, "fix parser", now=1000.0)
    approve = {"update_id": 10, "message": {"message_id": 2, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                            "text": f"approve {code}", "date": 1060}}
    sent = []
    handler, polls = _telegram([[], [approve]], sent)
    with pytest.raises(Stop):
        run_listener(st["grants"], "123:abc", OWNER, poll_seconds=0, tasks=st["tasks"], call=call,
                     state_dir=st["dir"], humans=st["humans"], transport=httpx.MockTransport(handler))
    assert "list_human" in calls and "list_triage" in calls
    assert len(polls) == 3
    assert any(s.startswith("Granted tig-server") for s in sent)


def test_human_module_imports_on_its_own():
    """approvals and human import each other; importing human first must work."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-c", "import hermes_broker.human"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_listener_routes_human_replies(st):
    """run_listener hands its human store to the reply dispatch, not only to the announcer."""
    import time
    hcode = st["humans"].announce("fleet", "FibonAdithya/fleet", 42, H, now=time.time(), avoid=set())
    calls = []

    def call(verb, args, timeout=60):
        calls.append((verb, args))
        if verb == "list_human":
            return {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}
        return {"repo": "fleet", "number": 42, "assignee": "tig-bot"}

    take = {"update_id": 10, "message": {"message_id": 2, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                         "text": f"take {hcode}", "date": 1060}}
    sent = []
    handler, _ = _telegram([[take]], sent)
    with pytest.raises(Stop):
        run_listener(st["grants"], "123:abc", OWNER, poll_seconds=0, call=call, state_dir=st["dir"],
                     humans=st["humans"], transport=httpx.MockTransport(handler))
    assert ("take_human", {"repo": "fleet", "number": 42, "hash": H}) in calls
    assert any(s.startswith("fleet#42 is yours") for s in sent)
