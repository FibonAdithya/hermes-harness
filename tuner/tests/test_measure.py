import pytest
from hermes_tuner.measure import chat_stats, job_stats, weekly_spend
from hermes_tuner.model import Session, ToolCall


def _call(i=0, chars=10):
    return ToolCall(call_id=f"c{i}", name="t", arg_keys=(), ref=None, result_chars=chars)


def _s(sid, job="j1", cost=0.1, n_calls=1, source="cron", prompt=100, chars=10):
    return Session(id=sid, source=source, job_id=job, started_at=1.0, est_cost=cost,
                   prompt_tokens=prompt, output_tokens=1,
                   calls=tuple(_call(i, chars) for i in range(n_calls)))


def test_median_not_mean():
    ss = [_s("a", cost=0.1), _s("b", cost=0.1), _s("c", cost=5.0)]
    assert job_stats(ss, "j1").median_cost == 0.1


def test_only_that_jobs_sessions_count():
    ss = [_s("a", job="j1"), _s("b", job="j2"), _s("c", job="j1"), _s("d", job="j2"), _s("e", job="j2")]
    assert job_stats(ss, "j1").runs == 2
    assert job_stats(ss, "j2").runs == 3
    assert job_stats(ss, "none") is None


def test_failed_runs_with_zero_calls_are_excluded():
    ss = [_s("a", cost=9.0, n_calls=0), _s("b", cost=0.2, n_calls=1)]
    st = job_stats(ss, "j1")
    assert st.runs == 1 and st.median_cost == 0.2
    assert job_stats([_s("a", n_calls=0)], "j1") is None


def test_other_medians():
    ss = [_s("a", n_calls=1, prompt=10, chars=5), _s("b", n_calls=3, prompt=30, chars=7),
          _s("c", n_calls=5, prompt=20, chars=9)]
    st = job_stats(ss, "j1")
    assert st.median_tool_calls == 3
    assert st.median_prompt_tokens == 20
    assert st.median_result_chars == 21  # per-run sums 5, 21, 45


def test_chat_stats_uses_telegram_source_and_excludes_zero_calls():
    ss = [_s("a", job=None, source="telegram", cost=0.3), _s("b", job=None, source="telegram", n_calls=0),
          _s("c", source="cron")]
    assert chat_stats(ss).runs == 1 and chat_stats(ss).median_cost == 0.3
    assert chat_stats([_s("c", source="cron")]) is None


def test_weekly_spend_sums_selected_jobs_including_zero_call_runs():
    ss = [_s("a", job="j1", cost=1.0), _s("b", job="j1", cost=2.0, n_calls=0),
          _s("c", job="j2", cost=4.0), _s("d", job="j3", cost=8.0)]
    assert weekly_spend(ss, {"j1", "j2"}) == 7.0
    assert weekly_spend(ss, set()) == 0.0


def test_run_cost_prefers_estimate_else_tokens():
    from hermes_tuner import policy
    from hermes_tuner.measure import run_cost
    s = _s("a", cost=0.25)
    assert run_cost(s) == 0.25
    s0 = Session(id="b", source="cron", job_id="j", started_at=1.0, est_cost=0.0,
                 prompt_tokens=600_000, output_tokens=400_000, calls=())
    assert run_cost(s0) == pytest.approx(policy.FALLBACK_USD_PER_MTOK)
