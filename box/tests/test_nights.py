import json
import subprocess
from pathlib import Path

import pytest

import boxlib

VERBS = Path(__file__).resolve().parents[1] / "verbs"


def test_night_id_shape_and_validation():
    import calendar

    nid = boxlib.new_night_id("fleet", now=float(calendar.timegm((2026, 9, 24, 8, 0, 0))))
    assert boxlib.NIGHT_ID_RE.match(nid), nid
    assert nid.startswith("fleet-20260924-")
    assert boxlib.check_night_id(nid) == nid
    for bad in ["", "../x", "fleet-2026-1", "FLEET-20260924-0800-abcd", "a/b", nid + "\n"]:
        with pytest.raises(ValueError):
            boxlib.check_night_id(bad)


def test_two_ids_differ():
    assert boxlib.new_night_id("talos", now=1.0) != boxlib.new_night_id("talos", now=1.0)


def test_status_reads_every_night(tmp_path):
    root = tmp_path / "nights"
    a = boxlib.night_dir(root, "fleet-20260924-2300-aaaa")
    b = boxlib.night_dir(root, "talos-20260924-2301-bbbb")
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    boxlib.write_status(a, "running")
    boxlib.write_status(b, "failed", exit_code=3)
    (root / "not-a-night").mkdir()
    rows = boxlib.read_status(root)
    assert {r["id"] for r in rows} == {"fleet-20260924-2300-aaaa", "talos-20260924-2301-bbbb"}
    by = {r["id"]: r for r in rows}
    assert by["talos-20260924-2301-bbbb"]["status"] == "failed"
    assert by["talos-20260924-2301-bbbb"]["exit_code"] == 3
    assert by["fleet-20260924-2300-aaaa"]["exit_code"] is None


def test_systemd_run_argv_carries_the_limit_and_env():
    argv = boxlib.systemd_run_argv("night-x", 3600, Path("/tmp/w"), {"PATH": "/p"}, ["/bin/true"])
    assert argv[:3] == ["systemd-run", "--user", "--unit=night-x"]
    assert "--property=RuntimeMaxSec=3600" in argv
    assert "--setenv=PATH=/p" in argv
    assert argv[-1] == "/bin/true"


def _verb(name, payload, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run([str(VERBS / name)], input=json.dumps(payload).encode(), capture_output=True, env=env)


def test_status_verb_lists_nights(tmp_path):
    d = boxlib.night_dir(tmp_path / "nights", "task-20260924-0100-cafe")
    d.mkdir(parents=True)
    boxlib.write_status(d, "done", exit_code=0)
    p = _verb("status", {}, tmp_path)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["nights"][0]["id"] == "task-20260924-0100-cafe"
    assert out["nights"][0]["status"] == "done"


def test_log_verb_tails_and_refuses_bad_ids(tmp_path):
    d = boxlib.night_dir(tmp_path / "nights", "task-20260924-0100-cafe")
    d.mkdir(parents=True)
    (d / "log").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    p = _verb("log", {"id": "task-20260924-0100-cafe", "lines": 3}, tmp_path)
    assert json.loads(p.stdout)["lines"] == ["line 97", "line 98", "line 99"]
    for bad in ["../../etc/passwd", "", "task-20260924-0100-cafe/../x"]:
        p = _verb("log", {"id": bad, "lines": 3}, tmp_path)
        assert p.returncode == 2, bad
        assert "error" in json.loads(p.stdout)


def test_log_verb_for_missing_night(tmp_path):
    p = _verb("log", {"id": "task-20260924-0100-dead", "lines": 3}, tmp_path)
    assert p.returncode == 2
    assert "no such night" in json.loads(p.stdout)["error"]


def test_list_repos_verb(tmp_path):
    (tmp_path / "TIG" / "fleet").mkdir(parents=True)
    (tmp_path / "TIG" / "a-file").write_text("x")
    p = _verb("list_repos", {}, tmp_path)
    assert json.loads(p.stdout)["repos"] == ["fleet"]
