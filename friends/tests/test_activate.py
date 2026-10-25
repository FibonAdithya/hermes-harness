import json
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from fakesystem import FakeSystem
from hermes_friends.cli import main
from hermes_friends.policy import gateway_unit
from hermes_friends.render import GOOGLE_TOOLS

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))
TOKEN = "333333:" + "A" * 35


def setup(hermes_root, tmp_path):
    fake = FakeSystem(hermes_root)
    argv = ["add", "alice", "--telegram-id", "4242", "--bot-token", TOKEN,
            "--tz", "Europe/London", "--email", "alice@example.com"]
    assert main(argv, system=fake, root=hermes_root, caddy_dir=tmp_path / "caddy", now=NOW) == 0
    fake.calls.clear()
    return fake, hermes_root / "profiles" / "alice"


def test_activate_requires_credentials(hermes_root, tmp_path):
    fake, p = setup(hermes_root, tmp_path)
    before = (p / "config.yaml").read_text()
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) != 0
    assert (p / "config.yaml").read_text() == before
    assert json.loads((p / "friend.json").read_text())["activated"] is False
    assert fake.calls == []


def test_activate_unknown_friend(hermes_root, tmp_path):
    fake = FakeSystem(hermes_root)
    assert main(["activate", "nobody"], system=fake, root=hermes_root, now=NOW) != 0
    assert fake.calls == []


def test_activate_switches_tools(hermes_root, tmp_path):
    fake, p = setup(hermes_root, tmp_path)
    (p / "google-credentials").mkdir()
    (p / "google-credentials" / "alice@example.com.json").write_text('{"refresh_token": "r"}')
    assert main(["activate", "alice"], system=fake, root=hermes_root, now=NOW) == 0
    cfg = yaml.safe_load((p / "config.yaml").read_text())
    assert cfg["mcp_servers"]["google"]["tools"]["include"] == list(GOOGLE_TOOLS)
    assert json.loads((p / "friend.json").read_text())["activated"] is True
    assert "start_google_auth" not in (p / "memories" / "MEMORY.md").read_text()
    assert ("systemctl", "restart", gateway_unit("alice")) in fake.calls
    assert oct(p.joinpath("config.yaml").stat().st_mode & 0o777) == oct(0o600)
