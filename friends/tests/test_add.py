import json
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from conftest import OWNER_TOKEN
from fakesystem import FakeSystem
from hermes_friends.cli import main
from hermes_friends.owner import owner_google_emails
from hermes_friends.policy import CADDY_FILE_MODE, gateway_unit

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))
TOKEN = "333333:" + "A" * 35


@pytest.fixture
def env(hermes_root, tmp_path):
    return hermes_root, FakeSystem(hermes_root), tmp_path / "caddy"


def run_add(env, name="alice", token=TOKEN, email="alice@example.com", tid="4242", extra=()):
    root, fake, caddy = env
    argv = ["add", name, "--telegram-id", tid, "--bot-token", token,
            "--tz", "Europe/London", "--email", email, *extra]
    return main(argv, system=fake, root=root, caddy_dir=caddy, now=NOW)


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_add_happy_path_call_order(env, capsys):
    root, fake, caddy = env
    assert run_add(env) == 0
    create = fake.index("hermes", None, "profile", "create", "alice", "--no-alias", "--no-skills")
    first_write = fake.index("write_file")
    caddy_write = fake.index("write_file", caddy / "alice.caddy", CADDY_FILE_MODE)
    reload_ = fake.index("caddy_reload")
    assert caddy_write < reload_
    install = fake.index("hermes", "alice", "gateway", "install", "--start-now", "--start-on-login")
    assert create < first_write < reload_ < install
    assert all(c[1] == (None if c[2] == "profile" else "alice")
               for c in fake.calls if c[0] == "hermes")
    out = capsys.readouterr().out
    assert "https://auth.fibonadithya.me/alice/oauth2callback" in out
    assert "/start" in out
    p = root / "profiles" / "alice"
    assert json.loads((p / "friend.json").read_text())["activated"] is False
    assert "TELEGRAM_ALLOWED_USERS=4242" in (p / ".env").read_text()
    assert "SEEDED" not in (p / ".env").read_text()
    assert (p / "SOUL.md").read_text() != "default soul\n"
    assert (p / "memories" / "MEMORY.md").is_file()
    assert (caddy / "alice.caddy").is_file()


def test_every_hermes_call_sets_home(env):
    root, fake, _ = env
    assert run_add(env) == 0
    creds = root / "profiles" / "alice" / "google-credentials"
    creds.mkdir()
    (creds / "alice@example.com.json").write_text("{}")
    assert main(["activate", "alice"], system=fake, root=root, now=NOW) == 0
    hermes_calls = [c for c in fake.calls if c[0] == "hermes"]
    # profile create/delete run as the owner; everything else in the friend's HERMES_HOME
    assert hermes_calls and all(c[1] == (None if c[2] == "profile" else "alice")
                                for c in hermes_calls)
    assert any(c[2] == "gateway" for c in hermes_calls)


def test_add_refuses_existing_name_without_side_effects(env):
    root, fake, _ = env
    (root / "profiles" / "alice").mkdir()
    assert run_add(env) != 0
    assert fake.calls == []


def test_add_refuses_reused_bot_token(env):
    _, fake, _ = env
    assert run_add(env, token=OWNER_TOKEN) != 0
    assert fake.calls == []


def test_add_refuses_invalid_input(env):
    _, fake, _ = env
    assert run_add(env, name="Bad Name") != 0
    assert run_add(env, tid="-5") != 0
    assert fake.calls == []


def test_add_refuses_low_memory_boundary(env):
    root, fake, _ = env
    fake.mem_mb = 1267
    assert run_add(env) != 0
    assert fake.calls == []
    fake.mem_mb = 1268
    assert run_add(env) == 0


def test_add_refuses_owner_email_from_config(env):
    root, fake, _ = env
    (root / "config.yaml").write_text(yaml.safe_dump(
        {"mcp_servers": {"google": {"env": {"USER_GOOGLE_EMAIL": "Owner@Example.com"}}}}))
    assert run_add(env, email="owner@example.com") != 0
    assert fake.calls == []


def test_add_refuses_owner_email_from_credentials_filename(env):
    root, fake, _ = env
    d = root.parent / ".google_workspace_mcp" / "credentials"
    d.mkdir(parents=True)
    (d / "boss@example.com.json").write_text("{}")
    assert run_add(env, email="BOSS@example.com") != 0
    assert fake.calls == []


