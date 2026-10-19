from __future__ import annotations

from hermes_tuner.detect import Candidate, detect
from hermes_tuner.model import Session, ToolCall

DAY = 86400.0
NOW = 100 * DAY
GET = "mcp__google__get_gmail_message_content"
SEARCH = "mcp__google__search_gmail_messages"
MODIFY = "mcp__google__modify_gmail_message_labels"


def call(i, name, chars=10, ref=None):
    return ToolCall(call_id=f"c{i}", name=name, arg_keys=("k",), ref=ref, result_chars=chars)


def sess(sid, calls, job=None, age_days=1.0, cost=0.1, source="cron"):
    return Session(id=sid, source=source, job_id=job, started_at=NOW - age_days * DAY,
                   est_cost=cost, prompt_tokens=100, output_tokens=1, calls=tuple(calls))


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
    top = detect(briefing_runs() + web_chats(), NOW)[0]
    assert top.kind == "sequence"
    assert top.job_id == "3bc4e11d29b1"
    assert GET in top.key
    assert top.est_monthly_usd > 0


def test_web_ngram_is_found_but_ranked_below():
    cands = detect(briefing_runs() + web_chats(), NOW)
    web = [c for c in cands if c.kind == "sequence" and c.job_id is None]
    assert len(web) == 1 and web[0].key == "seq:web_search>web_search>web_search"
    assert web[0].sessions == 12 and web[0].occurrences == 12
    assert cands.index(web[0]) > 0


def test_sorted_by_money_descending():
    cands = detect(briefing_runs() + web_chats(), NOW)
    usd = [c.est_monthly_usd for c in cands]
    assert usd == sorted(usd, reverse=True)


def test_longest_ngram_kept_among_identical_session_sets():
    cands = [c for c in detect(briefing_runs(), NOW) if c.kind == "sequence"]
    assert len(cands) == 1
    assert cands[0].key == "seq:" + ">".join([GET] * 5)
    assert cands[0].sessions == 12
    # 20 gets give 16 overlapping 5-gram matches per run
    assert cands[0].occurrences == 12 * 16
    # each covered call counted once: all 20 gets
    assert cands[0].result_chars == 12 * 20 * 10_000


def test_sequence_money_uses_session_share_scaled_to_30_days():
    c = detect(briefing_runs(), NOW)[0]
    total = 500 + 20 * 10_000 + 20 * 20
    share = 20 * 10_000 / total
    expected = 12 * 0.12 * share * 30 / 28
    assert abs(c.est_monthly_usd - expected) < 1e-9


def test_zero_result_chars_session_contributes_zero():
    ss = [sess(f"z{i}", [call(0, "a", 0), call(1, "b", 0), call(2, "c", 0)], job="j", age_days=i + 1)
          for i in range(10)]
    cands = detect(ss, NOW)
    assert len(cands) == 1 and cands[0].est_monthly_usd == 0.0


def test_reread_found_for_calendar_job():
    runs = []
    for k in range(1, 8):
        cs = [call(i, GET, 1000, ref=f"r{r}") for i, r in enumerate(range(k, k + 7))]
        runs.append(sess(f"c{k}", cs, job="cc68e24f21cd", age_days=10 - k, cost=0.15))
    rr = [c for c in detect(runs, NOW) if c.kind == "reread"]
    assert len(rr) == 1
    assert rr[0].key == "reread:cc68e24f21cd" and rr[0].job_id == "cc68e24f21cd"
    assert rr[0].occurrences == 36
    assert rr[0].result_chars == 36 * 1000
    assert rr[0].sessions == 6


def test_reread_uses_adjacent_runs_not_calendar_days():
    # runs 10 days apart still count as consecutive; a gap run in between breaks it
    a = sess("a", [call(0, GET, 100, ref="x")], job="j", age_days=30 - 20)
    b = sess("b", [call(0, GET, 100, ref="x")], job="j", age_days=5)
    assert [c for c in detect([a, b], NOW) if c.kind == "reread"][0].occurrences == 1
    mid = sess("m", [call(0, GET, 100, ref="y")], job="j", age_days=8)
    assert [c for c in detect([a, mid, b], NOW) if c.kind == "reread"] == []


def test_reread_ignores_other_tools_and_missing_refs():
    runs = [sess(f"r{i}", [call(0, "other", 100, ref="x"), call(1, GET, 100, ref=None)],
                 job="j", age_days=i + 1) for i in range(3)]
    assert [c for c in detect(runs, NOW) if c.kind == "reread"] == []


def schema_sessions(n):
    return [sess(f"s{i}", [call(j, "tool_describe", 500) for j in range(3)], job=None,
                 age_days=i % 20 + 1, source="telegram") for i in range(n)]


