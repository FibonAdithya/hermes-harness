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


def test_mode_preserved(tmp_path):
    import os, stat
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    os.chmod(f, 0o600)
    assert install_session_end_hook(f, CMD) is True
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_new_file_is_mode_600(tmp_path):
    import os, stat
    old = os.umask(0o022)
    try:
        f = tmp_path / "settings.json"
        install_session_end_hook(f, CMD)
    finally:
        os.umask(old)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_symlink_kept_and_target_updated(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert install_session_end_hook(link, CMD) is True
    assert link.is_symlink()
    assert json.loads(real.read_text())["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == CMD
    assert json.loads((tmp_path / "dotfiles" / "settings.json.bak-wiki").read_text()) == ORIGINAL
    assert not list(tmp_path.glob("*.tmp-wiki")) and not list(real.parent.glob("*.tmp-wiki"))


def test_tmp_removed_on_failure(tmp_path, monkeypatch):
    import os
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(ORIGINAL), encoding="utf-8")

    def boom(*a):
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", boom)
    try:
        install_session_end_hook(f, CMD)
    except OSError:
        pass
    assert not list(tmp_path.glob("*.tmp-wiki"))
    assert json.loads(f.read_text()) == ORIGINAL
