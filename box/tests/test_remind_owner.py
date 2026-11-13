"""droplet/remind-owner.sh with a stub `hermes` first on PATH (never the real one)."""
import json
import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "droplet" / "remind-owner.sh"

STUB = """#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$STUB_LOG"
exit "${FAKE_RC:-0}"
"""

# Shell-special text must reach the owner byte for byte.
MESSAGE = 'Check run 42: `nvidia-smi`, "$HOME" and C:\\new\\t stay literal\nsecond line'


def _run(tmp_path, args=("2h", "sweep"), message=MESSAGE, rc=0, deliver=None,
         jobs=None, files=(), extra=()):
    bindir = tmp_path / "stubs"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "hermes"
    stub.write_text(STUB)
    stub.chmod(0o755)
    home = tmp_path / "home"
    scripts = home / ".hermes" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "hermes.log"
    if jobs is not None:
        cron = home / ".hermes" / "cron"
        cron.mkdir(parents=True, exist_ok=True)
        (cron / "jobs.json").write_text(jobs if isinstance(jobs, str) else json.dumps(jobs))
    now = time.time()
    for base, age in files:
        for ext in (".sh", ".txt"):
            f = scripts / (base + ext)
            f.write_text("x\n")
            os.utime(f, (now - age, now - age))
    for name, age in extra:
        f = scripts / name
        f.write_text("x\n")
        os.utime(f, (now - age, now - age))
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


R1 = "remind-20261113094637-5830"
R2 = "remind-20261113103942-30175"
R3 = "remind-20261113111111-77"


def _jobs(*pairs):
    return {"jobs": [{"id": i, "script": s} for i, s in pairs]}


def _names(scripts):
    return sorted(p.name for p in scripts.iterdir())


def test_cancel_removes_job_and_only_its_files(tmp_path):
    jobs = _jobs(("j1", R1 + ".sh"), ("j2", R2 + ".sh"))
    r, calls, scripts = _run(tmp_path, args=("--cancel", "j1"), jobs=jobs,
                             files=[(R1, 5), (R2, 5)])
    assert r.returncode == 0, r.stderr
    assert calls == [["cron", "remove", "j1"]]
    assert _names(scripts) == [R2 + ".sh", R2 + ".txt"]


def test_cancel_refuses_non_reminder_job(tmp_path):
    jobs = _jobs(("j1", "wiki-worklist.py"))
    r, calls, scripts = _run(tmp_path, args=("--cancel", "j1"), jobs=jobs,
                             files=[(R1, 5)], extra=[("wiki-worklist.py", 5)])
    assert r.returncode == 1
    assert "not a reminder job" in r.stderr
    assert calls == []
    assert _names(scripts) == sorted([R1 + ".sh", R1 + ".txt", "wiki-worklist.py"])


def test_cancel_refuses_null_script_and_unknown_id(tmp_path):
    jobs = _jobs(("j1", None))
    r, calls, _ = _run(tmp_path, args=("--cancel", "j1"), jobs=jobs)
    assert r.returncode == 1
    r, calls2, _ = _run(tmp_path, args=("--cancel", "nope"), jobs=jobs)
    assert r.returncode == 1
    assert calls == [] and calls2 == []


def test_cancel_refuses_near_miss_script_names(tmp_path):  # catches fullmatch -> search/match
    for n, name in enumerate(["remind-notes.sh", "xremind-20261113094637-5830.sh"]):
        d = tmp_path / str(n)
        d.mkdir()
        r, calls, scripts = _run(d, args=("--cancel", "j1"), jobs=_jobs(("j1", name)),
                                 extra=[(name, 5)])
        assert r.returncode == 1, name
        assert calls == []
        assert name in _names(scripts)


def test_cancel_keeps_files_when_remove_fails(tmp_path):
    jobs = _jobs(("j1", R1 + ".sh"))
    r, calls, scripts = _run(tmp_path, args=("--cancel", "j1"), jobs=jobs,
                             files=[(R1, 5)], rc=3)
    assert r.returncode != 0
    assert calls == [["cron", "remove", "j1"]]
    assert _names(scripts) == [R1 + ".sh", R1 + ".txt"]


def test_prune_deletes_only_orphans(tmp_path):
    jobs = _jobs(("j1", R1 + ".sh"))
    r, calls, scripts = _run(tmp_path, args=("--prune",), jobs=jobs,
                             files=[(R1, 660), (R2, 660)])
    assert r.returncode == 0, r.stderr
    assert calls == []
    assert _names(scripts) == [R1 + ".sh", R1 + ".txt"]
    assert str(scripts / (R2 + ".sh")) in r.stdout
    assert str(scripts / (R2 + ".txt")) in r.stdout
    assert R1 not in r.stdout


def test_prune_malformed_jobs_deletes_nothing(tmp_path):
    r, calls, scripts = _run(tmp_path, args=("--prune",), jobs="{not json",
                             files=[(R2, 660)])
    assert r.returncode == 1
    assert _names(scripts) == [R2 + ".sh", R2 + ".txt"]
    (tmp_path / "b").mkdir()
    r, calls, scripts = _run(tmp_path / "b", args=("--prune",), files=[(R2, 660)])
    assert r.returncode == 1
    assert _names(scripts) == [R2 + ".sh", R2 + ".txt"]


def test_prune_age_guard(tmp_path):
    r, _, scripts = _run(tmp_path, args=("--prune",), jobs={"jobs": []},
                         files=[(R2, 300), (R3, 660)])
    assert r.returncode == 0, r.stderr
    assert _names(scripts) == [R2 + ".sh", R2 + ".txt"]


def test_prune_tolerates_null_scripts(tmp_path):
    jobs = _jobs(("a", None), ("b", "loop0-reminder.sh"))
    r, _, scripts = _run(tmp_path, args=("--prune",), jobs=jobs, files=[(R2, 660)],
                         extra=[("loop0-reminder.sh", 660), ("remind-notes.sh", 660)])
    assert r.returncode == 0, r.stderr
    assert _names(scripts) == ["loop0-reminder.sh", "remind-notes.sh"]


def test_cancel_without_id_is_usage_error(tmp_path):
    r, calls, _ = _run(tmp_path, args=("--cancel",), jobs={"jobs": []})
    assert r.returncode == 2
    assert calls == []
