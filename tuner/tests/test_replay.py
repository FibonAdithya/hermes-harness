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
