"""run_talos with `resume`: continue a Talos job that stopped, in the directory it started in."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import boxlib

HOME = Path("/home/adi")
VERB = Path(__file__).resolve().parents[1] / "verbs" / "run_talos"
JOB = "20260924-230101-knapsack"


def test_talos_resume_command_shape():
    wd, argv, secs = boxlib.talos_resume_command(HOME, JOB, "modal")
    assert wd == HOME / "talos-modal"
    assert argv == [str(HOME / "talos-modal" / ".venv" / "bin" / "talos"), "run", "--resume", JOB, "--yes"]
    assert secs == 12 * 3600


def test_talos_resume_command_accepts_talos_job_ids():
    for job in (JOB, "20260924-230101-knapsack-2", "20260101-000000-energy_arbitrage"):
        _, argv, _ = boxlib.talos_resume_command(HOME, job, "local")
        assert argv[2:4] == ["--resume", job]


def test_talos_resume_command_refuses_bad_inputs():
    # Talos joins the id onto runs/ unchecked, so anything path-like must stop here.
    for bad in ["", "..", "../x", f"{JOB}/../..", f"/{JOB}", "20260924-230101-not_a_challenge",
                "2026092-230101-knapsack", f"{JOB}\n", f"{JOB}-x", 7]:
        with pytest.raises(ValueError):
            boxlib.talos_resume_command(HOME, bad, "local")
    with pytest.raises(ValueError):
        boxlib.talos_resume_command(HOME, JOB, "c3")


# --- the verb, end to end, with systemd faked on PATH -------------------------

def _fake_bin(tmp_path, active_units=()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    run = bin_dir / "systemd-run"
    run.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'systemd-run.argv'}\n")
    ctl = bin_dir / "systemctl"
    ctl.write_text("#!/bin/sh\ncase \"$3\" in " + "|".join(active_units or ["__none__"])
                   + ") echo active; exit 0;; esac\necho inactive; exit 3\n")
    for f in (run, ctl):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _home(tmp_path, backend="local", job=JOB, files=("job.json", "state.json")):
    wd = tmp_path / f"talos-{backend}"
    (wd / "runs" / job).mkdir(parents=True)
    (wd / "talos.config.json").write_text("{}")
    for f in files:
        (wd / "runs" / job / f).write_text("{}")
    return wd


def _run(tmp_path, args, active_units=()):
    env = {"HOME": str(tmp_path), "PATH": f"{_fake_bin(tmp_path, active_units)}:/usr/bin:/bin"}
    proc = subprocess.run([str(VERB)], input=json.dumps(args).encode(), env=env,
                          capture_output=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


def _nights(tmp_path):
    root = tmp_path / "nights"
    return sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []


def test_verb_resumes_in_the_backend_directory(tmp_path):
    _home(tmp_path, "modal")
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "modal"})
    assert rc == 0, out
    [d] = _nights(tmp_path)
    assert d.name == out["id"] and out["id"].startswith("talos-")
    meta = json.loads((d / "meta.json").read_text())
    assert meta["resume"] == JOB and meta["backend"] == "modal"
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert f"--property=WorkingDirectory={tmp_path / 'talos-modal'}" in sd
    assert sd[-3:] == ["--resume", JOB, "--yes"]


def test_verb_refuses_a_job_that_is_not_there(tmp_path):
    _home(tmp_path, "local", files=("job.json",))  # refused before its first save
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "local"})
    assert rc == 2 and "state.json" in out["error"]
    rc, out = _run(tmp_path, {"resume": "20260924-230101-hypergraph", "backend": "local"})
    assert rc == 2 and "no talos job" in out["error"]
    # The job lives under the backend it started on; the other backend has no such job.
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "modal"})
    assert rc == 2
    assert _nights(tmp_path) == []


def test_verb_refuses_new_run_arguments_alongside_resume(tmp_path):
    """Talos takes challenge, direction and budget from the job's job.json on a
    resume; passing them would look like they changed something."""
    _home(tmp_path)
    for extra in ({"direction": "x"}, {"challenge": "knapsack"}, {"iterations": 5}):
        rc, out = _run(tmp_path, {"resume": JOB, "backend": "local", **extra})
        assert rc == 2 and "resume" in out["error"], extra
    assert _nights(tmp_path) == []


def test_verb_refuses_while_a_talos_night_runs_on_that_backend(tmp_path):
    """Talos keeps no lock on a job: two processes on one state.json corrupt it.
    The night that owns the job may be the original run, whose meta cannot name
    the job id Talos minted, so any live Talos night on the backend blocks."""
    _home(tmp_path, "local")
    busy = "talos-20260924-2300-aaaa"
    d = boxlib.night_dir(tmp_path / "nights", busy)
    d.mkdir(parents=True)
    boxlib.record_start(d, {"kind": "talos", "backend": "local", "challenge": "knapsack"})
    boxlib.write_status(d, "running")

    rc, out = _run(tmp_path, {"resume": JOB, "backend": "local"}, active_units=[f"night-{busy}"])
    assert rc == 2 and busy in out["error"]
    assert _nights(tmp_path) == [d]

    # The same night with its unit gone (stale) does not block,
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "local"})
    assert rc == 0, out
    # nor does a live night on the other backend.
    _home(tmp_path, "modal")
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "modal"}, active_units=[f"night-{busy}"])
    assert rc == 0, out


def test_verb_new_run_is_unchanged(tmp_path):
    _home(tmp_path)
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "greedy", "iterations": 3})
    assert rc == 0, out
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert sd[-8:] == ["run", "--challenge", "knapsack", "--direction", "greedy",
                       "--budget-iterations", "3", "--yes"]
    assert "resume" not in json.loads((_nights(tmp_path)[0] / "meta.json").read_text())


def test_a_night_that_names_no_backend_blocks_both(tmp_path):
    """Talos nights started before meta recorded the backend could be on either."""
    _home(tmp_path, "modal")
    busy = "talos-20260924-2300-bbbb"
    d = boxlib.night_dir(tmp_path / "nights", busy)
    d.mkdir(parents=True)
    boxlib.record_start(d, {"kind": "talos", "challenge": "knapsack", "timer": True})
    boxlib.write_status(d, "running")
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "modal"}, active_units=[f"night-{busy}"])
    assert rc == 2 and busy in out["error"]


def test_timer_night_records_its_backend(tmp_path, monkeypatch):
    import night

    root = tmp_path / "nights"
    root.mkdir()
    (root / "config.toml").write_text('[talos]\niterations = 2\nbackend = "modal"\n'
                                      '[[talos.queue]]\nchallenge = "knapsack"\ndirection = "a"\n')
    _fake_bin(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert night.main(["night.py", "talos"]) == 0
    [d] = [d for d in root.iterdir() if d.is_dir()]
    assert json.loads((d / "meta.json").read_text())["backend"] == "modal"
