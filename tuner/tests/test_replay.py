import json
import random
import time

import pytest

from hermes_tuner import policy
from hermes_tuner.replay import boilerplate, norm, survives, used_strings


def test_used_strings_finds_verbatim_reuse():
    res = "Subject: Dentist appointment on 2026-10-30 at 14:00, 12 High Street"
    got = used_strings(res, ['{"summary": "Dentist appointment", "location": "12 High Street"}'], [])
    assert "dentist appointment" in got and "12 high street" in got


def test_used_strings_out_of_order():
    res = "Dentist appointment on Friday. Location: 12 High Street"
    got = used_strings(res, ['{"location": "12 High Street", "summary": "Dentist appointment"}'], [])
    assert "dentist appointment" in got and "12 high street" in got   # difflib's alignment finds one


def test_used_strings_reads_json_result_text():
    res = json.dumps({"result": "Dentist appointment\nat 14:00"})
    got = used_strings(res, ["Dentist appointment at 14:00"], [])
    assert "dentist appointment at 14:00" in got                     # raw JSON has a literal \n here


def test_used_strings_excludes_prompt_and_short():
    got = used_strings("calendar sync event: abc", ['calendar sync event: abc'], ["Calendar sync event"])
    assert "calendar sync event" not in got and all(len(s) >= 8 for s in got)


def test_survives_after_reduction():
    assert survives("12 high street", "Visit   12 High Street today")


def test_used_strings_bounded_time():   # Review Focus 3
    big = ("lorem ipsum dolor " * 12000)
    t0 = time.monotonic(); used_strings(big, [big[:5000]] * 20, []); assert time.monotonic() - t0 < 5


# --- Beyond the brief ---

def test_norm_lowercases_collapses_and_strips():
    assert norm("  Hello\t\n  World \r\n") == "hello world"
    assert norm("") == ""


def test_used_strings_min_length_boundary():
    # The brief's exclude test cannot see the minimum: its only match is 24 chars.
    res = "alpha 1234567 beta 12345678 gamma"
    assert used_strings(res, ["q1234567q"], []) == frozenset()
    assert used_strings(res, ["q12345678q"], []) == frozenset({"12345678"})
    assert policy.USED_MIN_CHARS == 8


def test_used_strings_exclusion_drops_substrings_of_exclude():
    # The brief's exclude test passes without exclusion: its match is longer than the excluded text.
    res = "Please sync calendar events now"
    later = ["SYNC  CALENDAR EVENTS"]
    assert used_strings(res, later, []) == frozenset({"sync calendar events"})
    assert used_strings(res, later, ["Sync calendar events for the owner"]) == frozenset()
    assert used_strings(res, later, ["unrelated", "calendar events"]) == frozenset({"sync calendar events"})


def test_used_strings_unions_later_messages_and_is_maximal():
    res = "Dentist appointment on Friday at 12 High Street"
    got = used_strings(res, ["dentist appointment", "see 12 high street!"], [])
    assert got == frozenset({"dentist appointment", "12 high street"})


def test_used_strings_result_bound_is_50000_normalised_chars():
    assert policy.USED_MAX_CHARS == 50_000
    s = "zqxv meeting room"                                        # 17 chars, absent from filler
    pad = "ab " * 20_000                                          # 60,000 chars, all whitespace single
    at_end = pad[: 50_000 - len(s)] + s                           # ends exactly at char 50,000
    past = pad[: 50_000 - len(s) + 1] + s                         # last char is char 50,001
    assert s in used_strings(at_end + pad, [s], [])
    got = used_strings(past + pad, [s], [])
    assert s not in got and s[:-1] in got
    # Whitespace runs collapse before the bound applies.
    assert s in used_strings("   \n\t" * 30_000 + at_end, [s], [])


def test_used_strings_later_bound_is_50000_normalised_chars():
    s = "zqxv meeting room"
    res = "the " + s + " is booked"
    assert s in used_strings(res, ["k" * (50_000 - len(s)) + s], [])
    got = used_strings(res, ["k" * (50_000 - len(s) + 1) + s], [])
    assert s not in got and s[:-1] in got


def _brute_maximal(a: str, b: str, min_len: int) -> set[str]:
    out = set()
    for i in range(len(b)):
        for j in range(i + min_len, len(b) + 1):
            sub = b[i:j]
            if sub not in a:
                break
            left_ok = i == 0 or b[i - 1:j] not in a
            right_ok = j == len(b) or b[i:j + 1] not in a
            if left_ok and right_ok:
                out.add(sub)
    return {s.strip() for s in out if len(s.strip()) >= min_len}


