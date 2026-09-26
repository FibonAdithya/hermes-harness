"""tasklib: the only code on the box that writes the fleet ledger."""
import pytest

import tasklib
from fakegh import SLUG, FakeGh
from tasklib import TaskError


def names(gh, n):
    return {x["name"] for x in gh.issues[n]["labels"]}


def test_issue_hash_is_sha256_of_title_newline_body():
    import hashlib
    assert tasklib.issue_hash("t", "b") == hashlib.sha256(b"t\nb").hexdigest()
    assert tasklib.issue_hash("t", "b") != tasklib.issue_hash("t", "b ")


def test_file_issue_lands_in_triage_and_never_ready():
    gh = FakeGh()
    out = tasklib.file_issue(gh, SLUG, " Fix the stall check ", "body", "docs", "patch", frozenset({"docs"}), "source:hermes")
    assert out == {"number": 1, "url": f"https://github.com/{SLUG}/issues/1"}
    assert names(gh, 1) == {"fleet:triage", "area:docs", "class:patch", "source:hermes"}
    assert gh.issues[1]["title"] == "Fix the stall check"


def test_file_issue_refuses_an_area_the_repo_does_not_have():
    gh = FakeGh()
    with pytest.raises(TaskError, match="area 'eval' is not in"):
        tasklib.file_issue(gh, SLUG, "t", "b", "eval", "patch", frozenset({"docs"}), "source:hermes")
    assert gh.issues == {}


@pytest.mark.parametrize("cls", ["bug", "", "PATCH"])
def test_file_issue_refuses_an_unknown_class(cls):
    with pytest.raises(TaskError, match="class"):
        tasklib.file_issue(FakeGh(), SLUG, "t", "b", "docs", cls, frozenset({"docs"}), "source:hermes")


@pytest.mark.parametrize("title,body", [("", "b"), ("   ", "b"), ("x" * 201, "b"), ("t", "x" * 20001),
                                        ("t", "x" * 3501)],
                         ids=["empty", "blank", "long-title", "body-20001", "body-3501"])
def test_file_issue_refuses_empty_or_oversized_text(title, body):
    with pytest.raises(TaskError):
        tasklib.file_issue(FakeGh(), SLUG, title, body, "docs", "patch", frozenset({"docs"}), "source:hermes")


def test_file_issue_refuses_a_source_that_is_not_a_producer():
    """Catches passing the caller's label through: Hermes must not be able to file as the doctor."""
    with pytest.raises(TaskError, match="source"):
        tasklib.file_issue(FakeGh(), SLUG, "t", "b", "docs", "patch", frozenset({"docs"}), "fleet:ready")


def _filed(gh):
    tasklib.file_issue(gh, SLUG, "t", "b", "docs", "patch", frozenset({"docs"}), "source:hermes")
    return tasklib.issue_hash("t", "b")


def test_approve_for_fleet_adds_ready_and_auto_ok_then_removes_triage():
    gh = FakeGh()
    h = _filed(gh)
    out = tasklib.approve(gh, SLUG, 1, h, "fleet")
    assert out == {"number": 1, "title": "t", "body": "b"}
    assert names(gh, 1) == {"fleet:ready", "fleet:auto-ok", "area:docs", "class:patch", "source:hermes"}
    edits = [c[0] for c in gh.calls if c[0][:2] == ("issue", "edit")]
    assert "--add-label" in edits[0] and "--remove-label" in edits[1]  # add before remove


def test_approve_for_solo_moves_to_human_and_marks_solo():
    gh = FakeGh()
    h = _filed(gh)
    tasklib.approve(gh, SLUG, 1, h, "solo")
    assert names(gh, 1) == {"fleet:human", "fleet:solo", "area:docs", "class:patch", "source:hermes"}


def test_approve_refuses_when_the_text_changed_since_announcement():
    gh = FakeGh()
    h = _filed(gh)
    gh.issues[1]["body"] = "b, plus something the owner never read"
    with pytest.raises(TaskError, match="changed since it was announced"):
        tasklib.approve(gh, SLUG, 1, h, "fleet")
    assert "fleet:ready" not in names(gh, 1)


