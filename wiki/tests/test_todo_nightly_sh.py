"""Tests for droplet/todo-nightly.sh, using fake docker and python3 executables."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "droplet" / "todo-nightly.sh"
SUGGESTIONS = '{"title": "x"}\n'

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *"rm -f"*) exit "${FAKE_RM_RC:-0}" ;;
  *) [ "${FAKE_READ_RC:-0}" = 0 ] || exit "$FAKE_READ_RC"
     printf '%s' "$FAKE_SUGGESTIONS" ;;
esac
"""
FAKE_PYTHON = """#!/bin/sh
echo "python3 $*" >> "$FAKE_LOG"
prev=
for a in "$@"; do
  [ "$prev" = --suggestions ] && cat "$a" > "$FAKE_SEEN"
  prev=$a
done
echo '{"ok": true}'
exit "${FAKE_PY_RC:-0}"
"""


@pytest.fixture
def sandbox(tmp_path):
    for name, body in (("docker", FAKE_DOCKER), ("python3", FAKE_PYTHON)):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(0o755)
    (tmp_path / "home").mkdir()
    return tmp_path


def run(sandbox, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(sandbox / "home"),
        "DOCKER": str(sandbox / "docker"),
        "PYTHON3": str(sandbox / "python3"),
        "SBX_WIKI": str(sandbox / "wiki"),
        "FAKE_LOG": str(sandbox / "log"),
        "FAKE_SEEN": str(sandbox / "seen"),
        "FAKE_SUGGESTIONS": SUGGESTIONS,
    }
    env.update(extra)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    log = (sandbox / "log").read_text().splitlines() if (sandbox / "log").exists() else []
    return r, log


def test_nightly_sh_passes_suggestions_to_python(sandbox):
    r, log = run(sandbox)
    assert r.returncode == 0, r.stderr
    py = [l for l in log if l.startswith("python3 ")]
    assert len(py) == 1
    assert "-m hermes_wiki todo nightly" in py[0]
    assert "--suggestions " in py[0]
    assert (sandbox / "seen").read_text() == SUGGESTIONS
    assert '{"ok": true}' in r.stdout
    assert "--config %s/home/.config/hermes-wiki/config.json" % sandbox in py[0]
    # catches: passing the wrong file, an empty temp file, or swallowing the summary


def test_nightly_sh_deletes_after_success(sandbox):
    r, log = run(sandbox)
    assert r.returncode == 0, r.stderr
    rms = [l for l in log if "rm -f /wiki/.todo-suggestions.jsonl" in l]
    assert len(rms) == 1
    assert f"-v {sandbox}/wiki:/wiki " in rms[0] and ":ro" not in rms[0]
    assert log.index(rms[0]) > max(i for i, l in enumerate(log) if l.startswith("python3 "))
    # catches: never deleting (suggestions re-applied nightly), or deleting before python ran


def test_nightly_sh_keeps_suggestions_on_failure(sandbox):
    r, log = run(sandbox, FAKE_PY_RC="1")
    assert r.returncode != 0
    assert not [l for l in log if "rm -f" in l]
    # catches: deleting suggestions after a failed nightly run, losing them


def test_nightly_sh_docker_read_failure_aborts(sandbox):
    r, log = run(sandbox, FAKE_READ_RC="125")
    assert r.returncode != 0
    assert not [l for l in log if l.startswith("python3 ")]
    assert not [l for l in log if "rm -f" in l]
    # catches: `|| true` masking a failed read, then deleting unread suggestions


def test_nightly_sh_read_is_readonly_and_networkless(sandbox):
    r, log = run(sandbox)
    read = [l for l in log if l.startswith("docker ") and "rm -f" not in l]
    assert len(read) == 1
    assert "--network=none" in read[0] and ":/wiki:ro" in read[0]
    assert "hermes-sandbox:latest" in read[0]
    # catches: mounting the checkout writable for the read, or allowing network