def test_used_strings_matches_brute_force():
    rng = random.Random(0)
    alphabet = "abc "
    nonempty = 0
    for _ in range(200):
        a_raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        later_raw = []
        for _ in range(rng.randint(1, 3)):
            if a_raw and rng.random() < 0.5:   # splice pieces of a so long matches occur
                parts = []
                while sum(map(len, parts)) < rng.randint(0, 40):
                    i = rng.randrange(len(a_raw))
                    parts.append(a_raw[i:i + rng.randint(1, 15)])
                    parts.append(rng.choice(alphabet))
                later_raw.append("".join(parts)[:40])
            else:
                later_raw.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))))
        a = norm(a_raw)
        want = set()
        for b_raw in later_raw:
            want |= _brute_maximal(a, norm(b_raw), 8)
        got = used_strings(a_raw, later_raw, [])
        assert got == want, (a_raw, later_raw)
        nonempty += bool(want)
    assert nonempty >= 50   # the comparison is not vacuous


def test_survives_uses_text_view_and_norm():
    reduced = json.dumps({"result": "Dentist\nAppointment", "x": 3})
    assert survives("dentist appointment", reduced)
    assert not survives("dentist appointment", json.dumps({"result": "Dentist"}))


def test_used_strings_strips_edge_spaces():
    res = json.dumps({"snippet": "Dentist appointment at 3pm, 12 High Street", "subject": "Reminder"})
    got = used_strings(res, ["I created the dentist appointment at noon"], [])
    assert got == frozenset({"dentist appointment at"})
    assert survives("dentist appointment at", json.dumps({"snippet": "Dentist appointment at"}))


def test_used_strings_exclusion_applies_to_stripped_string():
    res = "Please sync calendar events now"
    assert used_strings(res, ["x sync calendar events"], []) == frozenset({"sync calendar events"})
    assert used_strings(res, ["x sync calendar events"], ["Sync calendar events"]) == frozenset()


def test_used_strings_min_length_applies_after_strip():
    res = "x abcdefg y"
    assert used_strings(res, ["q abcdefg q"], []) == frozenset()   # " abcdefg " is 9, "abcdefg" 7


_S = "weekly standup"


def test_boilerplate_needs_min_results():
    assert policy.BOILERPLATE_MIN_RESULTS == 4
    assert boilerplate([_S], [f"r{i} {_S}" for i in range(3)]) == frozenset()


def test_boilerplate_more_than_half_of_results():
    results = [f"a {_S}", f"b {_S}", f"c {_S}", "d nothing"]
    assert boilerplate([_S, "dentist appointment"], results) == frozenset({_S})
    assert policy.BOILERPLATE_SHARE == 0.5


def test_boilerplate_exactly_half_is_kept():
    results = [f"a {_S}", f"b {_S}", "c nothing", "d nothing"]
    assert boilerplate([_S], results) == frozenset()


def test_boilerplate_counts_results_not_used_sets():
    # Used in every result's used set, but in only half of the result texts.
    results = [f"a {_S}", f"b {_S}", "c nothing", "d nothing"]
    assert boilerplate([_S] * 4, results) == frozenset()


def test_boilerplate_reads_normalised_text_view():
    results = [json.dumps({"result": "Weekly\nStandup " + x}) for x in "abc"] + ["d nothing"]
    assert boilerplate([_S], results) == frozenset({_S})


# --- Task 6: greedy reducer search on replay -------------------------------------
# Synthetic text only. Results use the live shape: the same text under `result` and
# `structuredContent.result`.

from factory import NOUS_V4_PRO  # noqa: E402
from hermes_tuner.detect import carried_usd  # noqa: E402
from hermes_tuner.history import ReplayCall, ReplayRun  # noqa: E402
from hermes_tuner.reducers import apply_reducer, validate_spec  # noqa: E402
from hermes_tuner.replay import search_reducer  # noqa: E402

GMAIL_GET = "mcp__google__get_gmail_message_content"
CAL = "cc68e24f21cd"
_TRACK = "https://click.example-mail.com/ls/click?upn=" + "a1b2c3d4" * 20


def _live(text):
    return json.dumps({"result": text, "structuredContent": {"result": text}}, ensure_ascii=False)


def _single(text):
    # One copy of the text plus a small id, so dropping "result" really cuts bytes.
    return json.dumps({"result": text, "id": "msg-0001"}, ensure_ascii=False)


def _email(i, schemeless=False):
    subject = f"Board games night number {i} at the Crown"
    zoom = f"https://us02web.zoom.us/j/88{i:08d}"
    event = f"https://tickets.example-venue.org/e/{42 + i}"
    html = (f"<html><head><style>{'.x{color:red} ' * 40}</style></head><body>"
            f"<h1>{subject}</h1><div><p>Hello, join us this week.</p>"
            f"<p>Join on Zoom: {zoom}</p><p>Tickets: {event}</p>"
            + "".join(f"<p>Read more: {_TRACK}{i}{k}</p>" for k in range(5))
            + "<p>" + "Some news text here. " * 30 + "</p>"
            "<footer><p>Unsubscribe here</p><p>Privacy policy</p></footer></body></html>")
    quoted = event.split("://", 1)[1] if schemeless else event
    return html, (subject, zoom, quoted)


