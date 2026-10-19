"""Tuner spend, measured savings and the weekly line (spec 4.8, 6).

Every dollar figure here is an ESTIMATE: runs are priced from their tokens
(`measure.priced_cost`), drafting from the reply's tokens, never from Hermes's
estimated cost (which reads $0 under subscription billing). Nothing here reads
the clock: `now` is always a parameter.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from .ledger import Ledger
from .measure import priced_cost
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


def tuner_spend(ledger: Ledger, sessions: Iterable[Session], shadow_ids: Iterable[str],
                since: float, until: float | None = None) -> float:
    """Drafting spend on records created in (since, until] plus priced_cost of
    shadow-job sessions started in that window."""
    shadows = set(shadow_ids)

    def inside(t: float) -> bool:
        return t > since and (until is None or t <= until)

    drafting = sum(r.spend_usd for r in ledger.records if inside(r.created_at))
    shadow = sum(priced_cost(s) for s in sessions if s.job_id in shadows and inside(s.started_at))
    return drafting + shadow


def savings(ledger: Ledger, sessions: list[Session], since: float, until: float) -> float:
    """Σ over jobs with a job fix in trial or kept, of Σ over that job's runs in
    (since, until] after the fix went live (`live_from`), of (baseline − priced_cost(run)).

    Per job the fix used is the EARLIEST applied one that is live, so two fixes kept
    one after the other on the same job are not counted twice; its baseline is
    `baseline_cost`, the priced median of the 28 days before applied_at stored at
    go-live (the history read is not widened to recompute it). Records with
    live_from None (waiting for a restart) or no baseline are skipped. Config and
    skill fixes have no dollar figure (chat sessions differ too much to attribute).
    """
    first: dict[str, tuple[float, float, float | None]] = {}  # job -> (applied_at, live_from, base)
    for r in ledger.records:
        if (r.kind in JOB_KINDS and r.state in SAVING_STATES and r.applied_at is not None
                and r.live_from is not None):
            if r.target not in first or r.applied_at < first[r.target][0]:
                first[r.target] = (r.applied_at, r.live_from, r.baseline_cost)
    total = 0.0
    for job, (_applied_at, live_from, base) in first.items():
        if base is None:
            continue
        total += sum(base - priced_cost(s) for s in job_runs(sessions, job)
                     if since < s.started_at <= until and s.started_at > live_from)
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
