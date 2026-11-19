import base64
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from hermes_wiki import todocmds
from hermes_wiki.config import WikiConfig
from hermes_wiki.todopage import render
from hermes_wiki.todorecords import Record, dumps, loads
from hermes_wiki.todos import TodoError, TodoRejected
from hermes_wiki.todotree import DryTree

TODAY = date(2026, 11, 19)
FIXTURE = Path(__file__).parent / "fixtures" / "todos.md"


def cfg_for(tmp_path, **kw):
    return WikiConfig(root=tmp_path, source="claude-box", state_dir=tmp_path / "state", **kw)


def rec(id, status="open", **kw):
    return Record(id=id, title=f"Item {id}", details=[], project="hermes", created="2026-11-10",
                  source="claude-box", status=status, **kw)


def seed(tmp_path, records):
    d = tmp_path / "dry"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-todos.jsonl").write_text(dumps(records), encoding="utf-8")
    (d / "agent-todos.md").write_text(render(records, TODAY), encoding="utf-8")
    return DryTree(d), d / "agent-todos.jsonl"


def kw(ids=("abc123",)):
    it = iter(ids)
    return dict(today=TODAY, new_id=lambda: next(it), sleep=lambda s: None)


class Run:
    """Fake subprocess.run: `git remote get-url` answers `origin`; other calls are logged."""

    def __init__(self, origin="git@github.com:FibonAdithya/chain.git", rc=0, gh=None):
        self.origin, self.rc, self.gh, self.calls = origin, rc, gh, []

    def __call__(self, argv, **k):
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, self.rc, self.origin, "")
        if self.gh:
            return self.gh(argv, **k)
        raise AssertionError(f"unexpected call {argv}")


def add(cfg, tree, run, title="T", details=(), project=None, checks=(), ids=("abc123",)):
    return todocmds.cmd_add(cfg, tree, title, list(details), project, list(checks), Path("/x"),
                            run=run, **kw(ids))


def test_add_defaults_project_from_origin(tmp_path):
    tree, jl = seed(tmp_path, [])
    i = add(cfg_for(tmp_path), tree, Run())
    assert i == "abc123"
    assert loads(jl.read_text())[0].project == "chain"  # catches ignoring cwd/origin


def test_add_origin_failure_is_unsorted(tmp_path):
    tree, jl = seed(tmp_path, [])
    add(cfg_for(tmp_path), tree, Run(rc=128))
    assert loads(jl.read_text())[0].project == "unsorted"  # catches crashing without a remote


def test_add_project_flag_wins(tmp_path):
    tree, jl = seed(tmp_path, [])
    add(cfg_for(tmp_path), tree, Run(), project="mine")
    assert loads(jl.read_text())[0].project == "mine"  # catches overriding an explicit flag


def test_add_records_checks_source_created(tmp_path):
    tree, jl = seed(tmp_path, [])
    add(cfg_for(tmp_path), tree, Run(), checks=["pr:a/b#1"])
    r = loads(jl.read_text())[0]
    assert r.done_when == [{"pr": "a/b#1", "state": "merged"}]
    assert (r.source, r.created) == ("claude-box", "2026-11-19")  # catches dropped checks/metadata


def test_add_invalid_project_rejected_without_write(tmp_path):
    tree, jl = seed(tmp_path, [])
    before = jl.read_text()
    with pytest.raises(TodoRejected):
        add(cfg_for(tmp_path), tree, Run(), project="Bad Name")
    assert jl.read_text() == before  # catches writing before validating


def test_add_commit_message_and_id_unique(tmp_path):
    tree, jl = seed(tmp_path, [rec("abc123")])
    i = add(cfg_for(tmp_path), tree, Run(), ids=("abc123", "def456"))
    assert i == "def456"  # catches id collision with an existing record


def test_done_sets_closed_by_agent(tmp_path):
    tree, jl = seed(tmp_path, [rec("aaaaaa")])
    assert todocmds.cmd_done(cfg_for(tmp_path), tree, "aaaaaa", "shipped", run=Run(), **kw())
    r = loads(jl.read_text())[0]
    assert r.status == "done"
    assert r.closed == {"at": "2026-11-19", "by": "agent", "reason": "shipped"}  # catches wrong closer