def test_schema_lookup_found():
    c = [c for c in detect(schema_sessions(25), NOW) if c.kind == "schema_lookup"]
    assert len(c) == 1
    assert c[0].key == "schema_lookup" and c[0].job_id is None
    assert c[0].sessions == 25 and c[0].result_chars == 25 * 1500


def test_schema_lookup_absent_below_threshold():
    assert [c for c in detect(schema_sessions(19), NOW) if c.kind == "schema_lookup"] == []


def test_schema_lookup_needs_two_describes_per_session():
    ss = schema_sessions(19) + [sess("one", [call(0, "tool_describe", 5)], age_days=1)] * 5
    assert [c for c in detect(ss, NOW) if c.kind == "schema_lookup"] == []


def seq_sessions(n):
    return [sess(f"q{i}", [call(0, "a", 100), call(1, "b", 100), call(2, "c", 100)],
                 job="jj", age_days=i + 1) for i in range(n)]


def test_sequence_below_session_threshold_dropped():
    assert [c for c in detect(seq_sessions(9), NOW) if c.kind == "sequence"] == []
    assert len([c for c in detect(seq_sessions(10), NOW) if c.kind == "sequence"]) == 1


def test_sessions_older_than_28_days_ignored():
    old = [sess(f"o{i}", seq_sessions(1)[0].calls, job="jj", age_days=29 + i) for i in range(10)]
    assert detect(old, NOW) == []
    assert len(detect(seq_sessions(5) + [
        sess(f"o{i}", seq_sessions(1)[0].calls, job="jj", age_days=27 + i * 0.01) for i in range(5)],
        NOW)) == 1


def test_window_boundaries_inclusive_and_future_excluded():
    calls = seq_sessions(1)[0].calls
    edge = [sess(f"e{i}", calls, job="jj", age_days=28) for i in range(10)]
    assert len(detect(edge, NOW)) == 1
    future = [sess(f"f{i}", calls, job="jj", age_days=-1) for i in range(10)]
    assert detect(future, NOW) == []


def skill_sessions(n, chars):
    return [sess(f"k{i}", [call(0, "skill_view", chars)], job=None, age_days=i + 1) for i in range(n)]


def test_skill_load_threshold_and_key():
    ss = skill_sessions(10, 21_000)
    names = {s.id: ["gmail-cleanup"] for s in ss}
    c = [c for c in detect(ss, NOW, skill_names=names) if c.kind == "skill_load"]
    assert len(c) == 1 and c[0].key == "skill_load:gmail-cleanup"
    assert c[0].occurrences == 10 and c[0].sessions == 10 and c[0].result_chars == 210_000
    ss = skill_sessions(10, 19_999)
    names = {s.id: ["gmail-cleanup"] for s in ss}
    assert [c for c in detect(ss, NOW, skill_names=names) if c.kind == "skill_load"] == []
    ss = skill_sessions(10, 20_000)  # exactly 200,000 is kept
    names = {s.id: ["x"] for s in ss}
    assert len([c for c in detect(ss, NOW, skill_names=names) if c.kind == "skill_load"]) == 1


def test_skill_load_skipped_without_names():
    assert detect(skill_sessions(10, 50_000), NOW) == []


def test_deterministic():
    ss = briefing_runs() + web_chats()
    assert detect(ss, NOW) == detect(list(reversed(ss)), NOW)


def test_candidate_is_dataclass_with_fields():
    c = Candidate("sequence", "seq:a", None, 1, 1, 1, 0.0)
    assert c.kind == "sequence"


def test_schema_lookup_boundary_exactly_20():
    assert len([c for c in detect(schema_sessions(20), NOW) if c.kind == "schema_lookup"]) == 1


def test_ranked_by_money_not_count():
    # cheap: 20 sessions x 30 tiny calls -> many occurrences; costly: 10 sessions, big results
    cheap = [sess(f"cheap{i}", [call(j, "p", 1) for j in range(30)], job="jc", age_days=i + 1, cost=0.001)
             for i in range(20)]
    costly = [sess(f"big{i}", [call(0, "x", 50_000), call(1, "y", 50_000), call(2, "z", 50_000)],
                   job="jb", age_days=i + 1, cost=0.5) for i in range(10)]
    cands = detect(cheap + costly, NOW)
    assert cands[0].job_id == "jb"
    assert max(cands, key=lambda c: c.occurrences).job_id == "jc"


def test_ngrams_counted_per_job():
    # 6 sessions each in two jobs, same sequence: 12 total but only 6 per job -> none kept
    ss = [sess(f"a{i}", seq_sessions(1)[0].calls, job="ja", age_days=i + 1) for i in range(6)]
    ss += [sess(f"b{i}", seq_sessions(1)[0].calls, job="jb", age_days=i + 1) for i in range(6)]
    assert detect(ss, NOW) == []