def _newsletter(i):
    return (f"Weekly digest {i}\n" + "".join(f"Story {k}: {_TRACK}n{i}{k}\n" for k in range(4))
            + "Plain words for the digest. " * 20)


def _call(pos, text, name=GMAIL_GET, later_turns=3):
    return ReplayCall(pos=pos, call_id=f"c{pos}", name=name, args={}, text=text,
                      chars=len(text), turn=pos, later_turns=later_turns, ref=None)


def _run(i, calls, said):
    return ReplayRun(session_id=f"cron_{CAL}_{i}", started_at=float(i), calls=tuple(calls),
                     said=tuple(said))


def _gmail_runs(n=4, wrap=_live, schemeless=False):
    runs = []
    for i in range(n):
        html, quoted = _email(i, schemeless)
        calls = [_call(1, wrap(html)), _call(2, wrap(_newsletter(i)))]
        runs.append(_run(i, calls, [(3, q) for q in quoted]))
    return runs


def _all_calls(runs, tool=GMAIL_GET):
    return [c for r in runs for c in r.calls if c.name == tool]


def test_search_cuts_and_keeps_used():
    runs = _gmail_runs()
    r = search_reducer(runs, GMAIL_GET, CAL, exclude=[], pricing=NOUS_V4_PRO)
    assert r.reason == "ok" and r.used_kept == r.used_total > 0
    assert r.bytes_after <= 0.5 * r.bytes_before        # the duplicate alone is half
    steps = r.spec["steps"]
    assert {"op": "drop_fields", "fields": ["structuredContent"]} in steps
    ops = [s["op"] for s in steps]
    assert "html_to_text" in ops and "shorten_urls" in ops
    keep = next(s for s in steps if s["op"] == "shorten_urls")["keep_hosts"]
    assert "tickets.example-venue.org" in keep           # a used non-meeting host is kept
    assert keep == sorted(keep)
    # drop_footer would cut a little more, but under REDUCER_STEP_MIN: the search stops.
    more = {**r.spec, "steps": steps + [{"op": "drop_footer"}]}
    extra = r.bytes_after - sum(len(apply_reducer(more, c.text)) for c in _all_calls(runs))
    assert 0 < extra < policy.REDUCER_STEP_MIN * r.bytes_before and "drop_footer" not in ops
    assert r.spec["tool"] == GMAIL_GET and r.spec["job"] == CAL and validate_spec(r.spec) == []
    calls = _all_calls(runs)
    assert (r.sessions, r.results) == (4, 8)
    assert r.bytes_before == sum(len(c.text) for c in calls)
    assert r.bytes_after == sum(len(apply_reducer(r.spec, c.text)) for c in calls)
    saving = sum(carried_usd(c.chars, c.later_turns, NOUS_V4_PRO)
                 - carried_usd(len(apply_reducer(r.spec, c.text)), c.later_turns, NOUS_V4_PRO)
                 for c in calls)
    assert r.est_saving_per_run == pytest.approx(saving / 4) and r.est_saving_per_run > 0
    per = [(c, used_strings(c.text, [t for p, t in run.said if p > c.pos], []))
           for run in runs for c in run.calls]
    bp = boilerplate(set().union(*(u for _, u in per)), [c.text for c in calls])
    assert "https://" in bp                              # in every result: needn't survive
    need = [(c, u - bp) for c, u in per]
    assert sum(len(u) for _, u in need) == r.used_total
    for c, u in need:                                    # every quoted string is still there
        assert all(survives(s, apply_reducer(r.spec, c.text)) for s in u)


def test_search_keeps_host_of_schemeless_used_url():
    # The model quoted "tickets.example-venue.org/e/42" without its scheme.
    r = search_reducer(_gmail_runs(schemeless=True), GMAIL_GET, CAL, [], NOUS_V4_PRO)
    keep = next(s for s in r.spec["steps"] if s["op"] == "shorten_urls")["keep_hosts"]
    assert "tickets.example-venue.org" in keep


