import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fakesystem import FakeSystem
from hermes_friends.cli import main
from hermes_friends.model import Friend
from hermes_friends.policy import gateway_unit
from hermes_friends.registry import list_friends
from test_spend import make_db, prior, set_cost, sends, state

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))
MONTH = "2026-10"
UNIT = gateway_unit("alice")


def mk(root, name="alice", cap=5.0, email=None):
    p = root / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    Friend(name=name, telegram_id=4242, email=email or f"{name}@example.com", tz="Europe/London",
           port=8101, cap_usd=cap, activated=True).save(p)
    return p


def run(root, fake, *argv, caddy=None, now=NOW):
    return main(list(argv), system=fake, root=root, caddy_dir=caddy, now=now)


def sysctl(fake):
    return [c for c in fake.calls if c[0] == "systemctl"]


# ---- cap ----

def test_cap_zero_accepted(hermes_root):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "0") == 0
    assert Friend.load(p).cap_usd == 0.0
    assert sysctl(fake) == []


@pytest.mark.parametrize("v", ["-1", "-0.01", "nan", "inf", "-inf", "abc"])
def test_cap_negative_rejected(hermes_root, v):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", v) != 0
    assert Friend.load(p).cap_usd == 5.0
    assert fake.calls == []


def test_cap_raise_restarts_capped_friend(hermes_root):
    p = mk(hermes_root, cap=1.0)
    set_cost(p, 1.5)
    prior(p, MONTH, stopped_by_cap=True, notified_cap=True)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "3") == 0
    assert Friend.load(p).cap_usd == 3.0
    assert sysctl(fake) == [("systemctl", "enable", UNIT), ("systemctl", "start", UNIT)]
    st = state(p)
    assert st["stopped_by_cap"] is False and st["notified_cap"] is False


def test_cap_raise_half_finished_stop_restarts(hermes_root):
    p = mk(hermes_root, cap=1.0)
    set_cost(p, 1.5)
    prior(p, MONTH, notified_cap=True, stopped_by_cap=False)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "3") == 0
    assert ("systemctl", "start", UNIT) in fake.calls


def test_cap_raise_keeps_runaway_hold(hermes_root):
    p = mk(hermes_root, cap=1.0)
    set_cost(p, 1.5)
    prior(p, MONTH, stopped_by_cap=True, notified_cap=True, stopped_by_runaway=True,
          notified_runaway=True)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "3") == 0
    st = state(p)
    assert st["stopped_by_cap"] is False and st["notified_cap"] is False
    assert st["stopped_by_runaway"] is True and st["notified_runaway"] is True
    assert sysctl(fake) == []
    assert Friend.load(p).cap_usd == 3.0


def test_cap_raise_not_above_spend_stays_stopped(hermes_root):
    p = mk(hermes_root, cap=1.0)
    set_cost(p, 2.0)
    prior(p, MONTH, stopped_by_cap=True, notified_cap=True)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "2.0") == 0  # equal: still at cap
    assert Friend.load(p).cap_usd == 2.0
    assert sysctl(fake) == []
    st = state(p)
    assert st["stopped_by_cap"] is True


def test_cap_change_on_running_friend_only_updates(hermes_root):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "9") == 0
    assert Friend.load(p).cap_usd == 9.0
    assert sysctl(fake) == []


def test_cap_raise_then_cap_stop_notifies_friend_again(hermes_root):
    p = mk(hermes_root, cap=1.0)
    set_cost(p, 1.5)
    prior(p, MONTH, stopped_by_cap=True, notified_cap=True)
    fake = FakeSystem(hermes_root)
    run(hermes_root, fake, "cap", "alice", "3")
    set_cost(p, 3.5)
    assert run(hermes_root, fake, "spend-check") == 0
    assert any(c[1] == "alice" and c[3].startswith("You've reached") for c in sends(fake))


def test_cap_unknown_friend_no_calls(hermes_root):
    victim = hermes_root / "profiles" / ".." / ".." / "victim"
    victim.mkdir(parents=True, exist_ok=True)
    Friend(name="victim", telegram_id=1, email="v@example.com", tz="Europe/London", port=8102,
           cap_usd=1.0, activated=True).save(victim)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "../../victim", "7") != 0
    assert run(hermes_root, fake, "cap", "../x", "1") != 0
    assert run(hermes_root, fake, "cap", "nobody", "1") != 0
    assert fake.calls == []
    assert Friend.load(victim).cap_usd == 1.0


# ---- resume ----

def test_resume_clears_runaway(hermes_root):
    p = mk(hermes_root)
    prior(p, MONTH, stopped_by_runaway=True, notified_runaway=True, stopped_by_cap=True,
          notified_cap=True)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "resume", "alice") == 0
    st = state(p)
    assert not any(st[k] for k in ("stopped_by_runaway", "notified_runaway", "stopped_by_cap",
                                   "notified_cap"))
    assert sysctl(fake) == [("systemctl", "enable", UNIT), ("systemctl", "start", UNIT)]


