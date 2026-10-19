from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from hermes_tuner.restart import restart_allowed

LONDON = ZoneInfo("Europe/London")
QUIET = 3600.0  # last activity an hour before


def at(y, mo, d, h, mi):
    """A London wall-clock time, handed to restart_allowed as UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).astimezone(timezone.utc)


# 2026-10-19 is BST (UTC+1); 2026-10-26 is GMT (UTC+0, the day after the changeover).
@pytest.mark.parametrize("day", [(2026, 10, 19), (2026, 10, 26), (2026, 10, 25)])
@pytest.mark.parametrize("hm, ok", [((1, 59), False), ((2, 0), True), ((4, 59), True),
                                    ((5, 0), False)])
def test_restart_window_boundaries(day, hm, ok):
    now = at(*day, *hm)
    t = now.timestamp()
    assert restart_allowed(now, t - QUIET, []) is ok


def test_restart_window_uses_london_not_utc():
    # 01:30 UTC on a BST day is 02:30 London: allowed. 04:30 UTC is 05:30 London: not.
    bst_0130_utc = datetime(2026, 10, 19, 1, 30, tzinfo=timezone.utc)
    assert restart_allowed(bst_0130_utc, bst_0130_utc.timestamp() - QUIET, []) is True
    bst_0430_utc = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
    assert restart_allowed(bst_0430_utc, bst_0430_utc.timestamp() - QUIET, []) is False


def test_restart_quiet_period():
    now = at(2026, 10, 19, 3, 0)
    t = now.timestamp()
    assert restart_allowed(now, t - 29 * 60, []) is False
    assert restart_allowed(now, t - 30 * 60, []) is True


def test_restart_job_margin():
    now = at(2026, 10, 19, 3, 0)
    t = now.timestamp()
    assert restart_allowed(now, t - QUIET, [t + 14 * 60]) is False
    assert restart_allowed(now, t - QUIET, [t]) is False
    assert restart_allowed(now, t - QUIET, [t + 15 * 60]) is True
    assert restart_allowed(now, t - QUIET, [t - 60, t + 3600]) is True


def test_restart_naive_datetime_refused():
    with pytest.raises(ValueError):
        restart_allowed(datetime(2026, 10, 19, 3, 0), 0.0, [])
