"""When the tuner may restart the gateway (spec 4.7)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from . import policy


def restart_allowed(now: datetime, last_activity: float, next_runs: list[float]) -> bool:
    """True only inside [02:00, 05:00) Europe/London, with no state.db activity in the
    last QUIET_MIN minutes (exactly QUIET_MIN ago is allowed) and no job due within
    JOB_MARGIN_MIN minutes (a job due now blocks; one already past does not).
    `now` must be timezone-aware."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(policy.RESTART_TZ))
    start, end = policy.RESTART_WINDOW
    if not start <= local.hour < end:
        return False
    t = now.timestamp()
    if t - last_activity < policy.QUIET_MIN * 60:
        return False
    return not any(0 <= n - t < policy.JOB_MARGIN_MIN * 60 for n in next_runs)
