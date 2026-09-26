"""The doctor turns a broken fleet night into one triage issue on FibonAdithya/fleet."""
import json
from pathlib import Path

import pytest

import doctor
from fakegh import FakeGh

OPS = Path(__file__).parent / "fixtures" / "ops"
NIGHT = "fleet-20260926-2300-ab12"


def make_night(home, status="failed", log="", runlog=None, repo="fleet-fixture", night=NIGHT):
    d = home / "nights" / night
    d.mkdir(parents=True)
    if status is not None:
        (d / "status").write_text(status + "\n")
    (d / "meta.json").write_text(json.dumps({"kind": "fleet", "repo": repo, "started_at": 1.0}))
    if log is not None:
        (d / "log").write_text(log)
    r = home / "TIG" / repo
    r.mkdir(parents=True, exist_ok=True)
    (r / "fleet.toml").write_text(f'[repo]\nslug = "FibonAdithya/{repo}"\n')
    if runlog is not None:
        (r / ".fleet").mkdir(exist_ok=True)
        (r / ".fleet" / "run.jsonl").write_text("".join(json.dumps(e) + "\n" for e in runlog))
    return d


def fleet_repo(home, sha="1" * 40):
    r = home / "TIG" / "fleet"
    r.mkdir(parents=True, exist_ok=True)
    (r / "fleet.toml").write_text('[repo]\nslug = "FibonAdithya/fleet"\n')
    stamp = home / ".local" / "share" / "fleet-deployed.sha"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(sha)


def gh_for_fleet():
    gh = FakeGh()
    gh.labels |= {"area:daemon", "area:cost", "area:executor", "class:investigation"}
    return gh


def entry(run_id, kind, **fields):
    return {"run_id": run_id, "intent": {"kind": kind, **fields}, "outcome": "applied", "cost_usd": 0.0}


CRASH = ('fleet run: run_id=r1 (new run) spent=$0.00 socket=/x\nTraceback (most recent call last):\n'
         '  File "/home/adi/TIG/fleet/src/fleet/daemon.py", line 168, in tick\n'
         'fleet.ledger.LedgerError: unknown area: \'allocator\'\n')


def test_a_failed_night_that_ticked_is_s1(tmp_path):
    d = make_night(tmp_path, log=CRASH, runlog=[entry("r1", "Dispatch", task=1, role="implementer", base="x")])
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.suggested, f.area, f.ops) == ("S1", "solo", "daemon", False)
    assert "LedgerError" in f.key and "daemon.py" in f.key and "168" not in f.key


def test_a_night_that_never_ticked_is_s2(tmp_path):
    d = make_night(tmp_path, log=CRASH, runlog=[])
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.suggested) == ("S2", "solo")


def test_a_night_with_no_log_and_no_runlog_is_s2_not_a_crash(tmp_path):
    d = make_night(tmp_path, status=None, log=None, runlog=None)
    [f] = doctor.examine({"id": NIGHT, "status": "unknown"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert f.kind == "S2" and "no-log" in f.key


def test_an_idle_night_killed_at_its_hour_limit_is_healthy(tmp_path):
    """`fleet run` loops until RuntimeMaxSec's SIGTERM, so every fleet night ends
    `killed`, and an idle backlog writes no run-log entries (the run log records
    intents, not ticks). Catches treating either as a fault: the doctor would file
    on every normal night."""
    d = make_night(tmp_path, status="killed", log="fleet run: run_id=r1 (new run) spent=$0.00 socket=/x\n", runlog=[])
    assert doctor.examine({"id": NIGHT, "status": "killed"}, d, tmp_path / "TIG" / "fleet-fixture") == []


def test_a_night_that_died_before_starting_is_s2(tmp_path):
    """A broken fleet.toml fails in load_config, before `fleet run:` is printed.
    Catches keying S2 only on an empty run log: with no run id there is no run log to read."""
    log = ('Traceback (most recent call last):\n'
           '  File "/home/adi/TIG/fleet/src/fleet/config.py", line 160, in load_config\n'
           'fleet.config.ConfigError: slots must be an integer\n')
    d = make_night(tmp_path, status="failed", log=log, runlog=None)
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.key) == ("S2", "no-start|ConfigError|config.py:load_config")


def test_a_drain_is_s3_for_the_owner(tmp_path):
    log = [entry("r1", "Drain", reason="UnpricedModel: claude-opus-9 has no [prices] row")]
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n", runlog=log)
    [f] = doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.suggested, f.area) == ("S3", "task", "cost")
    assert "invariant 5" in "\n".join(f.evidence)


def test_three_releases_for_one_reason_is_s4_two_is_not(tmp_path):
    rel = [entry("r1", "Release", task=n, reason="no_agent", attempt=1) for n in (1, 2, 3)]
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n", runlog=rel)
    [f] = doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.key, f.area) == ("S4", "Release:no_agent", "executor")
    d2 = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n",
                    runlog=rel[:2], night="fleet-20260927-2300-cd34", repo="other")
    assert doctor.examine({"id": "fleet-20260927-2300-cd34", "status": "done"}, d2, tmp_path / "TIG" / "other") == []


