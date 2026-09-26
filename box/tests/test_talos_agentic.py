"""run_talos(mode="agentic"): Talos hands each iteration to a claude session."""
import json
from pathlib import Path

import pytest

import boxlib
from test_pause import _start_night
from test_talos_resume import JOB, _home, _nights, _run

HOME = Path("/home/adi")


def test_agentic_adds_the_mode_flag_and_single_shot_does_not():
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3", mode="agentic")
    assert argv[argv.index("--mode") + 1] == "agentic"
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3")
    assert "--mode" not in argv
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3", mode="single-shot")
    assert "--mode" not in argv


def test_unknown_mode_is_refused():
    for bad in ["", "Agentic", "codex", None, 1]:
        with pytest.raises(ValueError):
            boxlib.talos_command(HOME, "knapsack", "x", 1, "local", mode=bad)


def test_stop_timeout_by_mode():
    assert boxlib.talos_stop_sec("single-shot") == 600
    # a claude session runs up to 30 minutes before Talos sees the stop
    assert boxlib.talos_stop_sec("agentic") == 2400


def test_start_night_uses_the_given_stop_timeout(tmp_path, monkeypatch):
    argv = _start_night(tmp_path, monkeypatch, "talos", stop_sec=2400)
    assert "--property=TimeoutStopSec=2400" in argv
    assert "--property=TimeoutStopSec=600" not in argv


def test_verb_starts_an_agentic_night(tmp_path):
    _home(tmp_path, backend="c3")
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "d", "iterations": 3,
                              "backend": "c3", "mode": "agentic"})
    assert rc == 0, out
    [d] = _nights(tmp_path)
    assert json.loads((d / "meta.json").read_text())["mode"] == "agentic"
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert sd[sd.index("--mode") + 1] == "agentic"
    assert "--property=TimeoutStopSec=2400" in sd


def test_verb_single_shot_by_default(tmp_path):
    _home(tmp_path)
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "d", "iterations": 3})
    assert rc == 0, out
    [d] = _nights(tmp_path)
    assert json.loads((d / "meta.json").read_text())["mode"] == "single-shot"
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert "--mode" not in sd and "--property=TimeoutStopSec=600" in sd


def test_verb_refuses_a_bad_mode_and_mode_with_resume(tmp_path):
    _home(tmp_path)
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "d", "mode": "turbo"})
    assert rc == 2 and "mode" in out["error"]
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "local", "mode": "agentic"})
    assert rc == 2 and "mode" in out["error"]
    assert _nights(tmp_path) == []


def test_a_resumed_night_gets_the_agentic_stop_timeout(tmp_path):
    """The verb cannot tell which mode the job started in, so it assumes the slower one."""
    _home(tmp_path)
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "local"})
    assert rc == 0, out
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert "--property=TimeoutStopSec=2400" in sd