def test_search_never_drops_the_only_copy():
    # results {"result": text, "id": ...}: dropping "result" would lose every used string
    single_copy_runs = _gmail_runs(wrap=_single)
    r = search_reducer(single_copy_runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.reason == "ok"
    assert all(s["op"] != "drop_fields" for s in r.spec["steps"])
    assert r.used_kept == r.used_total > 0


_FILL = "lorem ipsum dolor sit amet " * 1000


def test_search_refuses_cap_that_cuts_used_text():
    runs = []
    for i in range(3):
        quote = f"Quarterly review number {i} with Priya Raman"
        text = _FILL[:9000] + " " + quote + " " + _FILL[:15000]
        assert text.index(quote) == 9001
        runs.append(_run(i, [_call(1, _live(text))], [(2, quote)]))
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.reason == "ok" and r.used_kept == r.used_total == 3
    caps = [s["chars"] for s in r.spec["steps"] if s["op"] == "cap"]
    assert caps == [16000]                    # 8000 and below would cut the quote


def test_search_no_runs():   # Review Focus 4
    r = search_reducer([], GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.reason == "no_runs" and r.spec is None
    assert (r.sessions, r.results, r.bytes_before, r.bytes_after) == (0, 0, 0, 0)
    assert r.est_saving_per_run == 0.0
    other = [_run(0, [_call(1, _live("x" * 500), name="mcp__google__get_events")], [])]
    assert search_reducer(other, GMAIL_GET, CAL, [], NOUS_V4_PRO).reason == "no_runs"


def test_search_too_few_sessions():
    runs = _gmail_runs(n=2)
    assert policy.REPLAY_MIN_SESSIONS == 3
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.reason == "too_few_sessions" and r.spec is None
    assert (r.sessions, r.results) == (2, 4)
    assert r.bytes_before == sum(len(c.text) for c in _all_calls(runs))


def test_search_cut_below_min():
    # Short plain single-copy text: nothing worth cutting.
    runs = [_run(i, [_call(1, json.dumps({"result": f"Note {i}: dentist on Friday at noon"}))],
                 [(2, "dentist on Friday")]) for i in range(3)]
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.reason == "cut_below_min" and r.spec is None
    assert r.bytes_after == r.bytes_before > 0
    assert policy.REDUCER_MIN_CUT == 0.15 and policy.REDUCER_STEP_MIN == 0.02


def test_search_counts_runs_with_a_result_as_sessions():
    runs = _gmail_runs(n=3) + [_run(9, [_call(1, "x" * 50, name="mcp__google__get_events")], [])]
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert (r.sessions, r.results, r.reason) == (3, 6, "ok")


def test_search_is_deterministic():
    runs = _gmail_runs()
    a = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    b = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    c = search_reducer(list(reversed(runs)), GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert a == b and a.spec == c.spec


def test_search_ignores_text_said_before_the_call():
    # The quote was said before the call (pos 0 < 1): not a used string, so the
    # smallest cap is free to cut it.
    runs = []
    for i in range(3):
        quote = f"Quarterly review number {i} with Priya Raman"
        text = _FILL[:9000] + " " + quote + " " + _FILL[:15000]
        runs.append(_run(i, [_call(1, _live(text))], [(0, quote)]))
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.used_total == 0
    assert [s["chars"] for s in r.spec["steps"] if s["op"] == "cap"] == [2000]


def test_search_merges_dropped_fields_into_one_step():
    runs = []
    for i in range(3):
        html, quoted = _email(i)
        text = json.dumps({"result": html, "structuredContent": {"result": html},
                           "raw": "QUJDRA" * 400}, ensure_ascii=False)
        runs.append(_run(i, [_call(1, text)], [(2, q) for q in quoted]))
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    drops = [s for s in r.spec["steps"] if s["op"] == "drop_fields"]
    assert drops == [{"op": "drop_fields", "fields": ["raw", "structuredContent"]}]
    assert validate_spec(r.spec) == []


def test_search_tie_goes_to_first_listed_key():
    # "b" and "a" hold copies of equal length: dropping either cuts the same bytes.
    runs = []
    for i in range(3):
        quote = f"Quarterly review number {i} with Priya Raman"
        text = quote + " " + _FILL[:3000]
        runs.append(_run(i, [_call(1, json.dumps({"b": text, "a": text}))], [(2, quote)]))
    r = search_reducer(runs, GMAIL_GET, CAL, [], NOUS_V4_PRO)
    assert r.spec["steps"][0] == {"op": "drop_fields", "fields": ["a"]}


# --- composed-tool replay (Task 9) ---------------------------------------------------

import re as _re  # noqa: E402

from factory import FLAT  # noqa: E402
from hermes_tuner.replay import (ComposedReplay, LIMIT_PREFIXES, composed_gate,  # noqa: E402
                                 composed_monthly, limit_hit, replay_composed)
from test_relay import RUNNER, relay  # noqa: E402

EMAIL = "owner@example.test"
BATCH = {
    "name": "tuned_fetch", "description": "fetch messages", "mutating": False,
    "parameters": {"type": "object", "properties": {"message_ids": {"type": "array"}}},
    "calls": [GMAIL_GET], "job_prompt": None,
    "replay_inputs": [{"param": "message_ids", "from_call": GMAIL_GET, "arg": "message_id",
                       "collect": "list"}],
    "program": (f"def main(args, call):\n"
                f"    return [call({GMAIL_GET!r}, {{'message_id': m}}) for m in args['message_ids']]\n"),
}


def _mail_run(i, n=8, chars=7000, later_turns=3, started=None):
    """A run of `n` recorded fetches whose args carry a key the program never sends."""
    calls = tuple(
        ReplayCall(pos=k + 1, call_id=f"c{i}-{k}", name=GMAIL_GET,
                   args={"message_id": f"m{i}-{k}", "user_google_email": EMAIL},
                   text=(f"mail {i}-{k} " + "z" * chars)[:chars], chars=chars, turn=1,
                   later_turns=later_turns, ref=f"m{i}-{k}")
        for k in range(n))
    return ReplayRun(f"cron_x_{i}", 1000.0 * i if started is None else started, calls, ())


def _argv(name):
    return RUNNER


def test_replay_composed_hits_output_cap():
    """Acceptance check 3 on synthetic data: 8 x 7,000-char mails batched exceed the
    50,000-byte cap. Catches: an exact-args-only stub (the program sends no
    user_google_email, so every run would be skipped as too few sessions)."""
    runs = [_mail_run(i) for i in range(3)]
    rep = replay_composed(BATCH, runs, relay().run_program, pricing=FLAT, argv_for=_argv)
    assert rep.limit_hits >= 1 and rep.reason == "limit_hit"
    assert rep.sessions == 1                       # stops at the first hit
    assert composed_gate(rep, 10.0) == "replay: limit hit in 1 of 1 sessions"


def test_replay_composed_under_the_cap_replays_every_run_by_ref():
    """The same program on 4 small mails per run fits: 3 sessions, no hits, no errors.
    Catches: the ref fallback missing (sessions 0) and a positive saving lost."""
    runs = [_mail_run(i, n=4, chars=500) for i in range(3)]
    count = (f"def main(args, call):\n"
             f"    return len([call({GMAIL_GET!r}, {{'message_id': m}}) for m in args['message_ids']])\n")
    rep = replay_composed({**BATCH, "program": count}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert (rep.sessions, rep.limit_hits, rep.errors, rep.reason) == (3, 0, 0, "ok")
    assert rep.in_bytes == 3 * 4 * 500
    assert rep.out_bytes == 3 * len(json.dumps({"result": 4}))
    want = (carried_usd(4 * 500, 3, FLAT) - carried_usd(len(json.dumps({"result": 4})), 3, FLAT))
    assert rep.saving_per_run == pytest.approx(want)
    assert rep.eligible == 3


LABELS = "mcp__google__modify_gmail_message_labels"
LABEL_BATCH = {
    "name": "tuned_label", "description": "label messages", "mutating": True,
    "parameters": {"type": "object", "properties": {"message_ids": {"type": "array"}}},
    "calls": [LABELS], "job_prompt": None,
    "replay_inputs": [{"param": "message_ids", "from_call": LABELS, "arg": "message_id",
                       "collect": "list"}],
    "program": (f"def main(args, call):\n"
                f"    return len([call({LABELS!r}, {{'message_id': m, 'add_label_ids': ['STARRED']}})\n"
                f"                for m in args['message_ids']])\n"),
}


def _label_run(i, n=5, chars=300):
    """A run of `n` recorded label calls (made-up text), one per later turn."""
    calls = tuple(
        ReplayCall(pos=k + 1, call_id=f"l{i}-{k}", name=LABELS,
                   args={"message_id": f"m{i}-{k}", "add_label_ids": ["STARRED"],
                         "user_google_email": EMAIL},
                   text=(f"labelled {i}-{k} " + "q" * chars)[:chars], chars=chars, turn=k + 1,
                   later_turns=3, ref=f"m{i}-{k}")
        for k in range(n))
    return ReplayRun(f"cron_x_{i}", 1000.0 * i, calls, ())


def test_replay_composed_counts_mutating_calls_by_their_dry_run_intents():
    """A tool that only calls a mutating tool: the relay records each call as a dry-run
    intent and never asks the stub. An intent for the same tool and ref as a replaced
    call serves it, so the runs count and the saving gate is reached. Catches: replay
    ignoring intents (sessions 0, "too few sessions")."""
    runs = [_label_run(i) for i in range(4)]
    rep = replay_composed(LABEL_BATCH, runs, relay().run_program, pricing=FLAT,
                          argv_for=_argv)
    assert (rep.sessions, rep.limit_hits, rep.errors, rep.reason) == (4, 0, 0, "ok")
    assert rep.in_bytes == 4 * 5 * 300
    out = json.dumps({"result": 5})
    assert rep.out_bytes == 4 * len(out)
    want = carried_usd(5 * 300, 3, FLAT) - carried_usd(len(out), 3, FLAT)
    assert rep.saving_per_run == pytest.approx(want)
    assert composed_gate(rep, composed_monthly(rep, 1.0)) is None
    assert composed_gate(rep, 0.0) == "replay: saving below floor"


def test_replay_intent_for_an_unrecorded_ref_serves_nothing():
    """An intent whose ref matches no replaced call of that tool serves nothing."""
    prog = (f"def main(args, call):\n"
            f"    return call({LABELS!r}, {{'message_id': 'never-recorded'}})\n")
    runs = [_label_run(i, n=2) for i in range(3)]
    rep = replay_composed({**LABEL_BATCH, "program": prog}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert (rep.sessions, rep.reason) == (0, "too_few_sessions")


def _fake(out):
    seen = []

    def run_program(program, args, call, runner_argv, **kw):
        seen.append((args, runner_argv, kw))
        call(GMAIL_GET, {"message_id": args["message_ids"][0]})  # served: a session
        return out
    return run_program, seen


def test_replay_composed_bounded():
    """10 replayable runs: the newest REPLAY_MAX_SESSIONS only, each with its own
    container name, the replay timeout, a dry run and only the payload's tools."""
    assert (policy.REPLAY_MAX_SESSIONS, policy.REPLAY_TIMEOUT_S) == (6, 60)
    runs = [_mail_run(i, n=2, chars=100) for i in range(10)]
    run_program, seen = _fake(json.dumps({"result": 1}))
    names = []
    rep = replay_composed(BATCH, runs, run_program, pricing=FLAT,
                          argv_for=lambda name: names.append(name) or ["argv", name])
    assert len(seen) == policy.REPLAY_MAX_SESSIONS == rep.sessions
    assert rep.eligible == 10
    assert [a["message_ids"] for a, _r, _k in seen] == [[f"m{i}-0", f"m{i}-1"]
                                                         for i in range(9, 3, -1)]
    assert all(kw["timeout_s"] == policy.REPLAY_TIMEOUT_S for _a, _r, kw in seen)
    assert all(kw["dry_run"] is True and kw["intents"] == [] for _a, _r, kw in seen)
    assert all(kw["allowed"] == frozenset({GMAIL_GET}) for _a, _r, kw in seen)
    assert all(kw["max_output"] == policy.MAX_OUTPUT_BYTES and kw["max_calls"] == policy.MAX_CALLS
               for _a, _r, kw in seen)
    assert len(set(names)) == 6 and all(_re.fullmatch(r"tuner-replay-[0-9a-f]{8}", n) for n in names)
    assert [kw["container_name"] for _a, _r, kw in seen] == names
    assert [r for _a, r, _k in seen] == [["argv", n] for n in names]

    hit, seen = _fake(json.dumps({"error": "output exceeds 50000 bytes"}))
    rep = replay_composed(BATCH, runs, hit, pricing=FLAT, argv_for=_argv)
    assert len(seen) == 1 and (rep.sessions, rep.limit_hits) == (1, 1)


def test_replay_skips_runs_without_the_tool_or_a_first_input():
    """A run with no call to the payload's tools is not replayable; nor is one where a
    "first" input has no value. Neither is counted as a session."""
    other = ReplayRun("cron_x_other", 99_999.0,
                      (ReplayCall(1, "o", "web_search", {"query": "q"}, "t", 1, 1, 1, "q"),), ())
    first = {**BATCH, "replay_inputs": [{"param": "message_ids", "from_call": GMAIL_GET,
                                         "arg": "thread_id", "collect": "first"}]}
    run_program, seen = _fake(json.dumps({"result": 1}))
    rep = replay_composed(first, [other, _mail_run(1)], run_program, pricing=FLAT, argv_for=_argv)
    assert seen == [] and rep.sessions == 0 and rep.reason == "too_few_sessions"
    rep = replay_composed(BATCH, [other] + [_mail_run(i, n=1) for i in range(3)], run_program,
                          pricing=FLAT, argv_for=_argv)
    assert len(seen) == 3 and rep.eligible == 3
    assert all(a == {"message_ids": [f"m{i}-0"]} for (a, _r, _k), i in zip(seen, (2, 1, 0)))


def test_replay_stub_miss_skips_the_run():
    """A program asking for a call that was never recorded: that run is skipped, not
    counted as an error."""
    prog = (f"def main(args, call):\n"
            f"    return call({GMAIL_GET!r}, {{'message_id': 'never-recorded'}})\n")
    runs = [_mail_run(i, n=1, chars=50) for i in range(3)]
    rep = replay_composed({**BATCH, "program": prog}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert (rep.sessions, rep.errors, rep.limit_hits) == (0, 0, 0)
    assert rep.reason == "too_few_sessions"


def test_replay_exact_args_lookup_comes_first():
    """Two recorded calls share a ref; the exact (name, args) one is returned."""
    a = ReplayCall(1, "a", GMAIL_GET, {"message_id": "m", "format": "full"}, "FULL", 4, 1, 3, "m")
    b = ReplayCall(2, "b", GMAIL_GET, {"message_id": "m", "format": "min"}, "MIN", 3, 1, 3, "m")
    prog = (f"def main(args, call):\n"
            f"    return [call({GMAIL_GET!r}, {{'message_id': 'm', 'format': 'min'}}),\n"
            f"            call({GMAIL_GET!r}, {{'message_id': 'm'}})]\n")
    run_program = relay().run_program
    outs = []

    def spy(*a_, **kw):
        outs.append(run_program(*a_, **kw))
        return outs[-1]
    runs = [ReplayRun(f"r{i}", float(i), (a, b), ()) for i in range(3)]
    rep = replay_composed({**BATCH, "program": prog}, runs, spy, pricing=FLAT, argv_for=_argv)
    assert rep.sessions == 3 and rep.errors == 0
    assert json.loads(outs[0]) == {"result": ["MIN", "FULL"]}


def test_replay_program_error_is_counted():
    prog = (f"def main(args, call):\n    call({GMAIL_GET!r}, {{'message_id': args['message_ids'][0]}})\n"
            f"    raise RuntimeError('boom')\n")
    runs = [_mail_run(i, n=1, chars=50) for i in range(3)]
    rep = replay_composed({**BATCH, "program": prog}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert (rep.sessions, rep.errors, rep.limit_hits, rep.reason) == (3, 3, 0, "program_error")
    assert composed_gate(rep, 10.0) == "replay: program error in 3 of 3 sessions"
    # An {"error": ...} returned as the result is a failure too (trial.call_failed).
    prog = (f"def main(args, call):\n    call({GMAIL_GET!r}, {{'message_id': args['message_ids'][0]}})\n"
            f"    return {{'error': 'no'}}\n")
    rep = replay_composed({**BATCH, "program": prog}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert rep.errors == 3


def test_limit_messages_match_the_relay():
    """Pins LIMIT_PREFIXES to the messages relay.run_program actually returns."""
    assert LIMIT_PREFIXES == ("output exceeds", "tool call limit", "program timed out")
    run = relay().run_program
    big = run("def main(args, call):\n    return 'x' * 60000\n", {}, lambda n, a: "r", RUNNER,
              dry_run=True, intents=[])
    many = run(f"def main(args, call):\n    return [call({GMAIL_GET!r}, {{}}) for _ in range(60)]\n",
               {}, lambda n, a: "r", RUNNER, dry_run=True, intents=[])
    slow = run("import time\ndef main(args, call):\n    time.sleep(30)\n", {}, lambda n, a: "r",
               RUNNER, dry_run=True, intents=[], timeout_s=1)
    for out in (big, many, slow):
        assert limit_hit(out), out
    assert not limit_hit(json.dumps({"error": "boom"}))
    assert not limit_hit(json.dumps({"result": "output exceeds"}))
    assert not limit_hit("not json")


def test_composed_estimate_units():
    """Output as large as its inputs, a sequence that saves no turns: the estimate is
    at most 0 and the gate rejects below MIN_DRAFT_MONTHLY_USD."""
    echo = (f"def main(args, call):\n"
            f"    return [call({GMAIL_GET!r}, {{'message_id': m}}) for m in args['message_ids']]\n")
    runs = [_mail_run(i, n=2, chars=1000) for i in range(4)]
    rep = replay_composed({**BATCH, "program": echo}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert (rep.sessions, rep.errors, rep.limit_hits) == (4, 0, 0)
    assert rep.out_bytes >= rep.in_bytes
    monthly = composed_monthly(rep, 0.0)
    assert monthly <= 0
    assert composed_gate(rep, monthly) == "replay: saving below floor"


def test_composed_monthly_formula():
    """mean saving per replayed run x runs with a replaced call in the 28-day window x
    30/28, plus the turns part (already monthly)."""
    rep = ComposedReplay(sessions=3, limit_hits=0, errors=0, in_bytes=0, out_bytes=0,
                         reason="ok", eligible=28, saving_per_run=0.01)
    assert composed_monthly(rep, 0.1) == pytest.approx(0.01 * 28 * 30 / 28 + 0.1)
    assert composed_monthly(rep, 0.0) == pytest.approx(0.30)


@pytest.mark.parametrize("sessions,monthly,want", [
    (2, 10.0, "replay: too few sessions (2)"),
    (3, policy.MIN_DRAFT_MONTHLY_USD, "replay: saving below floor"),
    (3, policy.MIN_DRAFT_MONTHLY_USD + 1e-9, None),
])
def test_composed_gate_boundaries(sessions, monthly, want):
    assert policy.REPLAY_MIN_SESSIONS == 3
    rep = ComposedReplay(sessions, 0, 0, 0, 0, "ok", 3, 0.0)
    assert composed_gate(rep, monthly) == want


def test_composed_gate_order():
    """limit hit before error before session count before the floor."""
    rep = ComposedReplay(1, 1, 1, 0, 0, "limit_hit", 3, -1.0)
    assert composed_gate(rep, -1.0) == "replay: limit hit in 1 of 1 sessions"
    rep = ComposedReplay(2, 0, 1, 0, 0, "program_error", 3, -1.0)
    assert composed_gate(rep, -1.0) == "replay: program error in 1 of 2 sessions"


# --- Task 9 fix round 1 ---------------------------------------------------------------

def test_replay_wrong_arg_is_not_replayable():
    """Reviewer's probe: arg names a key the recorded calls lack, so the program gets []
    and makes no calls. Catches: an empty list input counted as a replayed session with
    the full saving (it passed the gate at 0.1134/month)."""
    wrong = {**BATCH, "replay_inputs": [{**BATCH["replay_inputs"][0], "arg": "id"}]}
    runs = [_mail_run(i) for i in range(6)]
    rep = replay_composed(wrong, runs, relay().run_program, pricing=FLAT, argv_for=_argv)
    assert rep.sessions == 0 and rep.reason == "too_few_sessions"
    assert composed_gate(rep, composed_monthly(rep, 0.0)) == "replay: too few sessions (0)"
    started = []
    replay_composed(wrong, runs, lambda *a, **k: started.append(1) or json.dumps({"result": 1}),
                    pricing=FLAT, argv_for=_argv)
    assert started == []          # rule (a): an empty list input is not replayable at all


def test_replay_run_that_serves_no_call_is_not_a_session():
    """Rule (b), apart from (a): the input is non-empty but the program calls nothing."""
    lazy = {**BATCH, "program": "def main(args, call):\n    return 1\n"}
    runs = [_mail_run(i, n=2, chars=100) for i in range(3)]
    rep = replay_composed(lazy, runs, relay().run_program, pricing=FLAT, argv_for=_argv)
    assert rep.sessions == 0 and rep.reason == "too_few_sessions"


def test_replay_saving_counts_only_the_served_calls():
    """A program that reads 2 of 8 recorded calls is credited with those 2 only."""
    two = (f"def main(args, call):\n"
           f"    return len([call({GMAIL_GET!r}, {{'message_id': m}}) for m in args['message_ids'][:2]])\n")
    runs = [_mail_run(i, n=8, chars=500) for i in range(3)]
    rep = replay_composed({**BATCH, "program": two}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    out = len(json.dumps({"result": 2}))
    assert rep.sessions == 3
    assert rep.saving_per_run == pytest.approx(2 * carried_usd(500, 3, FLAT) - carried_usd(out, 3, FLAT))
    assert rep.in_bytes == 3 * 2 * 500


def test_replay_limit_hit_counts_even_after_a_missed_lookup():
    """A run that missed one lookup and then hit the output cap is still a hit."""
    prog = (f"def main(args, call):\n"
            f"    call({GMAIL_GET!r}, {{'message_id': 'never-recorded'}})\n"
            f"    return [call({GMAIL_GET!r}, {{'message_id': m}}) for m in args['message_ids']]\n")
    runs = [_mail_run(i) for i in range(3)]
    rep = replay_composed({**BATCH, "program": prog}, runs, relay().run_program,
                          pricing=FLAT, argv_for=_argv)
    assert (rep.sessions, rep.limit_hits, rep.reason) == (1, 1, "limit_hit")


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_replay_budget_stops_runs_and_says_no_time():
    """Runs start only while REPLAY_TIMEOUT_S + 30 s fit in the budget left."""
    assert policy.REPLAY_CYCLE_BUDGET_S == 360
    clock = _Clock()
    seen = []

    def slow(program, args, call, runner_argv, **kw):
        seen.append(1)
        clock.t += 70
        call(GMAIL_GET, {"message_id": args["message_ids"][0]})
        return json.dumps({"result": 1})
    runs = [_mail_run(i, n=1, chars=100) for i in range(6)]
    # 360 -> 290 -> 220 -> 150 -> 80 left: the 5th run (needs 90) does not start.
    rep = replay_composed(BATCH, runs, slow, pricing=FLAT, argv_for=_argv, clock=clock,
                          budget_s=360)
    assert len(seen) == 4 and rep.sessions == 4 and rep.out_of_time
    assert rep.elapsed_s == pytest.approx(280) and rep.reason == "ok"
    seen.clear()
    rep = replay_composed(BATCH, runs, slow, pricing=FLAT, argv_for=_argv, clock=clock,
                          budget_s=360 - 280)
    assert seen == [] and rep.sessions == 0 and rep.reason == "no_time"
    assert composed_gate(rep, 10.0) == "replay: no time left"
    rep = replay_composed(BATCH, runs, slow, pricing=FLAT, argv_for=_argv, clock=clock,
                          budget_s=policy.REPLAY_TIMEOUT_S + 30)
    assert len(seen) == 1                       # exactly enough left: one run starts


def test_no_time_left_only_below_min_sessions():
    rep = ComposedReplay(3, 0, 0, 0, 0, "ok", 6, 1.0, out_of_time=True, elapsed_s=300)
    assert composed_gate(rep, 10.0) is None
    rep = ComposedReplay(1, 1, 0, 0, 0, "limit_hit", 6, 1.0, out_of_time=True)
    assert composed_gate(rep, 10.0) == "replay: limit hit in 1 of 1 sessions"
