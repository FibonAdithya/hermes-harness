"""Final-review fix wave: one section per brief item."""
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from fakesystem import FakeSystem
from hermes_friends.cli import main
from hermes_friends.model import Friend
from hermes_friends.owner import normalize_email
from hermes_friends.policy import gateway_unit
from hermes_friends.render import render_memory
from hermes_friends.system import System, containers_under
from hermes_friends.validate import validate_email
from test_activate import NOW, TOKEN, setup
from test_manage import UNIT, mk, run, setup_remove, sysctl
from test_spend import MONTH, make_db, prior, sends, set_cost, state
from test_spend import run as spend_run


def msgs_to(fake, who):
    return [c[3] for c in sends(fake) if c[1] == who]


# ---- 2: no 80% warning after a cap stop ----

@pytest.mark.parametrize("second_cost", [5.0, 6.0])
def test_no_warning_after_cap_stop(hermes_root, second_cost):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    set_cost(p, 5.0)
    assert spend_run(hermes_root, fake, NOW) == 0
    assert state(p)["warned"] is True
    n = len(sends(fake))
    set_cost(p, second_cost)
    assert spend_run(hermes_root, fake, NOW + timedelta(minutes=15)) == 0
    assert len(sends(fake)) == n
    assert not any("80%" in c[3] for c in sends(fake))


def test_no_warning_after_carried_new_month_stop(hermes_root):
    p = mk(hermes_root, cap=0.0)
    fake = FakeSystem(hermes_root)
    prior(p, "2026-09", stopped_by_cap=True, notified_cap=True)
    make_db(p / "state.db", [])
    assert spend_run(hermes_root, fake, NOW) == 0
    assert state(p)["warned"] is True
    assert spend_run(hermes_root, fake, NOW + timedelta(minutes=15)) == 0
    assert sends(fake) == []


# ---- 3: resume / cap clear old samples ----

def test_resume_clears_samples_so_old_baseline_cannot_restop(hermes_root):
    p = mk(hermes_root)
    set_cost(p, 1.5)
    prior(p, MONTH, stopped_by_runaway=True, notified_runaway=True,
          samples=[[NOW.timestamp() - 3700, 0.0]])
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "resume", "alice") == 0
    assert state(p)["samples"] == []
    fake.calls.clear()
    assert spend_run(hermes_root, fake, NOW + timedelta(minutes=10)) == 0
    assert sysctl(fake) == [] and sends(fake) == []
    assert state(p)["stopped_by_runaway"] is False


def test_cap_restart_clears_samples(hermes_root):
    p = mk(hermes_root)
    set_cost(p, 5.0)
    prior(p, MONTH, stopped_by_cap=True, notified_cap=True, warned=True,
          samples=[[NOW.timestamp() - 3700, 0.0]])
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "cap", "alice", "10") == 0
    assert ("systemctl", "start", UNIT) in fake.calls
    assert state(p)["samples"] == []


# ---- 4: email validation and creds path containment ----

@pytest.mark.parametrize("bad", ["../x@example.com", "a/b@example.com", "..@..", "a@b",
                                 "a@example.com/../x", "a\\b@example.com", "",
                                 "a" * 250 + "@example.com"])
def test_email_traversal_rejected(bad):
    with pytest.raises(ValueError):
        validate_email(bad)


@pytest.mark.parametrize("good", ["alice@example.com", "a.b+c@sub.example.co.uk"])
def test_email_accepted(good):
    assert validate_email(good) == good


def test_activate_refuses_creds_path_outside_profile(hermes_root):
    p = mk(hermes_root, email="../evil@e.com")
    (p / "google-credentials").mkdir()
    (p / "evil@e.com.json").write_text("{}")  # what the unchecked path would resolve to
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "activate", "alice") != 0
    assert fake.calls == []


def test_remove_does_not_revoke_outside_creds(hermes_root, tmp_path):
    p = mk(hermes_root, email="../evil@e.com")
    (p / "google-credentials").mkdir()
    (p / "evil@e.com.json").write_text(json.dumps({"refresh_token": "RT"}))
    fake = FakeSystem(hermes_root)
    run(hermes_root, fake, "remove", "alice", caddy=tmp_path / "caddy")
    assert not any(c[0] == "revoke_google_token" for c in fake.calls)


# ---- 5: activate does not bypass a stop ----

def creds(p):
    (p / "google-credentials").mkdir()
    (p / "google-credentials" / "alice@example.com.json").write_text("{}")


@pytest.mark.parametrize("flag", ["stopped_by_cap", "stopped_by_runaway", "notified_cap",
                                  "notified_runaway"])
