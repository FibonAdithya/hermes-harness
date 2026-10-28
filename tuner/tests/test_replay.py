import json
import random
import time

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
    assert got == frozenset({"dentist appointment", " 12 high street"})


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
    return out


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


def test_boilerplate_strictly_more_than_half():
    a, b, c = "unsubscribe here", "view in browser", "dentist appointment"
    per = [frozenset({a, b}), frozenset({a, b}), frozenset({a, c}), frozenset()]
    assert boilerplate(per) == frozenset({a})          # b is in exactly half: kept
    assert boilerplate([]) == frozenset()
    assert boilerplate([frozenset({c})]) == frozenset({c})
    assert policy.BOILERPLATE_SHARE == 0.5
