import json
import stat

import pytest

from conftest import APPROVALS_TOKEN, OWNER_TOKEN
from hermes_friends.model import Friend
from hermes_friends.policy import PORTS
from hermes_friends.registry import allocate_port, bot_token_in_use, list_friends, profile_dir


def mk(name, port):
    return Friend(name, 1000 + port, f"{name}@example.com", "Asia/Kolkata", port, 5.0, False)


def add(root, f):
    d = profile_dir(root, f.name)
    d.mkdir(parents=True, exist_ok=True)
    f.save(d)


def test_list_friends_ignores_plain_profiles(hermes_root):
    profile_dir(hermes_root, "plain").mkdir()
    add(hermes_root, mk("zed", 8101))
    add(hermes_root, mk("amy", 8102))
    assert [f.name for f in list_friends(hermes_root)] == ["amy", "zed"]


def test_allocate_port_fills_gap():
    assert allocate_port([mk("a", 8101), mk("b", 8103)]) == 8102
    assert allocate_port([]) == 8101
    with pytest.raises(RuntimeError):
        allocate_port([mk(f"f{p}", p) for p in PORTS])


def test_all_99_ports():
    assert len(PORTS) == 99


def test_bot_token_in_use_checks_owner_env(hermes_root):
    assert bot_token_in_use(hermes_root, OWNER_TOKEN)
    assert not bot_token_in_use(hermes_root, "333333:" + "Z" * 35)


def test_bot_token_in_use_checks_profile_env(hermes_root):
    add(hermes_root, mk("amy", 8101))
    tok = "444444:" + "Q" * 35
    (profile_dir(hermes_root, "amy") / ".env").write_text(f"TELEGRAM_BOT_TOKEN={tok}\n")
    assert bot_token_in_use(hermes_root, tok)


def test_bot_token_in_use_checks_approvals_bot(hermes_root):
    assert bot_token_in_use(hermes_root, APPROVALS_TOKEN)
    (hermes_root / "broker" / "broker.json").unlink()
    assert not bot_token_in_use(hermes_root, APPROVALS_TOKEN)


def test_friend_roundtrip_and_mode(tmp_path):
    f = mk("amy", 8101)
    f.save(tmp_path)
    assert Friend.load(tmp_path) == f
    assert stat.S_IMODE((tmp_path / "friend.json").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "friend.json").read_text())["name"] == "amy"


def test_bot_token_in_use_parses_export_and_comment(hermes_root):
    tok = "555555:" + "E" * 35
    (hermes_root / ".env").write_text(f"export TELEGRAM_BOT_TOKEN={tok}\n")
    assert bot_token_in_use(hermes_root, tok)
    tok2 = "666666:" + "C" * 35
    (hermes_root / ".env").write_text(f"TELEGRAM_BOT_TOKEN={tok2} # main bot\n")
    assert bot_token_in_use(hermes_root, tok2)


def test_save_tightens_existing_mode(tmp_path):
    p = tmp_path / "friend.json"
    p.write_text("{}")
    p.chmod(0o644)
    mk("amy", 8101).save(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
