from __future__ import annotations

import pytest

from factory import FLAT, FLAT_RATE
from hermes_tuner.detect import Candidate, detect
from hermes_tuner.measure import priced_cost
from hermes_tuner.model import Session, ToolCall

DAY = 86400.0
NOW = 100 * DAY
GET = "mcp__google__get_gmail_message_content"
SEARCH = "mcp__google__search_gmail_messages"
MODIFY = "mcp__google__modify_gmail_message_labels"


def call(i, name, chars=10, ref=None):
    return ToolCall(call_id=f"c{i}", name=name, arg_keys=("k",), ref=ref, result_chars=chars)


def sess(sid, calls, job=None, age_days=1.0, cost=0.1, source="cron", raw=0.0):
    """`cost` sets the priced cost through the token count; `raw` is the billing figure
    (subscription billing reads $0), which the detector must ignore."""
    tokens = round(cost * 1e6 / FLAT_RATE)  # fresh input, priced at FLAT
    return Session(id=sid, source=source, job_id=job, started_at=NOW - age_days * DAY,
                   est_cost=raw, prompt_tokens=tokens, output_tokens=0, calls=tuple(calls),
                   input_tokens=tokens)


def briefing_runs(n=12, job="3bc4e11d29b1"):
    out = []
    for r in range(n):
        cs = [call(0, SEARCH, 500)]
        cs += [call(1 + i, GET, 10_000, ref=f"run{r}-m{i}") for i in range(20)]
        cs += [call(21 + i, MODIFY, 20) for i in range(20)]
        out.append(sess(f"b{r}", cs, job=job, age_days=r + 1, cost=0.12))
    return out


def web_chats(n=12):
    return [sess(f"w{r}", [call(i, "web_search", 1000) for i in range(3)], job=None,
                 age_days=r + 1, cost=0.01, source="telegram") for r in range(n)]


def test_gmail_loop_found_for_briefing_job():
    top = detect(briefing_runs() + web_chats(), NOW, FLAT)[0]
    assert top.kind == "sequence"
    assert top.job_id == "3bc4e11d29b1"
    assert GET in top.key
    assert top.est_monthly_usd > 0


def test_web_ngram_is_found_but_ranked_below():
    cands = detect(briefing_runs() + web_chats(), NOW, FLAT)
    web = [c for c in cands if c.kind == "sequence" and c.job_id is None]
    assert len(web) == 1 and web[0].key == "seq:web_search>web_search>web_search"
    assert web[0].sessions == 12 and web[0].occurrences == 12
    assert cands.index(web[0]) > 0


def test_sorted_by_money_descending():
    cands = detect(briefing_runs() + web_chats(), NOW, FLAT)
    usd = [c.est_monthly_usd for c in cands]
    assert usd == sorted(usd, reverse=True)


def test_longest_ngram_kept_among_identical_session_sets():
    cands = [c for c in detect(briefing_runs(), NOW, FLAT) if c.kind == "sequence"]
    assert len(cands) == 1
    assert cands[0].key == "seq:" + ">".join([GET] * 5)
    assert cands[0].sessions == 12
    # 20 gets give 16 overlapping 5-gram matches per run
    assert cands[0].occurrences == 12 * 16
    # each covered call counted once: all 20 gets
    assert cands[0].result_chars == 12 * 20 * 10_000


def test_sequence_money_uses_session_share_scaled_to_30_days():
    c = detect(briefing_runs(), NOW, FLAT)[0]
    total = 500 + 20 * 10_000 + 20 * 20
    share = 20 * 10_000 / total
    expected = 12 * priced_cost(briefing_runs()[0], FLAT) * share * 30 / 28
    assert abs(c.est_monthly_usd - expected) < 1e-9
    assert c.est_monthly_usd == pytest.approx(12 * 0.12 * share * 30 / 28, rel=1e-5)


def test_zero_result_chars_candidate_is_skipped():
    ss = [sess(f"z{i}", [call(0, "a", 0), call(1, "b", 0), call(2, "c", 0)], job="j", age_days=i + 1)
          for i in range(10)]
    assert detect(ss, NOW, FLAT) == []


def test_money_is_priced_from_tokens_not_raw_billing():
    """F2: subscription billing reads $0. A candidate is valued on its tokens; one with
    no tokens is worth nothing (and skipped) whatever the raw figure says."""
    billed_zero = briefing_runs()  # raw $0, tokens priced at $0.12 a run
    assert detect(billed_zero, NOW, FLAT)[0].est_monthly_usd > 0
    no_tokens = [sess(s.id, s.calls, job=s.job_id, age_days=(NOW - s.started_at) / DAY,
                      cost=0.0, raw=0.12) for s in billed_zero]
    assert detect(no_tokens, NOW, FLAT) == []


