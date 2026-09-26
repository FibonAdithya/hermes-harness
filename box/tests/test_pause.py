"""Pausing a Talos night: SIGINT reaches Talos, which cancels its bench job and saves state.

The wrapper tests run the real run-night.sh around a stand-in child that records
which signal it got, because the whole point is which signal reaches Talos.
"""
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import boxlib

BOX = Path(__file__).resolve().parents[1]
WRAPPER = BOX / "run-night.sh"
VERB = BOX / "verbs" / "pause_talos"
NIGHT = "talos-20260926-0100-abcd"

CHILD = """
import signal, sys, time
from pathlib import Path
out = Path(sys.argv[1])
def on(sig, _):
    with out.open("a") as fh:
        fh.write(signal.Signals(sig).name + "\\n")
    # Talos saves state and exits 1 on a stop; a second signal while stopping is a no-op.
    if not getattr(on, "done", False):
        on.done = True
        time.sleep(0.3)
        sys.exit(1)
signal.signal(signal.SIGINT, on)
signal.signal(signal.SIGTERM, on)
(out.parent / "ready").touch()
time.sleep(20)
sys.exit(3)
"""


def _start(tmp_path, env_extra=None, child_code=CHILD):
    child = tmp_path / "child.py"
    child.write_text(child_code)
    d = tmp_path / "night"
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.Popen([str(WRAPPER), str(d), sys.executable, str(child), str(tmp_path / "got")], env=env)
    deadline = time.time() + 10
    while not (tmp_path / "ready").exists():
        assert time.time() < deadline, "child never became ready"
        time.sleep(0.02)
    return proc, d


def _got(tmp_path):
    return (tmp_path / "got").read_text().split() if (tmp_path / "got").exists() else []


def test_int_is_forwarded_as_int_and_the_night_is_paused(tmp_path):
    proc, d = _start(tmp_path)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=10) == 1
    assert _got(tmp_path) == ["SIGINT"]
    assert (d / "status").read_text().strip() == "paused"
    assert (d / "exit_code").read_text().strip() == "1"


def test_a_second_int_while_stopping_still_ends_paused(tmp_path):
    proc, d = _start(tmp_path)
    proc.send_signal(signal.SIGINT)
    time.sleep(0.1)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=10) == 1
    assert (d / "status").read_text().strip() == "paused"
    assert (d / "exit_code").read_text().strip() == "1"


def test_term_on_a_graceful_night_reaches_the_child_as_int(tmp_path):
    """RuntimeMaxSec sends TERM; a Talos night turns it into Talos's clean stop."""
    proc, d = _start(tmp_path, {"NIGHT_STOP_SIGNAL": "INT"})
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    assert _got(tmp_path) == ["SIGINT"]
    assert (d / "status").read_text().strip() == "killed"


def test_term_on_other_nights_is_unchanged(tmp_path):
    proc, d = _start(tmp_path)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 143
    assert _got(tmp_path) == ["SIGTERM"]
    assert (d / "status").read_text().strip() == "killed"
    assert (d / "exit_code").read_text().strip() == "143"


def test_natural_exit_is_unchanged(tmp_path):
    proc, d = _start(tmp_path, child_code=CHILD.replace("time.sleep(20)\nsys.exit(3)", "sys.exit(0)"))
    assert proc.wait(timeout=10) == 0
    assert (d / "status").read_text().strip() == "done"


# --- systemd properties --------------------------------------------------------

def _start_night(tmp_path, monkeypatch, kind, **kw):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemd-run"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'argv'}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    env = {"HOME": str(tmp_path), "NIGHTS_ROOT": str(tmp_path / "nights")}
    boxlib.start_night(kind, tmp_path, ["/bin/true"], 60, env, **kw)
    return (tmp_path / "argv").read_text().splitlines()


def test_talos_nights_stop_gracefully(tmp_path, monkeypatch):
    argv = _start_night(tmp_path, monkeypatch, "talos")
    assert "--property=KillMode=mixed" in argv
    assert "--property=TimeoutStopSec=600" in argv
    assert "--setenv=NIGHT_STOP_SIGNAL=INT" in argv


def test_other_nights_keep_the_default_stop(tmp_path, monkeypatch):
    argv = _start_night(tmp_path, monkeypatch, "fleet")
    assert not any("KillMode" in a or "TimeoutStopSec" in a or "NIGHT_STOP_SIGNAL" in a for a in argv)


def test_a_deactivating_unit_is_still_live(tmp_path, monkeypatch):
    """While Talos cancels after the time limit, a resume must still be blocked."""
    for state, live in (("active", True), ("deactivating", True), ("activating", True),
                        ("inactive", False), ("failed", False)):
        _fake_systemctl(tmp_path, state)
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
        assert boxlib.unit_active(NIGHT) is live, state


# --- the verb ---------------------------------------------------------------------

def _fake_systemctl(tmp_path, state="active"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ctl = bin_dir / "systemctl"
    ctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {tmp_path / 'systemctl.log'}\n"
                   f"case \"$2\" in is-active) echo {state}; [ {state} = active ] && exit 0; exit 3;; esac\n"
                   "exit 0\n")
    ctl.chmod(ctl.stat().st_mode | stat.S_IEXEC)


def _night(tmp_path, night_id=NIGHT, status="running"):
    d = boxlib.night_dir(tmp_path / "nights", night_id)
    d.mkdir(parents=True)
    boxlib.write_status(d, status)
    return d


def _pause(tmp_path, payload, state="active"):
    _fake_systemctl(tmp_path, state)
    env = {"HOME": str(tmp_path), "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}
    proc = subprocess.run([str(VERB)], input=json.dumps(payload).encode(), env=env,
                          capture_output=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


def _kills(tmp_path):
    log = tmp_path / "systemctl.log"
    return [l for l in log.read_text().splitlines() if " kill " in f" {l} "] if log.exists() else []


def test_pause_signals_only_the_wrapper_with_int(tmp_path):
    _night(tmp_path)
    rc, out = _pause(tmp_path, {"id": NIGHT})
    assert rc == 0, out
    assert out["id"] == NIGHT
    assert _kills(tmp_path) == [f"--user kill --kill-whom=main --signal=SIGINT night-{NIGHT}.service"]


def test_pause_refuses_what_it_should_not_touch(tmp_path):
    _night(tmp_path, "fleet-20260926-0100-abcd")
    _night(tmp_path, "talos-20260926-0100-dddd", status="done")
    for payload in ({"id": "../x"}, {"id": ""}, {},
                    {"id": "fleet-20260926-0100-abcd"},      # not a Talos night
                    {"id": "talos-20260926-0100-eeee"},      # no such night
                    {"id": "talos-20260926-0100-dddd"}):     # already finished
        rc, out = _pause(tmp_path, payload)
        assert rc == 2 and out["error"], payload
    assert _kills(tmp_path) == []


def test_pause_refuses_a_night_whose_unit_is_gone(tmp_path):
    _night(tmp_path)
    rc, out = _pause(tmp_path, {"id": NIGHT}, state="inactive")
    assert rc == 2 and "not running" in out["error"]
    assert _kills(tmp_path) == []


def test_night_status_reports_paused(tmp_path, monkeypatch):
    _night(tmp_path, status="paused")
    _fake_systemctl(tmp_path, "inactive")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    [row] = boxlib.read_status(tmp_path / "nights")
    assert row["status"] == "paused"
