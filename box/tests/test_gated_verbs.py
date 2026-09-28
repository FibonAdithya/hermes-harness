import json
import subprocess
from pathlib import Path

import pytest

import boxlib

HOME = Path("/home/adi")


def test_fleet_command_shape():
    wd, argv, secs = boxlib.fleet_command(HOME, "fleet-fixture", hours=8)
    assert wd == HOME / "TIG" / "fleet-fixture"
    assert argv[:3] == ["uv", "run", "--project"]
    assert argv[3] == str(HOME / "TIG" / "fleet")
    assert argv[4:] == ["fleet", "run", "--new-run"]
    assert secs == 8 * 3600


def test_fleet_command_refuses_bad_repo_and_hours():
    for bad in ["../x", "a/b", "", "Fleet Fixture"]:
        with pytest.raises(ValueError):
            boxlib.fleet_command(HOME, bad, hours=1)
    for bad in [0, -1, 25]:
        with pytest.raises(ValueError):
            boxlib.fleet_command(HOME, "fleet-fixture", hours=bad)


def test_talos_command_shape():
    wd, argv, secs = boxlib.talos_command(HOME, "knapsack", "try a greedy warm start", 20, "local")
    assert wd == HOME / "talos-local"
    assert argv[0] == str(HOME / "talos-local" / ".venv" / "bin" / "talos")
    assert argv[1:] == ["run", "--challenge", "knapsack", "--direction", "try a greedy warm start",
                        "--budget-iterations", "20", "--budget-compute-usd", "5.0", "--yes"]
    assert secs == 12 * 3600
    wd2, _, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "modal")
    assert wd2 == HOME / "talos-modal"


def test_talos_command_accepts_c007_and_c008():
    for challenge in ("job_scheduling", "energy_arbitrage"):
        _, argv, _ = boxlib.talos_command(HOME, challenge, "x", 1, "local")
        assert argv[2:4] == ["--challenge", challenge]


def test_talos_command_refuses_bad_inputs():
    with pytest.raises(ValueError):
        boxlib.talos_command(HOME, "knapsack", "x", 1, "gpu")
    with pytest.raises(ValueError):
        boxlib.talos_command(HOME, "not a challenge", "x", 1, "local")
    with pytest.raises(ValueError):
        boxlib.talos_command(HOME, "knapsack", "", 1, "local")
    for bad in [0, 501]:
        with pytest.raises(ValueError):
            boxlib.talos_command(HOME, "knapsack", "x", bad, "local")


def test_talos_command_forwards_nonces():
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "local", nonces=1)
    assert argv[-3:] == ["--nonces", "1", "--yes"]
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "c3", 40, "agentic", nonces=64)
    assert argv[-5:] == ["--mode", "agentic", "--nonces", "64", "--yes"]


def test_talos_command_without_nonces_leaves_talos_default():
    _, argv, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "local")
    assert "--nonces" not in argv
    assert argv == boxlib.talos_command(HOME, "knapsack", "x", 1, "local", nonces=None)[1]


def test_talos_command_refuses_bad_nonces():
    for bad in [0, -1, 65, True]:
        with pytest.raises(ValueError):
            boxlib.talos_command(HOME, "knapsack", "x", 1, "local", nonces=bad)


def test_run_talos_verb_refuses_nonces_alongside_resume(tmp_path):
    """A resumed job keeps the nonce set frozen in its job.json."""
    verb = Path(__file__).resolve().parents[1] / "verbs" / "run_talos"
    args = {"resume": "20260924-230101-knapsack", "backend": "local", "nonces": 1}
    proc = subprocess.run([str(verb)], input=json.dumps(args).encode(), env={"HOME": str(tmp_path)},
                          capture_output=True, check=False)
    assert proc.returncode == 2
    assert "nonces" in json.loads(proc.stdout)["error"]


def test_add_repo_check():
    ok = {"owner": {"login": "FibonAdithya"}, "fork": False, "name": "fleet"}
    assert boxlib.add_repo_check("FibonAdithya", ok, "fleet") is None
    assert "fork" in boxlib.add_repo_check("FibonAdithya", {**ok, "fork": True}, "fleet")
    assert "own" in boxlib.add_repo_check("FibonAdithya", {**ok, "owner": {"login": "someone"}}, "fleet")
    for bad in ["a/b", "", "..", "x" * 101, "has space"]:
        assert boxlib.add_repo_check("FibonAdithya", ok, bad) is not None, bad


def test_task_command_shape(tmp_path):
    wd, argv, secs = boxlib.task_command(HOME, tmp_path, "FibonAdithya/hermes-harness", minutes=30)
    assert argv[0] == str(HOME / ".local" / "bin" / "run-task.sh")
    assert argv[1:] == [str(tmp_path), "FibonAdithya/hermes-harness", "30"]
    assert secs == 30 * 60 + 300
    with pytest.raises(ValueError):
        boxlib.task_command(HOME, tmp_path, "no-slash", 30)
    with pytest.raises(ValueError):
        boxlib.task_command(HOME, tmp_path, "FibonAdithya/hermes-harness", 0)
