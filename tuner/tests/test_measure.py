import pytest
from hermes_tuner.measure import chat_stats, job_stats, priced_cost
from factory import FLAT, V4_PRO
from hermes_tuner.model import Pricing, Session, ToolCall

PER_TOKEN = 0.42 / 1e6


def _call(i=0, chars=10):
    return ToolCall(call_id=f"c{i}", name="t", arg_keys=(), ref=None, result_chars=chars)


def _s(sid, job="j1", tokens=1000, n_calls=1, source="cron", prompt=None, chars=10, cost=0.0):
    """`tokens` is prompt + output (output fixed at 1); the prompt is fresh input.
    `cost` is the raw billing figure, which the tuner must ignore."""
    prompt = tokens - 1 if prompt is None else prompt
    return Session(id=sid, source=source, job_id=job, started_at=1.0, est_cost=cost,
                   prompt_tokens=prompt, output_tokens=1,
                   calls=tuple(_call(i, chars) for i in range(n_calls)), input_tokens=prompt)


def _tokens(inp=0, read=0, write=0, out=0):
    return Session(id="b", source="cron", job_id="j", started_at=1.0, est_cost=7.0,
                   prompt_tokens=inp + read + write, output_tokens=out, calls=(),
                   input_tokens=inp, cache_read_tokens=read, cache_write_tokens=write)


def test_priced_cost_uses_cache_rate():
    assert priced_cost(_tokens(read=1_000_000), V4_PRO) == pytest.approx(0.003625)
    assert priced_cost(_tokens(inp=1_000_000), V4_PRO) == pytest.approx(0.435)


def test_priced_cost_falls_back_to_input_rate_when_cache_rate_missing():
    # V4_PRO has no cache-write rate: writes cost the input rate, not nothing
    assert priced_cost(_tokens(write=1_000_000), V4_PRO) == pytest.approx(0.435)
    no_read = Pricing(**{**V4_PRO.to_dict(), "cache_read_per_m": None})
    assert priced_cost(_tokens(read=1_000_000), no_read) == pytest.approx(0.435)
    with_write = Pricing(**{**V4_PRO.to_dict(), "cache_write_per_m": 0.5})
    assert priced_cost(_tokens(write=1_000_000), with_write) == pytest.approx(0.5)


def test_priced_cost_output_rate():
    assert priced_cost(_tokens(out=1_000_000), V4_PRO) == pytest.approx(0.87)


def test_priced_cost_sums_every_kind_and_ignores_raw_cost():
    s = _tokens(inp=2_000_000, read=4_000_000, write=1_000_000, out=3_000_000)
    assert priced_cost(s, V4_PRO) == pytest.approx(0.87 + 0.0145 + 0.435 + 2.61)


def test_pricing_dict_round_trip_and_validation():
    assert Pricing.from_dict(V4_PRO.to_dict()) == V4_PRO
    bridge_out = {k: v for k, v in V4_PRO.to_dict().items() if k not in ("version", "fetched_at")}
    got = Pricing.from_dict(bridge_out | {"pricing_version": "deepseek-pricing-2026-07"})
    assert got.version == "deepseek-pricing-2026-07" and got.fetched_at == 0.0
    for bad in ({"input_per_m": None}, {"output_per_m": "0.87"}, {"input_per_m": -1.0},
                {"cache_read_per_m": True}, {"model": ""}, {"input_per_m": float("nan")}):
        with pytest.raises(ValueError):
            Pricing.from_dict(V4_PRO.to_dict() | bad)


def test_median_not_mean():
    ss = [_s("a", tokens=1000), _s("b", tokens=1000), _s("c", tokens=50_000)]
    assert job_stats(ss, "j1", FLAT).median_priced_cost == pytest.approx(1000 * PER_TOKEN)


def test_median_priced_cost_ignores_raw_billing():
    # raw dollars say a is the cheapest; tokens say it is the dearest
    ss = [_s("a", tokens=9000, cost=0.0), _s("b", tokens=1000, cost=5.0),
          _s("c", tokens=2000, cost=5.0)]
    assert job_stats(ss, "j1", FLAT).median_priced_cost == pytest.approx(2000 * PER_TOKEN)


def test_only_that_jobs_sessions_count():
    ss = [_s("a", job="j1"), _s("b", job="j2"), _s("c", job="j1"), _s("d", job="j2"), _s("e", job="j2")]
    assert job_stats(ss, "j1", FLAT).runs == 2
    assert job_stats(ss, "j2", FLAT).runs == 3
    assert job_stats(ss, "none", FLAT) is None


def test_failed_runs_with_zero_calls_are_excluded():
    ss = [_s("a", tokens=90_000, n_calls=0), _s("b", tokens=2000, n_calls=1)]
    st = job_stats(ss, "j1", FLAT)
    assert st.runs == 1 and st.median_priced_cost == pytest.approx(2000 * PER_TOKEN)
    assert job_stats([_s("a", n_calls=0)], "j1", FLAT) is None


def test_other_medians():
    ss = [_s("a", n_calls=1, prompt=10, chars=5), _s("b", n_calls=3, prompt=30, chars=7),
          _s("c", n_calls=5, prompt=20, chars=9)]
    st = job_stats(ss, "j1", FLAT)
    assert st.median_tool_calls == 3
    assert st.median_prompt_tokens == 20
    assert st.median_result_chars == 21  # per-run sums 5, 21, 45


def test_chat_stats_uses_telegram_source_and_excludes_zero_calls():
    ss = [_s("a", job=None, source="telegram", tokens=3000),
          _s("b", job=None, source="telegram", n_calls=0), _s("c", source="cron")]
    assert chat_stats(ss, FLAT).runs == 1
    assert chat_stats(ss, FLAT).median_priced_cost == pytest.approx(3000 * PER_TOKEN)
    assert chat_stats([_s("c", source="cron")], FLAT) is None


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_pricing_from_dict_rejects_non_finite_fetched_at(bad):
    with pytest.raises(ValueError, match="fetched_at"):
        Pricing.from_dict(V4_PRO.to_dict() | {"fetched_at": bad})