def test_done_default_reason_and_already_done(tmp_path):
    tree, jl = seed(tmp_path, [rec("aaaaaa")])
    c = cfg_for(tmp_path)
    todocmds.cmd_done(c, tree, "aaaaaa", None, run=Run(), **kw())
    assert loads(jl.read_text())[0].closed["reason"] == "done"
    assert todocmds.cmd_done(c, tree, "aaaaaa", "x", run=Run(), **kw()) is False  # catches re-closing


def test_unknown_id_errors(tmp_path):
    tree, _ = seed(tmp_path, [rec("aaaaaa")])
    c = cfg_for(tmp_path)
    for fn in (lambda: todocmds.cmd_done(c, tree, "ffffff", None, run=Run(), **kw()),
               lambda: todocmds.cmd_rm(c, tree, "ffffff", **kw()),
               lambda: todocmds.cmd_reopen(c, tree, "ffffff", **kw()),
               lambda: todocmds.cmd_edit(c, tree, "ffffff", "x", [], None, [], **kw())):
        with pytest.raises(TodoError, match="no item with id ffffff") as e:
            fn()
        assert e.value.code == 1  # catches silent success on unknown ids


def test_edit_appends_and_revalidates(tmp_path):
    tree, jl = seed(tmp_path, [rec("aaaaaa", done_when=[{"pr": "a/b#1", "state": "merged"}])])
    c = cfg_for(tmp_path)
    todocmds.cmd_edit(c, tree, "aaaaaa", "New", ["d1"], "proj", ["pr:a/b#1", "issue:a/b#2"], **kw())
    r = loads(jl.read_text())[0]
    assert (r.title, r.details, r.project) == ("New", ["d1"], "proj")
    assert [d.get("pr") or d.get("issue") for d in r.done_when] == ["a/b#1", "a/b#2"]  # catches duplicate/lost checks
    before = jl.read_text()
    with pytest.raises(TodoRejected):
        todocmds.cmd_edit(c, tree, "aaaaaa", "x" * 121, [], None, [], **kw())
    assert jl.read_text() == before  # catches unvalidated edits


def test_reopen_suggested_marks_rejected(tmp_path):
    s = rec("aaaaaa", "suggested", suggestion={"at": "2026-11-18", "evidence": "e"})
    tree, jl = seed(tmp_path, [s])
    todocmds.cmd_reopen(cfg_for(tmp_path), tree, "aaaaaa", **kw())
    r = loads(jl.read_text())[0]
    assert r.status == "open" and r.suggestion["rejected"] is True  # catches losing the rejection
    assert r.suggestion["evidence"] == "e"


def test_reopen_done_clears_closed(tmp_path):
    tree, jl = seed(tmp_path, [rec("aaaaaa", "done", closed={"at": "2026-11-18", "by": "owner", "reason": "r"})])
    todocmds.cmd_reopen(cfg_for(tmp_path), tree, "aaaaaa", **kw())
    r = loads(jl.read_text())[0]
    assert (r.status, r.closed) == ("open", None)  # catches keeping stale closed


def test_reopen_open_item_exits_1(tmp_path):
    tree, _ = seed(tmp_path, [rec("aaaaaa")])
    with pytest.raises(TodoError, match="already open") as e:
        todocmds.cmd_reopen(cfg_for(tmp_path), tree, "aaaaaa", **kw())
    assert e.value.code == 1  # catches a silent no-op


def test_rm_removes_record_and_line(tmp_path):
    tree, jl = seed(tmp_path, [rec("aaaaaa"), rec("bbbbbb")])
    todocmds.cmd_rm(cfg_for(tmp_path), tree, "aaaaaa", **kw())
    assert [r.id for r in loads(jl.read_text())] == ["bbbbbb"]
    page = (tmp_path / "dry" / "agent-todos.md").read_text()
    assert "aaaaaa" not in page and "bbbbbb" in page  # catches tick-instead-of-delete


