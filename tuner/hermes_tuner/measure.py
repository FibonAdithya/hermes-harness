from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from hermes_tuner import policy
from hermes_tuner.model import Pricing, Session


# Task 1 interim: the old flat rate as a Pricing, for callers not yet given the
# model's list (Task 2 wires the real one through and removes this).
INTERIM_FLAT_PRICING = Pricing(model="flat-estimate", input_per_m=policy.USD_PER_MTOK,
                               output_per_m=policy.USD_PER_MTOK, cache_read_per_m=None,
                               cache_write_per_m=None, source="tuner-flat-estimate",
                               source_url=None, version=None, fetched_at=0.0)


def priced_cost(session: Session, pricing: Pricing) -> float:
    """The run priced from its token breakdown at `pricing`'s list prices (USD):
    fresh input, cache reads, cache writes and output each at their own rate; a
    missing cache rate falls back to the input rate (never free).

    The raw `est_cost` is never used for a decision or a figure. Every comparison
    prices both sides with the same Pricing. An ESTIMATE."""
    p = pricing
    read = p.cache_read_per_m if p.cache_read_per_m is not None else p.input_per_m
    write = p.cache_write_per_m if p.cache_write_per_m is not None else p.input_per_m
    return (session.input_tokens * p.input_per_m + session.cache_read_tokens * read
            + session.cache_write_tokens * write + session.output_tokens * p.output_per_m) / 1e6


@dataclass(frozen=True)
class JobStats:
    runs: int
    median_priced_cost: float
    median_prompt_tokens: float
    median_tool_calls: float
    median_result_chars: float


def _stats(runs: list[Session], pricing: Pricing) -> JobStats | None:
    # Runs with zero tool calls are outages (e.g. auth failures), not
    # representative work; including them would skew the baseline.
    runs = [s for s in runs if len(s.calls) > 0]
    if not runs:
        return None
    return JobStats(
        runs=len(runs),
        median_priced_cost=median(priced_cost(s, pricing) for s in runs),
        median_prompt_tokens=median(s.prompt_tokens for s in runs),
        median_tool_calls=median(len(s.calls) for s in runs),
        median_result_chars=median(sum(c.result_chars for c in s.calls) for s in runs),
    )


def job_stats(sessions: list[Session], job_id: str, pricing: Pricing) -> JobStats | None:
    """Medians over the runs of `job_id` in `sessions`.

    The caller applies any since/until bounds by choosing which sessions to
    pass. Zero-tool-call runs are excluded. Returns None when no runs remain.
    """
    return _stats([s for s in sessions if s.job_id == job_id], pricing)


def chat_stats(sessions: list[Session], pricing: Pricing) -> JobStats | None:
    """Medians over Telegram chat sessions in `sessions`.

    The caller applies any since/until bounds by choosing which sessions to
    pass. Zero-tool-call runs are excluded. Returns None when none remain.
    """
    return _stats([s for s in sessions if s.source == "telegram"], pricing)

