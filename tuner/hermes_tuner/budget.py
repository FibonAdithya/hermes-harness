"""Tuner spend, measured savings and the weekly line (spec 4.8, 6).

Every dollar figure here is an ESTIMATE at the configured model's list prices
(model.Pricing, from Hermes's price list for the model): runs are priced from their
token breakdown (`measure.priced_cost`), drafting from the reply's breakdown, never
from Hermes's estimated cost (which reads $0 under subscription billing). Nothing
here reads the clock: `now` is always a parameter.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from .ledger import Ledger
from .measure import priced_cost
from .model import Pricing, Session
from .trial import baseline_medians

DAY = 86400.0
BASELINE_DAYS = 28
ESTIMATE_DAYS = 28  # the weekly line compares estimates with measurements this far back
JOB_KINDS = frozenset({"job_prompt", "composed_tool", "reducer"})
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
                since: float, pricing: Pricing, until: float | None = None) -> float:
    """Drafting spend on records created in (since, until] (stored when drafted) plus
    priced_cost at `pricing` of shadow-job sessions started in that window."""
    shadows = set(shadow_ids)

    def inside(t: float) -> bool:
        return t > since and (until is None or t <= until)

    drafting = sum(r.spend_usd for r in ledger.records if inside(r.created_at))
    shadow = sum(priced_cost(s, pricing) for s in sessions
                 if s.job_id in shadows and inside(s.started_at))
    return drafting + shadow


def savings(ledger: Ledger, sessions: list[Session], since: float, until: float,
            pricing: Pricing) -> float:
    """Σ over jobs with a job fix in trial or kept, of Σ over that job's runs in
    (since, until] after the fix went live (`live_from`), of (baseline − priced_cost(run)).

    Per job the fix used is the EARLIEST applied one that is live, so two fixes kept
    one after the other on the same job are not counted twice; its baseline is the
    median of its stored `baseline_runs` priced with the SAME `pricing` as the runs
    (the history read is not widened for it). Records with live_from None (waiting
    for a restart) or no baseline runs are skipped. Config and skill fixes have no
    dollar figure (chat sessions differ too much to attribute).
    """
    first: dict[str, tuple[float, float, list | None]] = {}  # job -> (applied_at, live_from, runs)
    for r in ledger.records:
        if (r.kind in JOB_KINDS and r.state in SAVING_STATES and r.applied_at is not None
                and r.live_from is not None):
            if r.target not in first or r.applied_at < first[r.target][0]:
                first[r.target] = (r.applied_at, r.live_from, r.baseline_runs)
    total = 0.0
    for job, (_applied_at, live_from, runs) in first.items():
        if not runs:
            continue
        base, _prompt = baseline_medians(runs, pricing)
        total += sum(base - priced_cost(s, pricing) for s in job_runs(sessions, job)
                     if since < s.started_at <= until and s.started_at > live_from)
    return total


def _money(x: float) -> str:
    return f"-${-x:.2f}" if round(x, 2) < 0 else f"${x:.2f}"


def weekly_line(ledger: Ledger, sessions: list[Session], now: float,
                pricing: Pricing | None, shadow_ids: Iterable[str] = (), errors: int = 0,
                since: float | None = None, parked: int = 0) -> str:
    """One line covering (since, now]; `since` defaults to 7 days before `now`. The
    cycle passes the time the previous line was written, so an outcome stamped by
    the run that writes a line is in exactly one line. `errors` is the number of
    failures in the window (the cycle logs each failure once, including those that
    left a bridge_error record, so records are not counted again here). Dollar
    figures name the price list they are estimated at; with no `pricing` there are
    none ("pricing unavailable").

    With a price list, fixes kept or rolled back in the 28 days before `now` that
    carry both an estimated and a measured saving per run add the means of each over
    those fixes ("estimates: ... vs measured ... per run (mean of n fixes)").
    `parked` is the number of candidates drafting left parked this run (backoff); it
    is shown when above 0."""
    if since is None:
        since = now - 7 * DAY

    def inside(t: float | None) -> bool:
        return t is not None and since < t <= now

    applied = sum(inside(r.applied_at) for r in ledger.records)
    kept = sum(r.state == "kept" and inside(r.decided_at) for r in ledger.records)
    rolled = sum(r.state == "rolled_back" and inside(r.decided_at) for r in ledger.records)
    counts = f"{applied} applied, {kept} kept, {rolled} rolled back"
    if pricing is None:
        line = (f"Hermes tuner: savings unknown this week (pricing unavailable), {counts}; "
                "tuner spend unknown")
    else:
        saved = savings(ledger, sessions, since, now, pricing)
        spent = tuner_spend(ledger, sessions, shadow_ids, since, pricing, until=now)
        version = pricing.version or pricing.source
        line = (f"Hermes tuner: saved ~{_money(saved)} this week (ESTIMATE at {pricing.model} "
                f"list prices, {version}), {counts}; tuner spent {_money(spent)}")
        judged = [r for r in ledger.records
                  if r.state in ("kept", "rolled_back") and r.decided_at is not None
                  and now - ESTIMATE_DAYS * DAY < r.decided_at <= now
                  and r.est_saving_per_run is not None
                  and r.measured_saving_per_run is not None]
        if judged:
            n = len(judged)
            est_mean = sum(r.est_saving_per_run for r in judged) / n
            meas_mean = sum(r.measured_saving_per_run for r in judged) / n
            line += (f"; estimates: {_money(est_mean)} vs measured {_money(meas_mean)} per run "
                     f"(mean of {n} fixes)")
    if parked > 0:
        line += f"; parked: {parked}"
    if errors:
        line += f"; errors: {errors} (see ~/.hermes/tuner/cycle_state.json and ledger.json)"
    return line
