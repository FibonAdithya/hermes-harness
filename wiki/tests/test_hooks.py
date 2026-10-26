import json

from hermes_wiki.hooks import install_session_end_hook

CMD = "/home/u/.local/bin/hermes-wiki session-end"
ORIGINAL = {
    "permissions": {"allow": ["Bash(ls)"]},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]},
}


def test_hook_added_once_and_preserves_other_settings(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    assert install_session_end_hook(f, CMD) is True
    data = json.loads(f.read_text())
    assert data["permissions"] == ORIGINAL["permissions"]
    assert data["hooks"]["PreToolUse"] == ORIGINAL["hooks"]["PreToolUse"]
    assert data["hooks"]["SessionEnd"] == [{"hooks": [{"type": "command", "command": CMD, "timeout": 10}]}]
    backup = tmp_path / "settings.json.bak-wiki"
    assert json.loads(backup.read_text()) == ORIGINAL
    before = f.read_text()
    assert install_session_end_hook(f, CMD) is False
    assert f.read_text() == before
    assert len(json.loads(before)["hooks"]["SessionEnd"]) == 1


def test_backup_written_once(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    install_session_end_hook(f, CMD)
    backup = tmp_path / "settings.json.bak-wiki"
    first = backup.read_text()
    install_session_end_hook(f, CMD + " --x")
    assert backup.read_text() == first
    assert len(json.loads(f.read_text())["hooks"]["SessionEnd"]) == 2


def test_hook_on_missing_file(tmp_path):
    f = tmp_path / "sub" / "settings.json"
    assert install_session_end_hook(f, CMD) is True
    assert json.loads(f.read_text()) == {
        "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": CMD, "timeout": 10}]}]}
    }
    assert not (tmp_path / "sub" / "settings.json.bak-wiki").exists()
