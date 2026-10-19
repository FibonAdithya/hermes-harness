import pytest
from hermes_tuner import policy
from hermes_tuner.measure import chat_stats, job_stats, priced_cost, weekly_spend
from hermes_tuner.model import Session, ToolCall

PER_TOKEN = policy.USD_PER_MTOK / 1e6


def _call(i=0, chars=10):
    return ToolCall(call_id=f"c{i}", name="t", arg_keys=(), ref=None, result_chars=chars)


def _s(sid, job="j1", tokens=1000, n_calls=1, source="cron", prompt=None, chars=10, cost=0.0):
    """`tokens` is prompt + output (output fixed at 1). `cost` is the raw billing
    figure, which the tuner must ignore."""
    prompt = tokens - 1 if prompt is None else prompt
    return Session(id=sid, source=source, job_id=job, started_at=1.0, est_cost=cost,
                   prompt_tokens=prompt, output_tokens=1,
                   calls=tuple(_call(i, chars) for i in range(n_calls)))


def test_priced_cost_is_tokens_times_price_and_ignores_raw_cost():
    s = Session(id="b", source="cron", job_id="j", started_at=1.0, est_cost=7.0,
                prompt_tokens=600_000, output_tokens=400_000, calls=())
    assert priced_cost(s) == pytest.approx(policy.USD_PER_MTOK)
    assert policy.USD_PER_MTOK == 0.42


def test_median_not_mean():
    ss = [_s("a", tokens=1000), _s("b", tokens=1000), _s("c", tokens=50_000)]
    assert job_stats(ss, "j1").median_priced_cost == pytest.approx(1000 * PER_TOKEN)


def test_median_priced_cost_ignores_raw_billing():
    # raw dollars say a is the cheapest; tokens say it is the dearest
    ss = [_s("a", tokens=9000, cost=0.0), _s("b", tokens=1000, cost=5.0),
          _s("c", tokens=2000, cost=5.0)]
    assert job_stats(ss, "j1").median_priced_cost == pytest.approx(2000 * PER_TOKEN)


def test_only_that_jobs_sessions_count():
    ss = [_s("a", job="j1"), _s("b", job="j2"), _s("c", job="j1"), _s("d", job="j2"), _s("e", job="j2")]
    assert job_stats(ss, "j1").runs == 2
    assert job_stats(ss, "j2").runs == 3
    assert job_stats(ss, "none") is None


def test_failed_runs_with_zero_calls_are_excluded():
    ss = [_s("a", tokens=90_000, n_calls=0), _s("b", tokens=2000, n_calls=1)]
    st = job_stats(ss, "j1")
    assert st.runs == 1 and st.median_priced_cost == pytest.approx(2000 * PER_TOKEN)
    assert job_stats([_s("a", n_calls=0)], "j1") is None


def test_other_medians():
    ss = [_s("a", n_calls=1, prompt=10, chars=5), _s("b", n_calls=3, prompt=30, chars=7),
          _s("c", n_calls=5, prompt=20, chars=9)]
    st = job_stats(ss, "j1")
    assert st.median_tool_calls == 3
    assert st.median_prompt_tokens == 20
    assert st.median_result_chars == 21  # per-run sums 5, 21, 45


def test_chat_stats_uses_telegram_source_and_excludes_zero_calls():
    ss = [_s("a", job=None, source="telegram", tokens=3000),
          _s("b", job=None, source="telegram", n_calls=0), _s("c", source="cron")]
    assert chat_stats(ss).runs == 1
    assert chat_stats(ss).median_priced_cost == pytest.approx(3000 * PER_TOKEN)
    assert chat_stats([_s("c", source="cron")]) is None


def test_weekly_spend_sums_priced_cost_of_selected_jobs_including_zero_call_runs():
    ss = [_s("a", job="j1", tokens=1000, cost=9.0), _s("b", job="j1", tokens=2000, n_calls=0),
          _s("c", job="j2", tokens=4000), _s("d", job="j3", tokens=8000)]
    assert weekly_spend(ss, {"j1", "j2"}) == pytest.approx(7000 * PER_TOKEN)
    assert weekly_spend(ss, set()) == 0.0
