"""run_talos on the c3 backend, and the compute cap every new Talos run carries."""
import json
from pathlib import Path

import pytest

import boxlib
from test_talos_resume import JOB, _fake_bin, _home, _nights, _run

HOME = Path("/home/adi")


def test_c3_runs_in_its_own_directory():
    wd, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3")
    assert wd == HOME / "talos-c3"
    assert argv[0] == str(HOME / "talos-c3" / ".venv" / "bin" / "talos")
    wd, _, _ = boxlib.talos_resume_command(HOME, JOB, "c3")
    assert wd == HOME / "talos-c3"


def test_every_new_run_passes_the_compute_cap():
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3", compute_usd=12.5)
    i = argv.index("--budget-compute-usd")
    assert float(argv[i + 1]) == 12.5
    # Without one, the box's default is passed, never Talos's own $20.
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "modal")
    assert float(argv[argv.index("--budget-compute-usd") + 1]) == boxlib.DEFAULT_TALOS_COMPUTE_USD == 5


def test_compute_cap_bounds():
    assert boxlib.MAX_TALOS_COMPUTE_USD == 90
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3", compute_usd=90)
    assert float(argv[argv.index("--budget-compute-usd") + 1]) == 90
    for bad in [0, -1, 90.01, float("nan"), float("inf"), "5", True, None]:
        with pytest.raises(ValueError):
            boxlib.talos_command(HOME, "knapsack", "x", 1, "c3", compute_usd=bad)


def test_verb_starts_a_c3_night_with_its_cap(tmp_path):
    _home(tmp_path, backend="c3")
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "d", "iterations": 3,
                              "backend": "c3", "compute_usd": 40})
    assert rc == 0, out
    [d] = _nights(tmp_path)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["backend"] == "c3" and meta["compute_usd"] == 40
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert f"--property=WorkingDirectory={tmp_path / 'talos-c3'}" in sd
    assert sd[sd.index("--budget-compute-usd") + 1] == "40.0"


def test_verb_refuses_a_cap_over_the_max(tmp_path):
    _home(tmp_path, backend="c3")
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "d", "iterations": 3,
                              "backend": "c3", "compute_usd": 91})
    assert rc == 2 and "compute_usd" in out["error"]
    assert _nights(tmp_path) == []


def test_verb_refuses_c3_when_not_set_up(tmp_path):
    rc, out = _run(tmp_path, {"challenge": "knapsack", "direction": "d", "backend": "c3"})
    assert rc == 2 and "talos-c3 is not set up" in out["error"]


def test_verb_refuses_a_cap_alongside_resume(tmp_path):
    """A resumed job keeps the cap in its job.json; a new one here would change nothing."""
    _home(tmp_path, backend="c3")
    rc, out = _run(tmp_path, {"resume": JOB, "backend": "c3", "compute_usd": 10})
    assert rc == 2 and "compute_usd" in out["error"]
    assert _nights(tmp_path) == []


def test_timer_refuses_c3(tmp_path, monkeypatch):
    """Paid compute starts only from an approved chat call, never from a timer."""
    import night

    root = tmp_path / "nights"
    root.mkdir()
    cfg = ('[talos]\niterations = 2\nbackend = "c3"\n'
           '[[talos.queue]]\nchallenge = "knapsack"\ndirection = "a"\n')
    (root / "config.toml").write_text(cfg)
    _home(tmp_path, backend="c3")
    _fake_bin(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert night.main(["night.py", "talos"]) == 1
    assert not (tmp_path / "systemd-run.argv").exists()
    [d] = [d for d in root.iterdir() if d.is_dir()]
    assert (d / "status").read_text().strip() == "failed"
    assert "c3" in (d / "log").read_text()
    # The queue entry is not consumed by a night that never ran.
    assert (root / "config.toml").read_text() == cfg


def test_timer_passes_the_configured_cap(tmp_path, monkeypatch):
    import night

    root = tmp_path / "nights"
    root.mkdir()
    (root / "config.toml").write_text('[talos]\niterations = 2\nbackend = "modal"\ncompute_usd = 15\n'
                                      '[[talos.queue]]\nchallenge = "knapsack"\ndirection = "a"\n')
    _fake_bin(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert night.main(["night.py", "talos"]) == 0
    sd = (tmp_path / "systemd-run.argv").read_text().splitlines()
    assert sd[sd.index("--budget-compute-usd") + 1] == "15.0"