def test_reread_found_for_calendar_job():
    runs = []
    for k in range(1, 8):
        cs = [call(i, GET, 1000, ref=f"r{r}") for i, r in enumerate(range(k, k + 7))]
        runs.append(sess(f"c{k}", cs, job="cc68e24f21cd", age_days=10 - k, cost=0.15))
    rr = [c for c in detect(runs, NOW, FLAT) if c.kind == "reread"]
    assert len(rr) == 1
    assert rr[0].key == "reread:cc68e24f21cd" and rr[0].job_id == "cc68e24f21cd"
    assert rr[0].occurrences == 36
    assert rr[0].result_chars == 36 * 1000
    assert rr[0].sessions == 6


def test_reread_uses_adjacent_runs_not_calendar_days():
    # runs 10 days apart still count as consecutive; a gap run in between breaks it
    a = sess("a", [call(0, GET, 100, ref="x")], job="j", age_days=30 - 20)
    b = sess("b", [call(0, GET, 100, ref="x")], job="j", age_days=5)
    assert [c for c in detect([a, b], NOW, FLAT) if c.kind == "reread"][0].occurrences == 1
    mid = sess("m", [call(0, GET, 100, ref="y")], job="j", age_days=8)
    assert [c for c in detect([a, mid, b], NOW, FLAT) if c.kind == "reread"] == []


def test_reread_ignores_other_tools_and_missing_refs():
    runs = [sess(f"r{i}", [call(0, "other", 100, ref="x"), call(1, GET, 100, ref=None)],
                 job="j", age_days=i + 1) for i in range(3)]
    assert [c for c in detect(runs, NOW, FLAT) if c.kind == "reread"] == []


def schema_sessions(n):
    return [sess(f"s{i}", [call(j, "tool_describe", 500) for j in range(3)], job=None,
                 age_days=i % 20 + 1, source="telegram") for i in range(n)]


def test_schema_lookup_found():
    c = [c for c in detect(schema_sessions(25), NOW, FLAT) if c.kind == "schema_lookup"]
    assert len(c) == 1
    assert c[0].key == "schema_lookup" and c[0].job_id is None
    assert c[0].sessions == 25 and c[0].result_chars == 25 * 1500


def test_schema_lookup_absent_below_threshold():
    assert [c for c in detect(schema_sessions(19), NOW, FLAT) if c.kind == "schema_lookup"] == []


def test_schema_lookup_needs_two_describes_per_session():
    ss = schema_sessions(19) + [sess("one", [call(0, "tool_describe", 5)], age_days=1)] * 5
    assert [c for c in detect(ss, NOW, FLAT) if c.kind == "schema_lookup"] == []


def seq_sessions(n):
    return [sess(f"q{i}", [call(0, "a", 100), call(1, "b", 100), call(2, "c", 100)],
                 job="jj", age_days=i + 1) for i in range(n)]


def test_sequence_below_session_threshold_dropped():
    assert [c for c in detect(seq_sessions(9), NOW, FLAT) if c.kind == "sequence"] == []
    assert len([c for c in detect(seq_sessions(10), NOW, FLAT) if c.kind == "sequence"]) == 1


def test_sessions_older_than_28_days_ignored():
    old = [sess(f"o{i}", seq_sessions(1)[0].calls, job="jj", age_days=29 + i) for i in range(10)]
    assert detect(old, NOW, FLAT) == []
    assert len(detect(seq_sessions(5) + [
        sess(f"o{i}", seq_sessions(1)[0].calls, job="jj", age_days=27 + i * 0.01) for i in range(5)],
        NOW, FLAT)) == 1


def test_window_boundaries_inclusive_and_future_excluded():
    calls = seq_sessions(1)[0].calls
    edge = [sess(f"e{i}", calls, job="jj", age_days=28) for i in range(10)]
    assert len(detect(edge, NOW, FLAT)) == 1
    future = [sess(f"f{i}", calls, job="jj", age_days=-1) for i in range(10)]
    assert detect(future, NOW, FLAT) == []


def skill_sessions(n, chars):
    return [sess(f"k{i}", [call(0, "skill_view", chars)], job=None, age_days=i + 1) for i in range(n)]


def test_skill_load_threshold_and_key():
    ss = skill_sessions(10, 21_000)
    names = {s.id: ["gmail-cleanup"] for s in ss}
    c = [c for c in detect(ss, NOW, FLAT, skill_names=names) if c.kind == "skill_load"]
    assert len(c) == 1 and c[0].key == "skill_load:gmail-cleanup"
    assert c[0].occurrences == 10 and c[0].sessions == 10 and c[0].result_chars == 210_000
    ss = skill_sessions(10, 19_999)
    names = {s.id: ["gmail-cleanup"] for s in ss}
    assert [c for c in detect(ss, NOW, FLAT, skill_names=names) if c.kind == "skill_load"] == []
    ss = skill_sessions(10, 20_000)  # exactly 200,000 is kept
    names = {s.id: ["x"] for s in ss}
    assert len([c for c in detect(ss, NOW, FLAT, skill_names=names) if c.kind == "skill_load"]) == 1


