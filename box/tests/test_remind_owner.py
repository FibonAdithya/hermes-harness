"""droplet/remind-owner.sh with a stub `hermes` first on PATH (never the real one)."""
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "droplet" / "remind-owner.sh"

STUB = """#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$STUB_LOG"
exit "${FAKE_RC:-0}"
"""

# Shell-special text must reach the owner byte for byte.
MESSAGE = 'Check run 42: `nvidia-smi`, "$HOME" and C:\\new\\t stay literal\nsecond line'


def _run(tmp_path, args=("2h", "sweep"), message=MESSAGE, rc=0, deliver=None):
    bindir = tmp_path / "stubs"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "hermes"
    stub.write_text(STUB)
    stub.chmod(0o755)
    home = tmp_path / "home"
    scripts = home / ".hermes" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "hermes.log"
    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
           "STUB_LOG": str(log), "FAKE_RC": str(rc)}
    env.pop("REMIND_DELIVER", None)
    if deliver is not None:
        env["REMIND_DELIVER"] = deliver
    r = subprocess.run(["bash", str(SCRIPT), *args], input=message, env=env,
                       capture_output=True, text=True, timeout=30)
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return r, calls, scripts


def _opt(call, flag):
    return call[call.index(flag) + 1]


def test_creates_one_shot_no_agent_job_whose_script_prints_message_and_cleans_up(tmp_path):
    r, calls, scripts = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert len(calls) == 1
    call = calls[0]
    assert call[:2] == ["cron", "create"]
    assert _opt(call, "--repeat") == "1"
    assert "--no-agent" in call
    assert _opt(call, "--deliver") == "telegram:8887384436"
    assert _opt(call, "--name") == "reminder: sweep"
    assert call[-1] == "2h"
    job_script = scripts / _opt(call, "--script")
    assert job_script.is_file()
    assert sorted(p.suffix for p in scripts.iterdir()) == [".sh", ".txt"]
    out = subprocess.run(["bash", str(job_script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert out.stdout == MESSAGE + "\n"
    assert list(scripts.iterdir()) == []


def test_deliver_target_can_be_overridden(tmp_path):
    r, calls, _ = _run(tmp_path, deliver="telegram:1")
    assert r.returncode == 0, r.stderr
    assert _opt(calls[0], "--deliver") == "telegram:1"


def test_blank_message_is_refused_without_calling_hermes(tmp_path):
    r, calls, scripts = _run(tmp_path, message=" \n\t\n")
    assert r.returncode == 2
    assert calls == []
    assert list(scripts.iterdir()) == []


def test_failed_create_removes_files_and_fails(tmp_path):
    r, calls, scripts = _run(tmp_path, rc=3)
    assert r.returncode == 1
    assert len(calls) == 1
    assert list(scripts.iterdir()) == []
    assert "no reminder set" in r.stderr


def test_missing_name_is_refused_without_calling_hermes(tmp_path):
    r, calls, scripts = _run(tmp_path, args=("2h",))
    assert r.returncode != 0
    assert calls == []
    assert list(scripts.iterdir()) == []
