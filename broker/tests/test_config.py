import json

import pytest
from hermes_broker.config import load_config


def write(tmp_path, **overrides):
    data = {
        "owner_telegram_id": 111222333,
        "approvals_bot_token": "123:abc",
        "repos": ["fibonadithya/wgan-synthetic", "fibonadithya/notes-tools"],
        "ssh_targets": {"tig-gpu": "tig-gpu", "tig-server": "tig-server"},
        "state_dir": str(tmp_path / "state"),
    }
    data.update(overrides)
    path = tmp_path / "broker.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_loads(tmp_path):
    cfg = load_config(write(tmp_path))
    assert cfg.owner_telegram_id == 111222333
    assert "fibonadithya/wgan-synthetic" in cfg.repos
    assert cfg.ssh_targets["tig-gpu"] == "tig-gpu"


def test_allowlisted_repo_passes(tmp_path):
    load_config(write(tmp_path)).check_repo("fibonadithya/wgan-synthetic")


def test_unlisted_repo_rejected(tmp_path):
    with pytest.raises(ValueError, match="not allowlisted"):
        load_config(write(tmp_path)).check_repo("someone-else/private")


def test_missing_key_is_an_error(tmp_path):
    path = write(tmp_path)
    path.write_text(json.dumps({"repos": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_empty_owner_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_config(write(tmp_path, owner_telegram_id=0))
