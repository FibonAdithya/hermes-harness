import subprocess


def test_real_gh_is_unreachable():
    r = subprocess.run(["gh", "--version"], capture_output=True, text=True)
    assert r.returncode == 97
    assert "disabled in tests" in r.stderr


def test_real_claude_is_unreachable():
    assert subprocess.run(["claude", "--version"], capture_output=True).returncode == 97