def test_resume_without_state_file(hermes_root):
    mk(hermes_root)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "resume", "alice") == 0
    assert ("systemctl", "start", UNIT) in fake.calls


def test_resume_then_runaway_notifies_friend(hermes_root):
    p = mk(hermes_root)
    set_cost(p, 0.5)
    prior(p, MONTH, stopped_by_runaway=True, notified_runaway=True,
          samples=[[NOW.timestamp() - 3600, 0.5]])
    fake = FakeSystem(hermes_root)
    run(hermes_root, fake, "resume", "alice")
    set_cost(p, 2.5)
    assert run(hermes_root, fake, "spend-check") == 0
    assert any(c[1] == "alice" and "paused myself" in c[3] for c in sends(fake))


def test_resume_unknown_friend_no_calls(hermes_root):
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "resume", "nobody") != 0
    assert fake.calls == []


# ---- ruling 8 ----

def test_new_month_half_finished_cap_stop_still_over_cap_stays_stopped(hermes_root):
    p = mk(hermes_root, cap=0.0)
    make_db(p / "state.db", [])
    prior(p, "2026-09", notified_cap=True, stopped_by_cap=False)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "spend-check") == 0
    assert not any(c[0] == "systemctl" and c[1] in ("enable", "start") for c in fake.calls)
    assert not any("re-enabled" in c[3] for c in sends(fake))
    assert [c for c in sends(fake) if c[1] == "alice"] == []


# ---- remove ----

def setup_remove(root, tmp_path, token=True):
    p = mk(root)
    caddy = tmp_path / "caddy"
    caddy.mkdir()
    (caddy / "alice.caddy").write_text("x")
    if token:
        c = p / "google-credentials"
        c.mkdir()
        (c / "alice@example.com.json").write_text(json.dumps({"refresh_token": "RT-1"}))
    return p, caddy


def test_remove_revokes_before_delete(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 0
    assert fake.index("revoke_google_token", "RT-1") < fake.index("hermes", "alice", "profile",
                                                                 "delete", "-y", "alice")
    assert fake.index("revoke_google_token") < fake.index("systemctl", "stop", UNIT)


def test_remove_order_and_commands(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    out = run(hermes_root, fake, "remove", "alice", caddy=caddy)
    assert out == 0
    i = fake.index
    assert (i("systemctl", "stop", UNIT) < i("systemctl", "disable", UNIT)
            < i("hermes", "alice", "gateway", "uninstall")
            < i("hermes", "alice", "profile", "delete", "-y", "alice")
            < i("remove_tree", caddy / "alice.caddy") < i("caddy_reload"))


def test_remove_continues_when_revoke_fails(hermes_root, tmp_path, capsys):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("revoke_google_token",)]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists()
    cap = capsys.readouterr()
    assert "revoke" in (cap.out + cap.err).lower() and "injected failure" in (cap.out + cap.err)


def test_remove_without_creds_skips_revoke(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path, token=False)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 0
    assert not any(c[0] == "revoke_google_token" for c in fake.calls)
    assert not p.exists()


def test_remove_tolerates_stop_disable_failure(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("systemctl", "stop"), ("systemctl", "disable")]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert ("hermes", "alice", "gateway", "uninstall") in fake.calls
    assert not p.exists()


def test_remove_leaves_nothing(hermes_root, tmp_path, capsys):
    p, caddy = setup_remove(hermes_root, tmp_path)
    mk(hermes_root, "bob")
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 0
    assert not p.exists() and not (caddy / "alice.caddy").exists()
    assert [f.name for f in list_friends(hermes_root)] == ["bob"]
    assert "delete the bot in BotFather (@BotFather" in capsys.readouterr().out
    assert fake.calls and not any(c[0] == "write_file" for c in fake.calls)  # no archive


def test_remove_profile_delete_failure_falls_back(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("hermes", "alice", "profile", "delete")]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists()
    assert ("remove_tree", p) in fake.calls


def test_remove_leftovers_after_delete_removed(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)

    class Leaky(FakeSystem):
        def hermes(self, name, *args):
            self._rec("hermes", name, *args)  # delete does nothing
            return ""
    fake = Leaky(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 0
    assert not p.exists()


def test_remove_unknown_name_no_calls(hermes_root, tmp_path):
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "nobody", caddy=tmp_path) != 0
    assert run(hermes_root, fake, "remove", "../x", caddy=tmp_path) != 0
    assert fake.calls == []


def test_remove_path_traversal_leaves_victim(hermes_root, tmp_path):
    victim = hermes_root / "profiles" / ".." / ".." / "victim"
    victim.mkdir(parents=True, exist_ok=True)
    Friend(name="victim", telegram_id=1, email="v@example.com", tz="Europe/London", port=8102,
           cap_usd=1.0, activated=True).save(victim)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "../../victim", caddy=tmp_path) != 0
    assert fake.calls == [] and victim.exists()