def test_approve_refuses_a_closed_or_already_moved_issue():
    gh = FakeGh()
    h = _filed(gh)
    gh.issues[1]["state"] = "CLOSED"
    with pytest.raises(TaskError, match="closed"):
        tasklib.approve(gh, SLUG, 1, h, "fleet")
    gh.issues[1]["state"] = "OPEN"
    gh.issues[1]["labels"] = [{"name": "fleet:ready"}]
    with pytest.raises(TaskError, match="no longer in triage"):
        tasklib.approve(gh, SLUG, 1, h, "fleet")


def test_approve_refuses_an_unknown_mode():
    gh = FakeGh()
    h = _filed(gh)
    with pytest.raises(TaskError, match="mode"):
        tasklib.approve(gh, SLUG, 1, h, "ready")


def test_close_marks_dropped_and_closes():
    gh = FakeGh()
    _filed(gh)
    tasklib.close(gh, SLUG, 1)
    assert gh.issues[1]["state"] == "CLOSED" and "fleet:dropped" in names(gh, 1)


def test_triage_lists_open_triage_issues_with_their_hash():
    gh = FakeGh()
    _filed(gh)
    rows = tasklib.triage(gh, SLUG)
    assert rows == [{"number": 1, "title": "t", "body": "b",
                     "labels": ["fleet:triage", "area:docs", "class:patch", "source:hermes"],
                     "hash": tasklib.issue_hash("t", "b")}]


def test_repo_dir_needs_a_fleet_toml(tmp_path):
    (tmp_path / "TIG" / "plain").mkdir(parents=True)
    with pytest.raises(TaskError, match="no fleet.toml"):
        tasklib.repo_dir(tmp_path, "plain")
    with pytest.raises(ValueError):
        tasklib.repo_dir(tmp_path, "../etc")


def test_slug_reads_fleet_toml(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "fleet.toml").write_text('[repo]\nslug = "FibonAdithya/fleet"\n')
    assert tasklib.slug(d) == "FibonAdithya/fleet"
    (d / "fleet.toml").write_text('[repo]\nslug = "not a slug"\n')
    with pytest.raises(TaskError, match="slug"):
        tasklib.slug(d)


def test_fleet_repos_are_discovered_not_listed(tmp_path):
    for name, has in (("a", True), ("b", False), ("c", True)):
        (tmp_path / "TIG" / name).mkdir(parents=True)
        if has:
            (tmp_path / "TIG" / name / "fleet.toml").write_text("")
    assert [p.name for p in tasklib.fleet_repos(tmp_path)] == ["a", "c"]


def test_file_issue_accepts_a_body_at_the_announcement_limit():
    """3500 is what the owner's announcement shows in full (broker BODY_CHARS)."""
    assert tasklib.MAX_BODY == 3500
    out = tasklib.file_issue(FakeGh(), SLUG, "t", "x" * 3500, "docs", "patch", frozenset({"docs"}), "source:hermes")
    assert out["number"] == 1


def test_fleet_areas_runs_fleet_without_syncing_and_without_the_target_on_sys_path(tmp_path, monkeypatch):
    """file_task is Hermes-reachable: it must not `uv sync` the deployed fleet checkout,
    and the target repo (the cwd) must not shadow fleet's imports."""
    import json
    import os
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "uv.json"
    (bin_ / "uv").write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                             f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}, open({str(log)!r}, 'w'))\n"
                             "print('[\"daemon\", \"docs\"]')\n")
    (bin_ / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    target = tmp_path / "home" / "TIG" / "fleet-fixture"
    target.mkdir(parents=True)
    assert tasklib.fleet_areas(target, tmp_path / "home") == frozenset({"daemon", "docs"})
    rec = json.loads(log.read_text())
    assert rec["argv"][:5] == ["run", "--no-sync", "--project", str(tmp_path / "home" / "TIG" / "fleet"), "python"]
    assert rec["argv"][5:7] == ["-P", "-c"] and "area_names" in rec["argv"][7]
    assert rec["cwd"] == str(target)