def test_owner_google_emails_missing_sources(hermes_root, tmp_path):
    assert owner_google_emails(hermes_root, tmp_path) == set()


def test_owner_google_emails_ignores_oauth_states_and_non_emails(hermes_root, tmp_path):
    d = tmp_path / ".google_workspace_mcp" / "credentials"
    d.mkdir(parents=True)
    (d / "oauth_states.json").write_text("{}")
    (d / "notes.json").write_text("{}")
    (d / "A@B.com.json").write_text("{}")
    assert owner_google_emails(hermes_root, tmp_path) == {"a@b.com"}


def test_owner_google_emails_config_without_google(hermes_root, tmp_path):
    (hermes_root / "config.yaml").write_text("model: x\n")
    assert owner_google_emails(hermes_root, tmp_path) == set()


def test_add_rolls_back_on_gateway_failure(env):
    root, fake, caddy = env
    fake.fail_on.append(("hermes", "alice", "gateway", "install"))
    assert run_add(env) != 0
    assert not (root / "profiles" / "alice").exists()
    assert not (caddy / "alice.caddy").exists()
    reloads = [i for i, c in enumerate(fake.calls) if c == ("caddy_reload",)]
    assert len(reloads) == 2
    assert fake.index("hermes", "alice", "gateway", "uninstall") < reloads[1]
    # the rollback's profile delete runs as the owner, like the create
    assert fake.index("hermes", None, "profile", "delete", "-y", "alice") > reloads[1]


def test_rollback_continues_when_a_step_fails(env):
    root, fake, caddy = env
    fake.fail_on.append(("hermes", "alice", "gateway", "install"))
    fake.fail_on.append(("hermes", "alice", "gateway", "uninstall"))
    assert run_add(env) != 0
    assert not (root / "profiles" / "alice").exists()


def test_add_rolls_back_on_caddy_reload_failure(env):
    root, fake, caddy = env
    fake.fail_on.append(("caddy_reload",))
    assert run_add(env) != 0
    assert not (root / "profiles" / "alice").exists()
    assert not (caddy / "alice.caddy").exists()


def test_written_modes(env):
    root, _, caddy = env
    assert run_add(env) == 0
    p = root / "profiles" / "alice"
    assert mode(p / "config.yaml") == 0o600
    assert mode(p / ".env") == 0o600
    assert mode(caddy / "alice.caddy") == CADDY_FILE_MODE == 0o644


def test_profile_dir_is_0700(env):
    root, _, _ = env
    assert run_add(env) == 0
    assert mode(root / "profiles" / "alice") == 0o700


def test_second_friend_gets_distinct_port(env):
    root, _, _ = env
    assert run_add(env) == 0
    assert run_add(env, name="bob", token="444444:" + "B" * 35, email="bob@example.com") == 0
    ports = {json.loads((root / "profiles" / n / "friend.json").read_text())["port"]
             for n in ("alice", "bob")}
    assert len(ports) == 2


def test_no_unlabelled_dollar_in_output(env, capsys):
    run_add(env)
    assert "$" not in capsys.readouterr().out


def test_add_lowercases_email(env):
    root, _, _ = env
    assert run_add(env, email="Alice@Example.com") == 0
    assert json.loads((root / "profiles" / "alice" / "friend.json").read_text())["email"] \
        == "alice@example.com"


@pytest.mark.parametrize("cap", ["nan", "inf", "-inf", "0"])
def test_add_rejects_bad_cap(env, cap):
    _, fake, _ = env
    assert run_add(env, extra=("--cap", cap)) != 0
    assert fake.calls == []


def test_add_cleans_partial_dir_when_create_fails(env):
    root, fake, _ = env
    orig = fake.hermes

    def partial(name, *args):
        if args[:2] == ("profile", "create"):
            (root / "profiles" / name).mkdir()
            raise RuntimeError("boom")
        return orig(name, *args)

    fake.hermes = partial
    assert run_add(env) != 0
    assert not (root / "profiles" / "alice").exists()


def test_caddy_write_failure_still_cleans_up(env):
    root, fake, caddy = env
    caddy.mkdir()
    orig = fake.write_file

    def w(path, data, mode):
        orig(path, data, mode)
        if str(path).endswith(".caddy"):
            raise RuntimeError("disk")

    fake.write_file = w
    assert run_add(env) != 0
    assert not (caddy / "alice.caddy").exists()
