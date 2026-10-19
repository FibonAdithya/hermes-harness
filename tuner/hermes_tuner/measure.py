from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from hermes_tuner.model import Pricing, Session


def breakdown(session: Session) -> list[int]:
    """[input, cache read, cache write, output] tokens: what a ledger record stores
    of a baseline run (FixRecord.baseline_runs)."""
    return [session.input_tokens, session.cache_read_tokens, session.cache_write_tokens,
            session.output_tokens]


def priced_breakdown(tokens: list[int], pricing: Pricing) -> float:
    """`breakdown` output priced like priced_cost."""
    fresh, read_t, write_t, out = tokens
    p = pricing
    read = p.cache_read_per_m if p.cache_read_per_m is not None else p.input_per_m
    write = p.cache_write_per_m if p.cache_write_per_m is not None else p.input_per_m
    return (fresh * p.input_per_m + read_t * read + write_t * write + out * p.output_per_m) / 1e6


def priced_cost(session: Session, pricing: Pricing) -> float:
    """The run priced from its token breakdown at `pricing`'s list prices (USD):
    fresh input, cache reads, cache writes and output each at their own rate; a
    missing cache rate falls back to the input rate (never free).

    The raw `est_cost` is never used for a decision or a figure. Every comparison
    prices both sides with the same Pricing. An ESTIMATE."""
    return priced_breakdown(breakdown(session), pricing)


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