def test_activate_held_does_not_restart(hermes_root, tmp_path, flag, capsys):
    fake, p = setup(hermes_root, tmp_path)
    creds(p)
    prior(p, MONTH, **{flag: True})
    capsys.readouterr()
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) == 0
    assert not any(c[:2] == ("systemctl", "restart") for c in fake.calls)
    assert json.loads((p / "friend.json").read_text())["activated"] is True
    assert "gateway stays stopped (cap/runaway hold)" in capsys.readouterr().out


def test_activate_clean_restarts(hermes_root, tmp_path):
    fake, p = setup(hermes_root, tmp_path)
    creds(p)
    prior(p, MONTH)
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) == 0
    assert ("systemctl", "restart", gateway_unit("alice")) in fake.calls


# ---- 6: failed send does not mark notified ----

def test_failed_cap_send_still_stops_not_notified_owner_told(hermes_root):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    fake.send_ok = False
    set_cost(p, 5.0)
    assert spend_run(hermes_root, fake, NOW) == 0
    assert ("systemctl", "stop", UNIT) in fake.calls and ("systemctl", "disable", UNIT) in fake.calls
    st = state(p)
    assert st["stopped_by_cap"] is True and st["notified_cap"] is False
    assert "friends: alice could not be messaged about the stop" in msgs_to(fake, None)
    n = len(msgs_to(fake, "alice"))
    spend_run(hermes_root, fake, NOW + timedelta(minutes=15))
    assert len(msgs_to(fake, "alice")) == n


def test_failed_runaway_send_not_notified(hermes_root):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    fake.send_ok = False
    set_cost(p, 2.0)
    prior(p, MONTH, samples=[[NOW.timestamp() - 3700, 0.0]])
    spend_run(hermes_root, fake, NOW)
    st = state(p)
    assert st["stopped_by_runaway"] is True and st["notified_runaway"] is False


def test_successful_send_no_extra_owner_line(hermes_root):
    p = mk(hermes_root)
    fake = FakeSystem(hermes_root)
    set_cost(p, 5.0)
    spend_run(hermes_root, fake, NOW)
    assert not any("could not be messaged" in c[3] for c in sends(fake))
    assert state(p)["notified_cap"] is True


# ---- 7: subprocess timeout ----

def test_run_passes_timeout_and_names_command_on_expiry(monkeypatch, tmp_path):
    seen = {}

    def boom(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="docker ps"):
        System(tmp_path)._run(["docker", "ps"])
    assert seen["timeout"] == 300


# ---- 8: activate keeps MEMORY.md ----

def test_activate_keeps_appended_memory_line(hermes_root, tmp_path):
    fake, p = setup(hermes_root, tmp_path)
    creds(p)
    mem = p / "memories" / "MEMORY.md"
    assert "start_google_auth" in mem.read_text()
    mem.write_text(mem.read_text() + "Likes tea.\n")
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) == 0
    text = mem.read_text()
    assert "Likes tea." in text and "start_google_auth" not in text
    assert "The user's first name is Alice." in text


def test_activate_missing_memory_writes_activated(hermes_root, tmp_path):
    fake, p = setup(hermes_root, tmp_path)
    creds(p)
    (p / "memories" / "MEMORY.md").unlink()
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) == 0
    f = Friend.load(p)
    assert (p / "memories" / "MEMORY.md").read_text() == render_memory(f)
    assert "start_google_auth" not in render_memory(f)


# ---- 9: gmail alias normalisation ----

def test_normalize_email():
    assert normalize_email("First.Last+x@googlemail.com") == "firstlast@gmail.com"
    assert normalize_email("a.b@gmail.com") == "ab@gmail.com"
    assert normalize_email("first.last@example.com") == "first.last@example.com"
    assert normalize_email("Bob+t@Example.com") == "bob+t@example.com"


def test_add_refuses_gmail_alias_of_owner(hermes_root, tmp_path):
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"mcp_servers": {"google": {"env": {"USER_GOOGLE_EMAIL": "firstlast@gmail.com"}}}}))
    fake = FakeSystem(hermes_root)
    argv = ["add", "alice", "--telegram-id", "4242", "--bot-token", TOKEN, "--tz", "Europe/London",
            "--email", "First.Last+x@googlemail.com"]
    assert main(argv, system=fake, root=hermes_root, caddy_dir=tmp_path / "c", now=NOW) == 1
    assert fake.calls == []


def test_add_allows_dotted_non_gmail(hermes_root, tmp_path):
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"mcp_servers": {"google": {"env": {"USER_GOOGLE_EMAIL": "firstlast@example.com"}}}}))
    fake = FakeSystem(hermes_root)
    argv = ["add", "alice", "--telegram-id", "4242", "--bot-token", TOKEN, "--tz", "Europe/London",
            "--email", "first.last@example.com"]
    assert main(argv, system=fake, root=hermes_root, caddy_dir=tmp_path / "c", now=NOW) == 0