def test_other_runs_in_the_same_runlog_are_ignored(tmp_path):
    rel = [entry("old", "Release", task=n, reason="stalled", attempt=1) for n in (1, 2, 3)]
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n",
                   runlog=rel + [entry("r1", "Dispatch", task=1, role="implementer", base="x")])
    assert doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture") == []


def test_a_clean_night_has_no_findings(tmp_path):
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n",
                   runlog=[entry("r1", "Dispatch", task=1, role="implementer", base="x")])
    assert doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture") == []


def test_every_ops_pattern_has_a_captured_message_and_every_capture_a_pattern():
    """Catches a pattern written from memory (no capture), and an empty fixtures
    directory, which would make the parametrized test below collect nothing and pass."""
    assert sorted(p.stem for p in OPS.glob("*.txt")) == sorted(name for name, _ in doctor.OPS_PATTERNS)
    assert doctor.OPS_PATTERNS


@pytest.mark.parametrize("path", sorted(OPS.glob("*.txt")), ids=lambda p: p.stem)
def test_captured_ops_messages_are_ops(tmp_path, path):
    d = make_night(tmp_path, log="fleet run: run_id=r1 (new run)\n" + path.read_text())
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert f.ops and f.kind == "ops" and f.key == path.stem


def test_signature_depends_on_the_deployed_sha():
    assert doctor.signature("S1", "k", "1" * 40) != doctor.signature("S1", "k", "2" * 40)
    assert len(doctor.signature("S1", "k", "1" * 40)) == 8


def test_run_files_once_marks_the_night_and_then_comments_on_a_repeat(tmp_path):
    fleet_repo(tmp_path)
    make_night(tmp_path, log=CRASH, runlog=[])
    gh = gh_for_fleet()
    out = doctor.run(tmp_path, gh)
    assert out["filed"] == [1] and len(gh.issues) == 1
    issue = gh.issues[1]
    names = {x["name"] for x in issue["labels"]}
    assert {"fleet:triage", "source:doctor", "class:investigation", "area:daemon"} <= names
    assert any(n.startswith("doctor:") for n in names)
    assert (tmp_path / "nights" / NIGHT / "doctored").exists()
    assert json.loads((tmp_path / "nights" / NIGHT / "doctor.json").read_text())["filed"] == [1]
    assert doctor.run(tmp_path, gh)["filed"] == []           # marker honoured
    make_night(tmp_path, log=CRASH, runlog=[], night="fleet-20260927-2300-cd34")
    out = doctor.run(tmp_path, gh)
    assert out["filed"] == [] and out["commented"] == [1] and len(gh.issues[1]["comments"]) == 1


def test_a_new_deployed_sha_files_a_new_issue(tmp_path):
    fleet_repo(tmp_path, sha="1" * 40)
    make_night(tmp_path, log=CRASH, runlog=[])
    gh = gh_for_fleet()
    doctor.run(tmp_path, gh)
    fleet_repo(tmp_path, sha="2" * 40)
    make_night(tmp_path, log=CRASH, runlog=[], night="fleet-20260927-2300-cd34")
    assert doctor.run(tmp_path, gh)["filed"] == [2]


def test_at_most_three_new_issues_per_run(tmp_path):
    fleet_repo(tmp_path)
    for n in range(5):
        make_night(tmp_path, log=CRASH.replace("LedgerError", f"Error{n}"), runlog=[],
                   night=f"fleet-2026092{n}-2300-ab1{n}")
    gh = gh_for_fleet()
    out = doctor.run(tmp_path, gh)
    assert len(out["filed"]) == 3 and out["skipped"] == 2
    assert all((tmp_path / "nights" / f"fleet-2026092{n}-2300-ab1{n}" / "doctored").exists() for n in range(5))


def test_running_and_non_fleet_nights_are_left_alone(tmp_path, monkeypatch):
    fleet_repo(tmp_path)
    make_night(tmp_path, status="running", log=CRASH, runlog=[])
    make_night(tmp_path, status="failed", log=CRASH, runlog=[], night="talos-20260926-2300-ab12")
    gh = gh_for_fleet()
    import boxlib
    monkeypatch.setattr(boxlib, "unit_active", lambda night_id: True)  # the running night's unit is alive
    assert doctor.run(tmp_path, gh)["filed"] == []
    assert not (tmp_path / "nights" / NIGHT / "doctored").exists()


def test_status_rows_carry_the_doctor_summary(tmp_path):
    import boxlib
    d = make_night(tmp_path, status="done", log="")
    (d / "doctor.json").write_text(json.dumps({"filed": [4], "commented": [], "skipped": 1}))
    [row] = boxlib.read_status(tmp_path / "nights")
    assert row["doctor"] == {"filed": [4], "commented": [], "skipped": 1}
