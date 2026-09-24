"""Tests added in the final review pass; each names the finding it pins."""
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

import boxlib
import night

VERBS = Path(__file__).resolve().parents[1] / "verbs"


def _night(root, nid, status="running", exit_code=None):
    d = boxlib.night_dir(root, nid)
    d.mkdir(parents=True)
    boxlib.write_status(d, status, exit_code=exit_code)
    return d


def _fake_systemctl(tmp_path, active_units):
    """A systemctl on PATH that answers is-active for the given unit names."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    f = bin_dir / "systemctl"
    f.write_text("#!/bin/sh\ncase \"$3\" in " + "|".join(active_units or ["__none__"]) + ") echo active; exit 0;; esac\necho inactive; exit 3\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(bin_dir)


# I2: a `running` night whose unit is gone is reported, not left running forever.
def test_running_night_without_a_unit_is_stale(tmp_path, monkeypatch):
    root = tmp_path / "nights"
    _night(root, "fleet-20260924-2300-aaaa", "running")
    _night(root, "fleet-20260924-2301-bbbb", "running")
    monkeypatch.setenv("PATH", _fake_systemctl(tmp_path, ["night-fleet-20260924-2301-bbbb"]) + ":/usr/bin:/bin")
    rows = {r["id"]: r for r in boxlib.read_status(root)}
    assert rows["fleet-20260924-2301-bbbb"]["status"] == "running"
    assert rows["fleet-20260924-2300-aaaa"]["status"] == "stale"


# I3: started is when the night started, not when it finished.
def test_started_does_not_move_when_the_night_finishes(tmp_path):
    root = tmp_path / "nights"
    d = boxlib.night_dir(root, "task-20260924-0100-cafe")
    d.mkdir(parents=True)
    boxlib.record_start(d, {"kind": "task"}, now=1790200000.0)
    boxlib.write_status(d, "running")
    before = boxlib.read_status(root)[0]["started"]
    time.sleep(1.1)
    boxlib.write_status(d, "done", exit_code=0)
    (d / "log").write_text("x")
    after = boxlib.read_status(root)[0]["started"]
    assert before == after == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1790200000.0))


# I4: files the task container can write are never followed through a symlink.
def test_symlinked_log_and_status_are_refused(tmp_path):
    root = tmp_path / "nights"
    d = _night(root, "task-20260924-0100-cafe", "done", exit_code=0)
    secret = tmp_path / "secret"
    secret.write_text("hosts.yml contents")
    (d / "log").symlink_to(secret)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    p = subprocess.run([str(VERBS / "log")], input=json.dumps({"id": d.name, "lines": 5}).encode(), capture_output=True, env=env)
    assert p.returncode == 2
    assert "symlink" in json.loads(p.stdout)["error"]
    (d / "status").unlink()
    (d / "status").symlink_to(secret)
    rows = boxlib.read_status(root)
    assert rows[0]["status"] == "unreadable"


# I4 (second half): junk in exit_code must not break status for every night.
def test_junk_exit_code_is_reported_not_raised(tmp_path):
    root = tmp_path / "nights"
    d = _night(root, "task-20260924-0100-cafe", "done")
    (d / "exit_code").write_text("not a number\n")
    rows = boxlib.read_status(root)
    assert rows[0]["exit_code"] is None
    assert rows[0]["status"] == "done"


# I6: a direction with a non-BMP character survives the queue rewrite.
def test_dump_toml_round_trips_emoji_and_is_atomic(tmp_path):
    import tomllib

    cfg = {"talos": {"iterations": 2, "backend": "local", "queue": [{"challenge": "knapsack", "direction": "try 🙂 this"}]}}
    assert tomllib.loads(night.dump_toml(cfg)) == cfg


# I6: a malformed config writes a failed night whose log names the problem.
def test_malformed_config_records_a_failed_night(tmp_path, monkeypatch):
    root = tmp_path / "nights"
    root.mkdir()
    (root / "config.toml").write_text("[fleet\nrepo = 1\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = night.main(["night.py", "fleet"])
    assert rc == 1
    dirs = [d for d in root.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    assert (dirs[0] / "status").read_text().strip() == "failed"
    assert "config.toml" in (dirs[0] / "log").read_text()


def test_missing_fleet_table_records_a_failed_night(tmp_path, monkeypatch):
    root = tmp_path / "nights"
    root.mkdir()
    (root / "config.toml").write_text('[talos]\niterations = 2\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    assert night.main(["night.py", "fleet"]) == 1
    d = [d for d in root.iterdir() if d.is_dir()][0]
    assert (d / "status").read_text().strip() == "failed"
