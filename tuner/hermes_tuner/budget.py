"""Tuner spend, measured savings and the weekly line (spec 4.8, 6).

Every dollar figure here is an ESTIMATE: costs come from Hermes's estimated
cost, or the token fallback in `measure.run_cost` when billing reads $0.
Nothing here reads the clock: `now` is always a parameter.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from statistics import median

from .ledger import Ledger
from .measure import run_cost
from .model import Session

DAY = 86400.0
BASELINE_DAYS = 28
JOB_KINDS = frozenset({"job_prompt", "composed_tool"})
SAVING_STATES = frozenset({"trial", "kept"})


def iso_week_start(now: float) -> float:
    """Epoch of Monday 00:00 UTC of `now`'s ISO week."""
    d = datetime.fromtimestamp(now, timezone.utc)
    monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
    return monday.timestamp()


def job_runs(sessions: Iterable[Session], job_id: str) -> list[Session]:
    """Runs of `job_id` with at least one tool call (zero-call runs are outages)."""
    return [s for s in sessions if s.job_id == job_id and s.calls]


def job_cost_baseline(sessions: Iterable[Session], job_id: str, applied_at: float) -> float | None:
    """Median run_cost of the job's runs in [applied_at - 28 d, applied_at)."""
    start = applied_at - BASELINE_DAYS * DAY
    costs = [run_cost(s) for s in job_runs(sessions, job_id) if start <= s.started_at < applied_at]
    return median(costs) if costs else None


def tuner_spend(ledger: Ledger, sessions: Iterable[Session], shadow_ids: Iterable[str],
                since: float, until: float | None = None) -> float:
    """Drafting spend on records created in (since, until] plus run_cost of shadow-job
    sessions started in that window."""
    shadows = set(shadow_ids)

    def inside(t: float) -> bool:
        return t > since and (until is None or t <= until)

    drafting = sum(r.spend_usd for r in ledger.records if inside(r.created_at))
    shadow = sum(run_cost(s) for s in sessions if s.job_id in shadows and inside(s.started_at))
    return drafting + shadow


def savings(ledger: Ledger, sessions: list[Session], since: float, until: float) -> float:
    """Σ over jobs with a job fix in trial or kept, of Σ over that job's runs in
    (since, until] after the fix was applied, of (baseline − run_cost(run)).

    Per job the baseline is the one before its EARLIEST live fix, so two fixes kept
    one after the other on the same job are not counted twice. Config and skill
    fixes have no dollar figure (chat sessions differ too much to attribute).
    """
    first: dict[str, float] = {}
    for r in ledger.records:
        if r.kind in JOB_KINDS and r.state in SAVING_STATES and r.applied_at is not None:
            first[r.target] = min(first.get(r.target, r.applied_at), r.applied_at)
    total = 0.0
    for job, applied_at in first.items():
        base = job_cost_baseline(sessions, job, applied_at)
        if base is None:
            continue
        total += sum(base - run_cost(s) for s in job_runs(sessions, job)
                     if since < s.started_at <= until and s.started_at >= applied_at)
    return total


def _money(x: float) -> str:
    return f"-${-x:.2f}" if round(x, 2) < 0 else f"${x:.2f}"


def weekly_line(ledger: Ledger, sessions: list[Session], now: float,
                shadow_ids: Iterable[str] = (), errors: int = 0,
                since: float | None = None) -> str:
    """One line covering (since, now]; `since` defaults to 7 days before `now`. The
    cycle passes the time the previous line was written, so an outcome stamped by
    the run that writes a line is in exactly one line. `errors` is the number of
    failures in the window (the cycle logs each failure once, including those that
    left a bridge_error record, so records are not counted again here)."""
    if since is None:
        since = now - 7 * DAY

    def inside(t: float | None) -> bool:
        return t is not None and since < t <= now

    saved = savings(ledger, sessions, since, now)
    applied = sum(inside(r.applied_at) for r in ledger.records)
    kept = sum(r.state == "kept" and inside(r.decided_at) for r in ledger.records)
    rolled = sum(r.state == "rolled_back" and inside(r.decided_at) for r in ledger.records)
    spent = tuner_spend(ledger, sessions, shadow_ids, since, now)
    errs = errors
    line = (f"Hermes tuner: saved ~{_money(saved)} this week (ESTIMATE), {applied} applied, "
            f"{kept} kept, {rolled} rolled back; tuner spent {_money(spent)}")
    if errs:
        line += f"; errors: {errs} (see ~/.hermes/tuner/ledger.json)"
    return line