# ---- 10: add rollback resets the failed unit ----

def test_add_rollback_resets_failed_unit(hermes_root, tmp_path):
    fake = FakeSystem(hermes_root)
    fake.fail_on.append(("hermes", "alice", "gateway", "install"))
    argv = ["add", "alice", "--telegram-id", "4242", "--bot-token", TOKEN, "--tz", "Europe/London",
            "--email", "alice@example.com"]
    assert main(argv, system=fake, root=hermes_root, caddy_dir=tmp_path / "c", now=NOW) == 1
    assert (fake.index("hermes", "alice", "gateway", "uninstall")
            < fake.index("systemctl", "reset-failed", UNIT))


# ---- 11: remove deletes the profile's containers ----

def test_containers_under_selects_only_profile_mounts():
    prof = Path("/h/.hermes/profiles/alice")
    mounts = {
        "c1": ["/h/.hermes/profiles/alice/workspace"],
        "c2": ["/h/.hermes/profiles/alice"],
        "sib": ["/h/.hermes/profiles/alice2/workspace"],
        "own": ["/h/.hermes/sandboxes/x"],
        "none": [],
        "mixed": ["/tmp/x", "/h/.hermes/profiles/alice/skills"],
    }
    assert sorted(containers_under(mounts, prof)) == ["c1", "c2", "mixed"]


def test_system_remove_containers(monkeypatch, tmp_path):
    prof = tmp_path / "profiles" / "alice"
    out = {"c1": f"{prof}/ws\n", "c2": f"{tmp_path}/profiles/alice2/ws\n"}
    calls = []

    def fake_run(self, cmd, env=None):
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return "c1\nc2\n"
        if cmd[:2] == ["docker", "inspect"]:
            return out[cmd[-1]]
        return ""

    monkeypatch.setattr(System, "_run", fake_run)
    assert System(tmp_path).remove_containers(prof) == 1
    assert ["docker", "rm", "-f", "c1"] in calls
    assert not any(c[:2] == ["docker", "rm"] and "c2" in c for c in calls)


def test_remove_deletes_containers_between_reset_failed_and_reclaim(hermes_root, tmp_path):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 0
    i = fake.index
    assert (i("systemctl", "reset-failed", UNIT) < i("remove_containers", p) < i("reclaim", p))


def test_remove_continues_when_container_removal_fails(hermes_root, tmp_path, capsys):
    p, caddy = setup_remove(hermes_root, tmp_path)
    fake = FakeSystem(hermes_root)
    fake.fail_on = [("remove_containers",)]
    assert run(hermes_root, fake, "remove", "alice", caddy=caddy) == 1
    assert not p.exists()
    assert "containers" in capsys.readouterr().err


# ---- re-review fixes ----

def test_inspect_template_has_no_raw_newline_or_backslash(monkeypatch, tmp_path):
    calls = []

    def fake_run(self, cmd, env=None):
        calls.append(cmd)
        return "c1\n" if cmd[:3] == ["docker", "ps", "-aq"] else ""

    monkeypatch.setattr(System, "_run", fake_run)
    System(tmp_path).remove_containers(tmp_path / "p")
    fmt = [c for c in calls if c[:2] == ["docker", "inspect"]][0][3]
    assert "\n" not in fmt and "\\" not in fmt and fmt == "{{range .Mounts}}{{println .Source}}{{end}}"


def test_remove_containers_skips_failed_inspect(monkeypatch, tmp_path):
    prof = tmp_path / "profiles" / "alice"
    calls = []

    def fake_run(self, cmd, env=None):
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return "gone\nc2\n"
        if cmd[:2] == ["docker", "inspect"]:
            if cmd[-1] == "gone":
                raise RuntimeError("no such object")
            return f"{prof}/ws\n"
        return ""

    monkeypatch.setattr(System, "_run", fake_run)
    assert System(tmp_path).remove_containers(prof) == 1
    assert ["docker", "rm", "-f", "c2"] in calls


def test_activate_corrupt_spend_state_changes_nothing(hermes_root, tmp_path):
    fake, p = setup(hermes_root, tmp_path)
    creds(p)
    (p / "friend-spend.json").write_text("{not json")
    before = {f: (p / f).read_text() for f in ("config.yaml", "friend.json", "memories/MEMORY.md")}
    fake.calls.clear()
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) == 1
    assert fake.calls == []
    assert before == {f: (p / f).read_text() for f in before}
