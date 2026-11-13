"""droplet/harness-pull.sh with stub git, uv and systemctl first on PATH (never the real ones)."""
import collections
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "droplet" / "harness-pull.sh"
WANT = "0123456789abcdef0123456789abcdef01234567"

GIT = """#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/git.log"
case "$*" in
  "rev-parse origin/master") echo "$WANT_SHA" ;;
  "rev-parse --short HEAD") echo abc1234 ;;
esac
exit 0
"""
LOGGER = """#!/usr/bin/env bash
echo "$(basename "$PWD"): $*" >> "$STUB_DIR/NAME.log"
exit 0
"""


def _run(tmp_path, record_age=None, state="record"):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "git").write_text(GIT)
    for name in ("uv", "systemctl"):
        (stubs / name).write_text(LOGGER.replace("NAME", name))
    for f in stubs.iterdir():
        f.chmod(0o755)
    home = tmp_path / "home"
    (home / "hermes-harness" / "broker").mkdir(parents=True)
    (home / "hermes-harness" / "friends").mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({"state_dir": str(state_dir)}))
    path = state_dir / "deploy_ask.json"
    if state == "record":
        path.write_text(json.dumps({"asked_sha": "x", "deploy_running": {
            "sha": "y", "started_at": time.time() - record_age}}))
    elif state == "corrupt":
        path.write_text("{not json")
    elif state == "no_record":
        path.write_text(json.dumps({"asked_sha": "x"}))
    env = {**os.environ, "HOME": str(home), "PATH": f"{stubs}:{os.environ['PATH']}",
           "HERMES_BROKER_CONFIG": str(config), "STUB_DIR": str(tmp_path), "WANT_SHA": WANT}
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    logs = collections.defaultdict(str)
    for name in ("git", "uv", "systemctl"):
        f = tmp_path / f"{name}.log"
        if f.exists():
            logs[name] = f.read_text()
    return r, logs, home


def test_fresh_deploy_record_defers_restart_and_stamp(tmp_path):
    r, logs, home = _run(tmp_path, record_age=300)
    assert r.returncode == 0, r.stderr
    assert "restart hermes-approvals" not in logs["systemctl"]
    assert not (home / ".local/share/harness-deployed.sha").exists()
    assert "deploy running; approvals restart deferred" in r.stdout
    assert "broker" not in logs["uv"]
    assert (home / ".local/share/friends-synced.sha").read_text() == WANT


def test_stale_record_restarts_and_stamps(tmp_path):
    r, logs, home = _run(tmp_path, record_age=1500)
    assert r.returncode == 0, r.stderr
    assert "--user restart hermes-approvals" in logs["systemctl"]
    assert (home / ".local/share/harness-deployed.sha").read_text() == WANT
    assert "broker" in logs["uv"]


@pytest.mark.parametrize("state", ["absent", "corrupt", "no_record"])
def test_unreadable_state_does_not_block(tmp_path, state):
    r, logs, home = _run(tmp_path, state=state)
    assert r.returncode == 0, r.stderr
    assert "--user restart hermes-approvals" in logs["systemctl"]
    assert (home / ".local/share/harness-deployed.sha").read_text() == WANT