def test_skill_load_skipped_without_names():
    assert detect(skill_sessions(10, 50_000), NOW, FLAT) == []


def test_deterministic():
    ss = briefing_runs() + web_chats()
    assert detect(ss, NOW, FLAT) == detect(list(reversed(ss)), NOW, FLAT)


def test_candidate_is_dataclass_with_fields():
    c = Candidate("sequence", "seq:a", None, 1, 1, 1, 0.0)
    assert c.kind == "sequence"


def test_schema_lookup_boundary_exactly_20():
    assert len([c for c in detect(schema_sessions(20), NOW, FLAT) if c.kind == "schema_lookup"]) == 1


def test_ranked_by_money_not_count():
    # cheap: 20 sessions x 30 tiny calls -> many occurrences; costly: 10 sessions, big results
    cheap = [sess(f"cheap{i}", [call(j, "p", 1) for j in range(30)], job="jc", age_days=i + 1, cost=0.001)
             for i in range(20)]
    costly = [sess(f"big{i}", [call(0, "x", 50_000), call(1, "y", 50_000), call(2, "z", 50_000)],
                   job="jb", age_days=i + 1, cost=0.5) for i in range(10)]
    cands = detect(cheap + costly, NOW, FLAT)
    assert cands[0].job_id == "jb"
    assert max(cands, key=lambda c: c.occurrences).job_id == "jc"


def test_ngrams_counted_per_job():
    # 6 sessions each in two jobs, same sequence: 12 total but only 6 per job -> none kept
    ss = [sess(f"a{i}", seq_sessions(1)[0].calls, job="ja", age_days=i + 1) for i in range(6)]
    ss += [sess(f"b{i}", seq_sessions(1)[0].calls, job="jb", age_days=i + 1) for i in range(6)]
    assert detect(ss, NOW, FLAT) == []


def _skill_run(chars_list, name="s"):
    ss = [sess(f"sk{i}", [call(0, "skill_view", c)], job=None, age_days=i + 1)
          for i, c in enumerate(chars_list)]
    return ss, {s.id: [name] for s in ss}


def test_skill_load_threshold_exact_with_float_roundoff():
    chars = [18_181] * 10 + [18_190]  # 11 loads, sum exactly 200,000
    assert sum(chars) == 200_000
    ss, names = _skill_run(chars)
    assert len([c for c in detect(ss, NOW, FLAT, skill_names=names) if c.kind == "skill_load"]) == 1
    chars[-1] -= 1
    ss, names = _skill_run(chars)
    assert [c for c in detect(ss, NOW, FLAT, skill_names=names) if c.kind == "skill_load"] == []


def test_skill_load_skips_mismatched_or_missing_sessions():
    ok, names = _skill_run([30_000] * 7)
    bad = sess("bad", [call(0, "skill_view", 900_000), call(1, "skill_view", 900_000)], age_days=1)
    gone = sess("gone", [call(0, "skill_view", 900_000)], age_days=2)
    names["bad"] = ["s"]  # one name, two views
    c = [c for c in detect(ok + [bad, gone], NOW, FLAT, skill_names=names) if c.kind == "skill_load"]
    assert len(c) == 1 and c[0].sessions == 7 and c[0].occurrences == 7
    assert c[0].result_chars == 210_000


def test_detector_ranks_by_list_price():
    """Equal result chars in two jobs. A's runs carry 3x B's prompt tokens, almost all
    cache reads; B's are fresh input. At list prices B is dearer and ranks first; a
    flat per-token rate would put A first (so neither a tie nor key order passes)."""
    from factory import FLAT, V4_PRO

    def runs(job, prefix, inp, read):
        out = []
        for r in range(10):
            cs = [call(i, n, 1000) for i, n in enumerate((SEARCH, GET, MODIFY))]
            out.append(Session(id=f"{prefix}{r}", source="cron", job_id=job,
                               started_at=NOW - (r + 1) * DAY, est_cost=0.0,
                               prompt_tokens=inp + read, output_tokens=0, calls=tuple(cs),
                               input_tokens=inp, cache_read_tokens=read))
        return out
    ss = runs("aaaaaaaaaaaa", "a", 1_000, 299_000) + runs("bbbbbbbbbbbb", "b", 100_000, 0)
    listed = [c.job_id for c in detect(ss, NOW, V4_PRO) if c.kind == "sequence"]
    assert listed == ["bbbbbbbbbbbb", "aaaaaaaaaaaa"]
    flat = [c.job_id for c in detect(ss, NOW, FLAT) if c.kind == "sequence"]
    assert flat == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]  # the control: a flat rate inverts it