# ---- list ----

def test_list_shows_estimate_label(hermes_root, capsys):
    a = mk(hermes_root, "alice", cap=5.0)
    mk(hermes_root, "bob", cap=2.5)
    (a / "google-credentials").mkdir()
    (a / "google-credentials" / "alice@example.com.json").write_text("{}")
    set_cost(a, 1.234)
    fake = FakeSystem(hermes_root)
    fake.active = {gateway_unit("alice")}
    assert run(hermes_root, fake, "list") == 0
    assert fake.calls == []
    lines = capsys.readouterr().out.splitlines()
    assert all(l.endswith("ESTIMATE") for l in lines) and len(lines) == 2
    assert lines[0].split() == ["alice", "active", "signed-in", "yes", "$1.23", "of", "$5.00",
                                "ESTIMATE"]
    assert lines[1].split() == ["bob", "inactive", "signed-in", "no", "$0.00", "of", "$2.50",
                                "ESTIMATE"]


def test_list_signed_in_needs_the_friends_own_creds_file(hermes_root, capsys):
    b = mk(hermes_root, "bob")
    (b / "google-credentials").mkdir()
    (b / "google-credentials" / "other@example.com.json").write_text("{}")
    assert run(hermes_root, FakeSystem(hermes_root), "list") == 0
    assert "signed-in no" in capsys.readouterr().out


def test_list_one_bad_friend_does_not_drop_the_rest(hermes_root, capsys):
    mk(hermes_root, "alice")
    bad = mk(hermes_root, "bad")
    (bad / "friend.json").write_text("{not json")
    mk(hermes_root, "carol")
    assert run(hermes_root, FakeSystem(hermes_root), "list") == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("alice  ") and lines[0].endswith("ESTIMATE")
    assert lines[1].startswith("bad  error: ")
    assert lines[2].startswith("carol  ") and lines[2].endswith("ESTIMATE")


def test_list_no_friends(hermes_root, capsys):
    assert run(hermes_root, FakeSystem(hermes_root), "list") == 0
    assert capsys.readouterr().out.strip() == "no friends"


def test_remove_reclaims_after_uninstall_before_delete(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 0
    i = fake.index
    assert (i("hermes", "alice", "gateway", "uninstall") < i("systemctl", "reset-failed", UNIT)
            < i("hermes", "alice", "profile", "delete", "-y", "alice"))
    assert (i("hermes", "alice", "gateway", "uninstall") < i("reclaim", p)
            < i("hermes", "alice", "profile", "delete", "-y", "alice"))


def test_remove_continues_when_reclaim_or_reset_failed_fail(hermes_root, tmp_path, capsys):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("reclaim",), ("systemctl", "reset-failed")]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists()
    err = capsys.readouterr().err
    assert "reclaim" in err and "reset-failed" in err


def test_remove_caddy_reload_failure_still_deletes(hermes_root, tmp_path, capsys):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("caddy_reload",)]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists() and not (caddy / "alice.caddy").exists()
    cap = capsys.readouterr()
    assert "caddy" in cap.err.lower()
    assert "remove finished with warnings; check the lines above" in cap.out


def test_remove_rerun_without_caddy_file_and_failing_uninstall(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    (caddy / "alice.caddy").unlink()
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("hermes", "alice", "gateway", "uninstall")]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists()


def test_remove_clean_run_has_no_warning_line(hermes_root, tmp_path, capsys):
    p, caddy = setup_remove(hermes_root, tmp_path)
    assert run(hermes_root, FakeSystem(hermes_root), "remove", "alice", caddy=caddy) == 0
    assert "warnings" not in capsys.readouterr().out


def test_remove_corrupt_friend_json_still_deletes(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    (p / "friend.json").write_text("{not json")
    (p / "google-credentials" / "oauth_states.json").write_text("{}")
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists() and not (caddy / "alice.caddy").exists()
    assert ("revoke_google_token", "RT-1") in fake.calls  # the single non-state creds file


def test_resume_refuses_unactivated_friend(hermes_root, capsys):
    p = mk(hermes_root)
    f = Friend.load(p)
    f.activated = False
    f.save(p)
    prior(p, MONTH, stopped_by_cap=True)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "resume", "alice") == 2
    assert fake.calls == []
    assert state(p)["stopped_by_cap"] is True
    assert capsys.readouterr().err.count("\n") == 1
