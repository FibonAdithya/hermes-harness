import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fakesystem import FakeSystem
from hermes_friends.cli import main
from hermes_friends.model import Friend
from hermes_friends.policy import gateway_unit
from hermes_friends.spend import (MonthSpend, SpendState, decide, month_spend,
                                  month_start)

LON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=LON)
MONTH = "2026-10"


def friend(name="alice", cap=5.0):
    return Friend(name=name, telegram_id=4242, email=f"{name}@example.com", tz="Europe/London",
                  port=8101, cap_usd=cap, activated=True)


def spend(total):
    return MonthSpend(total, [], 0.0)


def make_db(path: Path, rows):
    """rows: (id, started_at, in, out, cache_read, cache_write, cost)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, "
                "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
                "cache_write_tokens INTEGER, estimated_cost_usd REAL)")
    con.executemany("INSERT INTO sessions VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def mk_friend(root, name="alice", cap=5.0):
    p = root / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    friend(name, cap).save(p)
    return p


def set_cost(p, cost, ts=None):
    (p / "state.db").unlink(missing_ok=True)
    make_db(p / "state.db", [("s1", ts or NOW.timestamp() - 86400, 10, 10, 0, 0, cost)])


def run(root, fake, now):
    return main(["spend-check"], system=fake, root=root, now=now)


def state(p):
    return json.loads((p / "friend-spend.json").read_text())


def sends(fake):
    return [c for c in fake.calls if c[0] == "send"]


# ---- month_spend ----

def test_month_window_london(tmp_path):
    db = tmp_path / "state.db"
    sep30 = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc).timestamp()
    oct31 = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc).timestamp()
    make_db(db, [("a", sep30, 1, 1, 0, 0, 1.0), ("b", oct31, 1, 1, 0, 0, 2.0)])
    # 2026-09-30 23:30 UTC = 2026-10-01 00:30 BST and 2026-10-31 23:30 UTC (GMT) are both October
    assert month_spend(db, month_start(datetime(2026, 10, 15, tzinfo=LON))).priced_usd == 3.0
    nov = month_start(datetime(2026, 11, 15, tzinfo=LON))
    assert month_spend(db, nov).priced_usd == 0.0
    assert month_start(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)) == \
        datetime(2026, 10, 1, tzinfo=LON)


def test_unpriced_session_fallback(tmp_path):
    db = tmp_path / "state.db"
    t = NOW.timestamp()
    make_db(db, [("u", t, 1_000_000, 0, 0, 0, None), ("z", t, 0, 0, 0, 0, None),
                 ("zc", t, None, None, None, None, 0)])
    ms = month_spend(db, month_start(NOW))
    assert ms.unpriced == ["u"]
    assert ms.unpriced_usd == pytest.approx(1.89)
    assert ms.priced_usd == 0.0
    assert ms.total_usd == pytest.approx(1.89)


def test_started_at_is_epoch(tmp_path):
    db = tmp_path / "state.db"
    start = month_start(NOW)
    ts = start.timestamp()
    make_db(db, [("before", ts - 1, 1, 1, 0, 0, 1.0), ("at", ts, 1, 1, 0, 0, 2.0)])
    assert month_spend(db, start).priced_usd == 2.0


def test_missing_db_not_created(tmp_path):
    db = tmp_path / "nope.db"
    ms = month_spend(db, month_start(NOW))
    assert (ms.priced_usd, ms.unpriced, ms.unpriced_usd) == (0, [], 0)
    assert not db.exists()


# ---- decide ----

def test_zero_spend_month_no_actions():
    assert decide(friend(), spend(0.0), SpendState(MONTH), NOW) == []


def test_zero_cap_stops_immediately():
    assert "stop_cap" in decide(friend(cap=0.0), spend(0.0), SpendState(MONTH), NOW)


def test_warn_threshold_boundary():
    assert "warn" not in decide(friend(), spend(3.99), SpendState(MONTH), NOW)
    assert "warn" in decide(friend(), spend(4.00), SpendState(MONTH), NOW)
    assert "warn" not in decide(friend(), spend(4.00), SpendState(MONTH, warned=True), NOW)


def test_cap_boundary():
    assert "stop_cap" not in decide(friend(), spend(4.99), SpendState(MONTH), NOW)
    assert "stop_cap" in decide(friend(), spend(5.00), SpendState(MONTH), NOW)
    assert "stop_cap" not in decide(friend(), spend(5.00),
                                    SpendState(MONTH, stopped_by_cap=True), NOW)


def old_sample(v=1.0, age=3600):
    return [(NOW.timestamp() - age, v)]


def test_runaway_counts_growth_of_old_session():
    st = SpendState(MONTH, samples=old_sample())
    assert "stop_runaway" in decide(friend(), spend(2.01), st, NOW)
    assert "stop_runaway" not in decide(friend(), spend(2.00), st, NOW)


def test_runaway_needs_old_enough_sample():
    st = SpendState(MONTH, samples=old_sample(age=3599))
    assert "stop_runaway" not in decide(friend(), spend(9.0), st, NOW)
    assert "stop_runaway" not in decide(friend(), spend(9.0), SpendState(MONTH), NOW)


def test_runaway_uses_closest_old_sample():
    st = SpendState(MONTH, samples=old_sample(0.0, 7000) + old_sample(1.5, 3700))
    assert "stop_runaway" not in decide(friend(), spend(2.0), st, NOW)


def test_runaway_not_repeated():
    st = SpendState(MONTH, samples=old_sample(), stopped_by_runaway=True)
    assert "stop_runaway" not in decide(friend(), spend(2.01), st, NOW)


def test_unpriced_alert_once():
    sp = MonthSpend(0.0, ["a", "b"], 0.1)
    assert "alert_unpriced" in decide(friend(), sp, SpendState(MONTH), NOW)
    assert "alert_unpriced" not in decide(friend(), sp, SpendState(MONTH, alerted_unpriced=["a", "b"]), NOW)


# ---- spend-check ----

def test_spend_check_records_samples(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    t0 = NOW
    set_cost(p, 1.00)
    assert run(hermes_root, fake, t0) == 0
    assert run(hermes_root, fake, t0 + timedelta(minutes=15)) == 0
    assert len(state(p)["samples"]) == 2
    assert run(hermes_root, fake, t0 + timedelta(minutes=30)) == 0
    assert run(hermes_root, fake, t0 + timedelta(minutes=45)) == 0
    assert run(hermes_root, fake, t0 + timedelta(minutes=60)) == 0
    assert run(hermes_root, fake, t0 + timedelta(minutes=135)) == 0
    # samples older than 7200 s are pruned (t0 .. t0+15m are gone at t0+135m)
    ts = [s[0] for s in state(p)["samples"]]
    assert min(ts) >= (t0 + timedelta(minutes=135)).timestamp() - 7200
    assert t0.timestamp() not in ts
    assert not any(c[0] == "systemctl" for c in fake.calls)
    # spend grows by $1.01 against the sample from an hour earlier
    set_cost(p, 2.01)
    t = t0 + timedelta(minutes=195)
    assert run(hermes_root, fake, t) == 0
    assert ("systemctl", "stop", gateway_unit("alice")) in fake.calls
    assert state(p)["stopped_by_runaway"] is True
    assert (p / "friend-spend.json").stat().st_mode & 0o777 == 0o600


def test_spend_check_runaway_not_repeated(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    set_cost(p, 1.0)
    run(hermes_root, fake, NOW)
    set_cost(p, 2.5)
    run(hermes_root, fake, NOW + timedelta(hours=1))
    n = len(sends(fake))
    run(hermes_root, fake, NOW + timedelta(hours=1, minutes=15))
    assert len(sends(fake)) == n
    assert [c for c in fake.calls if c[:2] == ("systemctl", "stop")] == \
        [("systemctl", "stop", gateway_unit("alice"))]


def test_stop_disables_unit(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    set_cost(p, 5.0)
    assert run(hermes_root, fake, NOW) == 0
    unit = gateway_unit("alice")
    msgs = [c for c in fake.calls if c[0] in ("send", "systemctl")]
    friend_msg = ("send", "alice", 4242,
                  "You've reached this month's assistant allowance ($5.00, estimate). "
                  "I'll be back on the 1st.")
    assert friend_msg in msgs
    i = msgs.index(friend_msg)
    assert msgs[i + 1] == ("systemctl", "stop", unit)
    assert msgs[i + 2] == ("systemctl", "disable", unit)
    assert msgs[i + 3] == ("send", None, 999000111,
                           "friends: alice reached cap; gateway stopped and disabled "
                           "— $5.00 of $5.00 this month (ESTIMATE).")


def test_warn_messages(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    set_cost(p, 4.0)
    run(hermes_root, fake, NOW)
    assert sends(fake) == [
        ("send", "alice", 4242, "You've used 80% of this month's assistant allowance "
                                "($4.00 of $5.00, estimate). It resets on the 1st."),
        ("send", None, 999000111, "friends: alice passed 80% — $4.00 of $5.00 this month (ESTIMATE)."),
    ]
    assert state(p)["warned"] is True
    run(hermes_root, fake, NOW + timedelta(minutes=15))
    assert len(sends(fake)) == 2


def test_unpriced_owner_message_once(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    make_db(p / "state.db", [("s1", NOW.timestamp() - 5, 1_000_000, 0, 0, 0, None)])
    run(hermes_root, fake, NOW)
    assert sends(fake) == [("send", None, 999000111,
                            "friends: alice has unpriced sessions s1; counted at fallback rate "
                            "— $1.89 of $5.00 this month (ESTIMATE).")]
    run(hermes_root, fake, NOW + timedelta(minutes=15))
    assert len(sends(fake)) == 1


def prior(p, month, **kw):
    d = {"month": month, "warned": False, "stopped_by_cap": False, "stopped_by_runaway": False,
         "alerted_unpriced": [], "samples": []}
    d.update(kw)
    (p / "friend-spend.json").write_text(json.dumps(d))


@pytest.mark.parametrize("cap,runaway,restart", [(True, False, True), (False, True, False),
                                                 (True, True, False)])
def test_new_month_restarts_cap_stop_not_runaway(hermes_root, cap, runaway, restart):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    prior(p, "2026-09", stopped_by_cap=cap, stopped_by_runaway=runaway, warned=True)
    set_cost(p, 0.5)
    assert run(hermes_root, fake, NOW) == 0
    unit = gateway_unit("alice")
    assert (("systemctl", "enable", unit) in fake.calls) is restart
    assert (("systemctl", "start", unit) in fake.calls) is restart
    if restart:
        assert fake.index("systemctl", "enable", unit) < fake.index("systemctl", "start", unit)
        assert ("send", None, 999000111, "friends: alice new month; gateway re-enabled.") in fake.calls
    st = state(p)
    assert st["month"] == MONTH
    assert st["stopped_by_runaway"] is runaway
    assert st["stopped_by_cap"] is False and st["warned"] is False


def test_new_month_clears_samples(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    prior(p, "2026-09", samples=[[NOW.timestamp() - 3600, 4.0]])
    set_cost(p, 0.5)
    run(hermes_root, fake, NOW)
    # last month's 4.0 sample must not be compared against 0.5 (nor trip anything)
    assert [s[1] for s in state(p)["samples"]] == [0.5]


def test_one_friend_error_does_not_block_others(hermes_root):
    a = mk_friend(hermes_root, "alice")
    b = mk_friend(hermes_root, "bob")
    (a / "state.db").write_bytes(b"this is not a database" * 100)
    set_cost(b, 5.0)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, NOW) == 1
    owner_msgs = [c[3] for c in sends(fake) if c[1] is None]
    assert any(m.startswith("friends: alice spend-check failed: ") for m in owner_msgs)
    assert ("systemctl", "stop", gateway_unit("bob")) in fake.calls
    assert (b / "friend-spend.json").is_file()


# ---- fix round 1 ----

def test_failed_disable_retries_without_repeating_messages(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("systemctl", "disable")]
    set_cost(p, 5.0)
    unit = gateway_unit("alice")
    for n in range(3):
        assert run(hermes_root, fake, NOW + timedelta(minutes=15 * n)) == 1
        assert (p / "friend-spend.json").is_file()
    assert len(state(p)["samples"]) == 3
    assert state(p)["stopped_by_cap"] is False
    friend_msgs = [c for c in sends(fake) if c[1] == "alice"]
    assert len(friend_msgs) == 1 and "reached" in friend_msgs[0][3]
    assert len([c for c in fake.calls if c == ("systemctl", "stop", unit)]) == 3
    # the failure is reported to the owner each run
    assert len([c for c in sends(fake) if c[1] is None and "spend-check failed" in c[3]]) == 3
    fake.fail_on = []
    assert run(hermes_root, fake, NOW + timedelta(minutes=45)) == 0
    assert state(p)["stopped_by_cap"] is True
    assert ("systemctl", "disable", unit) in fake.calls
    assert len([c for c in sends(fake) if c[1] == "alice"]) == 1
    assert len([c for c in sends(fake) if c[1] is None and "reached cap" in c[3]]) == 1


def test_corrupt_friend_json_does_not_block_others(hermes_root):
    bad = mk_friend(hermes_root, "alice")
    (bad / "friend.json").write_text("{not json")
    b = mk_friend(hermes_root, "bob")
    set_cost(b, 5.0)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, NOW) == 1
    owner_msgs = [c[3] for c in sends(fake) if c[1] is None]
    assert any(m.startswith("friends: alice spend-check failed: ") for m in owner_msgs)
    assert ("systemctl", "stop", gateway_unit("bob")) in fake.calls


def test_new_month_resets_state_in_decide():
    st = SpendState("2026-09", warned=True)
    acts = decide(friend(), spend(4.0), st, NOW)
    assert "new_month" in acts and "warn" in acts


def test_cost_zero_with_tokens_is_unpriced(tmp_path):
    db = tmp_path / "state.db"
    make_db(db, [("zero", NOW.timestamp(), 1_000_000, 0, 0, 0, 0.0)])
    ms = month_spend(db, month_start(NOW))
    assert ms.unpriced == ["zero"] and ms.priced_usd == 0.0
    assert ms.unpriced_usd == pytest.approx(1.89)


def test_warn_dropped_when_cap_reached_same_run():
    acts = decide(friend(), spend(5.0), SpendState(MONTH), NOW)
    assert "stop_cap" in acts and "warn" not in acts


def test_cap_reached_sends_single_friend_message(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    set_cost(p, 5.0)
    run(hermes_root, fake, NOW)
    friend_msgs = [c[3] for c in sends(fake) if c[1] == "alice"]
    assert len(friend_msgs) == 1 and friend_msgs[0].startswith("You've reached")
    assert not any("passed 80%" in c[3] for c in sends(fake))


def test_new_month_with_cap_still_hit_stays_stopped(hermes_root):
    p = mk_friend(hermes_root, cap=0.0)
    fake = FakeSystem(hermes_root)
    prior(p, "2026-09", stopped_by_cap=True, notified_cap=True)
    make_db(p / "state.db", [])
    assert run(hermes_root, fake, NOW) == 0
    assert fake.calls and not any(c[0] == "systemctl" for c in fake.calls)
    assert sends(fake) == []
    st = state(p)
    assert st["month"] == MONTH and st["stopped_by_cap"] is True


def test_float_thresholds():
    assert "warn" in decide(friend(cap=3.0), spend(0.8 * 3.0 - 1e-12), SpendState(MONTH), NOW)
    total = sum(0.05 for _ in range(100))
    assert total < 5.0  # 4.999999999999998
    assert "stop_cap" in decide(friend(), spend(total), SpendState(MONTH), NOW)
    st = SpendState(MONTH, samples=old_sample(1.2))
    assert "stop_runaway" not in decide(friend(), spend(2.2), st, NOW)  # 2.2 - 1.2 = 1.0000000000000002


def test_both_stops_one_pair_runaway_message(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    prior(p, MONTH, samples=[[NOW.timestamp() - 3600, 1.0]])
    set_cost(p, 5.0)
    assert run(hermes_root, fake, NOW) == 0
    unit = gateway_unit("alice")
    assert [c for c in fake.calls if c[0] == "systemctl"] == [
        ("systemctl", "stop", unit), ("systemctl", "disable", unit)]
    friend_msgs = [c[3] for c in sends(fake) if c[1] == "alice"]
    assert friend_msgs == ["I've paused myself because I was using far more than usual. "
                           "The owner has been told."]
    st = state(p)
    assert st["stopped_by_cap"] is True and st["stopped_by_runaway"] is True


# ---- fix round 2 ----

def runaway_msgs(fake):
    return [c for c in sends(fake) if c[1] == "alice" and "paused myself" in c[3]]


def test_stale_runaway_notice_not_carried_across_months(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    prior(p, "2026-09", notified_runaway=True, stopped_by_runaway=False)
    set_cost(p, 0.5)
    run(hermes_root, fake, NOW)
    assert state(p)["notified_runaway"] is False
    set_cost(p, 2.0)
    t = NOW + timedelta(hours=1)
    run(hermes_root, fake, t)  # sample from NOW (0.5) -> 2.0 is a runaway
    assert len(runaway_msgs(fake)) == 1


def test_runaway_after_clear_runaway_notifies(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    prior(p, MONTH, notified_runaway=True, stopped_by_runaway=True,
          samples=[[NOW.timestamp() - 3600, 1.0]])
    st = SpendState.load(p, NOW)
    st.clear_runaway()  # what resume does
    st.save(p, fake)
    set_cost(p, 2.5)
    run(hermes_root, fake, NOW)
    assert len(runaway_msgs(fake)) == 1


def test_clear_helpers():
    st = SpendState(MONTH, stopped_by_cap=True, notified_cap=True,
                    stopped_by_runaway=True, notified_runaway=True)
    st.clear_runaway()
    assert (st.stopped_by_runaway, st.notified_runaway, st.stopped_by_cap, st.notified_cap) == \
        (False, False, True, True)
    st.clear_cap()
    assert (st.stopped_by_cap, st.notified_cap) == (False, False)


def test_new_month_restarts_half_finished_cap_stop(hermes_root):
    p = mk_friend(hermes_root)
    fake = FakeSystem(hermes_root)
    prior(p, "2026-09", notified_cap=True, stopped_by_cap=False)
    set_cost(p, 0.5)
    assert run(hermes_root, fake, NOW) == 0
    unit = gateway_unit("alice")
    assert ("systemctl", "enable", unit) in fake.calls
    assert ("systemctl", "start", unit) in fake.calls