def test_list_filters_and_json_refusal(tmp_path):
    tree, _ = seed(tmp_path, [rec("aaaaaa"), rec("bbbbbb", "done", closed={"at": "2026-11-18", "by": "agent", "reason": "r"}),
                              Record(id="cccccc", title="C", details=[], project="chain", created="2026-11-10", source="s")])
    c = cfg_for(tmp_path)
    ids = lambda **k: [r.id for r in todocmds.cmd_list(c, tree, k.get("all", False), False, k.get("project"), run=Run())]
    assert ids() == ["aaaaaa", "cccccc"]
    assert ids(all=True) == ["aaaaaa", "bbbbbb", "cccccc"]
    assert ids(project="chain") == ["cccccc"]  # catches ignoring --all / --project


def test_list_json_refused_on_public_only_without_gh_call(tmp_path):
    tree, _ = seed(tmp_path, [rec("aaaaaa")])
    run = Run()
    for as_json in (True, False):
        with pytest.raises(TodoError) as e:
            todocmds.cmd_list(cfg_for(tmp_path, public_only=True), tree, False, as_json, None, run=run)
        assert e.value.code == 2
    assert run.calls == []  # catches leaking private items to the box


def test_box_add_prints_only_id(tmp_path, capsys):
    from hermes_wiki import cli

    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"root": str(tmp_path), "source": "box", "public_only": True,
                                "state_dir": str(tmp_path / "state")}))
    seed(tmp_path / "state", [rec("aaaaaa")])
    import os
    os.environ["HERMES_WIKI_DRY_PUBLISH"] = "1"
    try:
        assert cli.main(["todo", "add", "Secret-ish title", "--project", "x", "--config", str(conf)]) == 0
    finally:
        del os.environ["HERMES_WIKI_DRY_PUBLISH"]
    cap = capsys.readouterr()
    import re
    assert re.fullmatch(r"[0-9a-f]{6}\n", cap.out)
    assert cap.err == ""  # catches printing content or logs on the box


def _gh(puts, todos_md=None):
    todos_md = todos_md or FIXTURE.read_text(encoding="utf-8")

    def gh(argv, **k):
        if "PUT" in argv:
            puts.append(argv)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        body = {"content": base64.b64encode(todos_md.encode()).decode(), "sha": "s1"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
    return gh


class Unmigrated:
    def head(self, paths=None):
        return "h", {"agent-todos.jsonl": None, "agent-todos.md": None, "todos.md": None}

    def commit(self, *a):
        raise AssertionError("must not commit")


def test_unmigrated_add_uses_old_path(tmp_path, capsys):
    puts = []
    i = add(cfg_for(tmp_path), Unmigrated(), Run(gh=_gh(puts)), project="p", checks=["pr:a/b#1"])
    assert i == "abc123"
    assert len(puts) == 1 and "repos/FibonAdithya/hermes-wiki/contents/todos.md" in " ".join(puts[0])
    assert "ignored until hermes-wiki todo migrate" in capsys.readouterr().err  # catches breaking pre-migration clients


def test_unmigrated_edit_exits_1(tmp_path):
    c = cfg_for(tmp_path)
    run = Run()
    for fn in (lambda: todocmds.cmd_edit(c, Unmigrated(), "aaaaaa", "x", [], None, [], **kw()),
               lambda: todocmds.cmd_rm(c, Unmigrated(), "aaaaaa", **kw()),
               lambda: todocmds.cmd_reopen(c, Unmigrated(), "aaaaaa", **kw()),
               lambda: todocmds.cmd_list(c, Unmigrated(), False, True, None, run=run)):
        with pytest.raises(TodoError, match="run hermes-wiki todo migrate first") as e:
            fn()
        assert e.value.code == 1
    assert run.calls == []  # catches writing to todos.md for new commands


def test_unmigrated_list_uses_old_path(tmp_path):
    todos = FIXTURE.read_text(encoding="utf-8").replace(
        "# Todos\n\n", "# Todos\n\n## Agent follow-ups\n- [ ] One (2026-11-12, claude-box, id 111111)\n\n", 1)
    out = todocmds.cmd_list(cfg_for(tmp_path), Unmigrated(), False, False, None, run=Run(gh=_gh([], todos)))
    assert any(r.title == "One" and r.id == "111111" and r.status == "open" for r in out)  # catches list failing before migration
