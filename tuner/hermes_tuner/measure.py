from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from hermes_tuner import policy
from hermes_tuner.model import Session


def priced_cost(session: Session) -> float:
    """The run priced from its tokens: (prompt + output) x USD_PER_MTOK / 1e6.

    Price-independent, so it is comparable across the billing change; the raw
    `est_cost` is never used for a decision or a figure. An ESTIMATE."""
    return (session.prompt_tokens + session.output_tokens) * policy.USD_PER_MTOK / 1e6


@dataclass(frozen=True)
class JobStats:
    runs: int
    median_priced_cost: float
    median_prompt_tokens: float
    median_tool_calls: float
    median_result_chars: float


def _stats(runs: list[Session]) -> JobStats | None:
    # Runs with zero tool calls are outages (e.g. auth failures), not
    # representative work; including them would skew the baseline.
    runs = [s for s in runs if len(s.calls) > 0]
    if not runs:
        return None
    return JobStats(
        runs=len(runs),
        median_priced_cost=median(priced_cost(s) for s in runs),
        median_prompt_tokens=median(s.prompt_tokens for s in runs),
        median_tool_calls=median(len(s.calls) for s in runs),
        median_result_chars=median(sum(c.result_chars for c in s.calls) for s in runs),
    )


def job_stats(sessions: list[Session], job_id: str) -> JobStats | None:
    """Medians over the runs of `job_id` in `sessions`.

    The caller applies any since/until bounds by choosing which sessions to
    pass. Zero-tool-call runs are excluded. Returns None when no runs remain.
    """
    return _stats([s for s in sessions if s.job_id == job_id])


def chat_stats(sessions: list[Session]) -> JobStats | None:
    """Medians over Telegram chat sessions in `sessions`.

    The caller applies any since/until bounds by choosing which sessions to
    pass. Zero-tool-call runs are excluded. Returns None when none remain.
    """
    return _stats([s for s in sessions if s.source == "telegram"])


def weekly_spend(sessions: list[Session], job_ids: set[str]) -> float:
    """Sum of priced_cost over every session whose job_id is in `job_ids`,
    zero-call runs included (spend is spend)."""
    return sum(priced_cost(s) for s in sessions if s.job_id in job_ids)
