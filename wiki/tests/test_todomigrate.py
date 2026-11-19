import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from hermes_wiki import cli, todomigrate
from hermes_wiki.config import WikiConfig
from hermes_wiki.todorecords import loads
from hermes_wiki.todos import TodoError, take_section_items
from hermes_wiki.todotree import DryTree

FIX = Path(__file__).parent / "fixtures" / "todos-migrate.md"
TODAY = date(2026, 11, 19)
HISTORY = [
    {"m": "todo: done b2c3d4", "d": "2026-11-17T10:00:00Z"},
    {"m": "todo: done b2c3d4", "d": "2026-11-16T09:00:00Z"},
    {"m": "todo: add b2c3d4\n\nbody", "d": "2026-11-09T09:00:00Z"},
]


class FakeRun:
    def __init__(self, history=HISTORY, rc=0, stderr=""):
        self.history, self.rc, self.stderr, self.calls = history, rc, stderr, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = "".join(json.dumps(h) + "\n" for h in self.history)
        return subprocess.CompletedProcess(argv, self.rc, stdout=out, stderr=self.stderr)


def gen(*ids):
    it = iter(ids)
    return lambda: next(it)


@pytest.fixture
def cfg(tmp_path):
    return WikiConfig(root=tmp_path, source="claude-box", state_dir=tmp_path / "state")


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "dry"
    d.mkdir()
    (d / "todos.md").write_text(FIX.read_text(encoding="utf-8"), encoding="utf-8")
    return DryTree(d)


def run_migrate(cfg, tree, run=None, new_id=None, **kw):
    return todomigrate.migrate(cfg, tree, today=TODAY, run=run or FakeRun(), log=lambda m: None,
                               new_id=new_id or gen("aaaaaa", "bbbbbb", "cccccc"), **kw)


def by_id(records):
    return {r.id: r for r in records}


def test_decide_item_linking_merged_pr_gets_no_check(cfg, tree):
    r = by_id(run_migrate(cfg, tree))["272d63"]
    assert r.done_when == []
    assert r.project == "widget"
    # catches: treating any item that mentions a PR URL as closable by that PR


def test_merge_item_with_one_pr_gets_check(cfg, tree):
    r = by_id(run_migrate(cfg, tree))["4f6cc0"]
    assert r.done_when == [{"pr": "FibonAdithya/widget#5", "state": "merged"}]
    # catches: never attaching the check to a Merge item with a single PR URL


def test_two_repos_is_unsorted(cfg, tree):
    r = by_id(run_migrate(cfg, tree))
    assert r["d4e5f6"].project == "unsorted"
    assert r["b2c3d4"].project == "gizmo"
    # catches: taking the first matching repo instead of requiring exactly one


def test_title_prefix_counts_only_when_in_repo_list(cfg, tree):
    t = (tree.dir / "todos.md")
    t.write_text(t.read_text(encoding="utf-8").replace(
        "- [ ] Old client item without an id", "- [ ] gadget: sweep logs\n- [ ] merge: fix things"), encoding="utf-8")
    r = {x.title: x for x in run_migrate(cfg, tree)}
    assert r["gadget: sweep logs"].project == "gadget"
    assert r["merge: fix things"].project == "unsorted"
    # catches: using any lowercase title prefix as a project


def test_ticked_item_closed_at_from_history(cfg, tree):
    r = by_id(run_migrate(cfg, tree))
    assert r["b2c3d4"].status == "done"
    assert r["b2c3d4"].closed == {"at": "2026-11-16", "by": "agent", "reason": "ticked before migration"}
    assert r["a1b2c3"].closed == {"at": "2026-11-19", "by": "owner", "reason": "ticked before migration"}
    assert r["a1b2c3"].done_when == []
    # catches: always using today, or the latest instead of the earliest done commit


def test_history_call_and_failure(cfg, tree):
    run = FakeRun()
    run_migrate(cfg, tree, run=run)
    argv, kw = run.calls[0]
    assert argv == ["gh", "api", "--paginate", "repos/FibonAdithya/hermes-wiki/commits?path=todos.md",
                    "--jq", ".[] | {m: .commit.message, d: .commit.committer.date}"]
    assert kw == {"capture_output": True, "text": True, "timeout": 60}
    # catches: a wrong history endpoint, jq filter or timeout


def test_history_failure_refuses(cfg, tree):
    with pytest.raises(TodoError) as e:
        run_migrate(cfg, tree, run=FakeRun(rc=1, stderr="boom"))
    assert e.value.code == 1 and "commit history" in str(e.value) and "boom" in str(e.value)
    assert not (tree.dir / "agent-todos.jsonl").exists()
    # catches: silently falling back to today when the history call fails


def test_no_history_mode_makes_no_call(cfg, tree):
    run = FakeRun()
    r = by_id(run_migrate(cfg, tree, run=run, history=False))
    assert run.calls == [] and r["b2c3d4"].closed["by"] == "owner"
    # catches: calling gh in dry-publish mode


def test_ids_kept_fresh_for_missing_and_duplicate(cfg, tree):
    records = run_migrate(cfg, tree, new_id=gen("4f6cc0", "aaaaaa", "bbbbbb"))
    ids = [r.id for r in records]
    assert len(set(ids)) == len(ids) == 7
    titles = {r.title: r.id for r in records}
    assert titles["Old client item without an id"] == "aaaaaa"
    assert titles["Duplicate id item"] == "bbbbbb"
    assert by_id(records)["4f6cc0"].title.startswith("Merge widget")
    r = by_id(records)["272d63"]
    assert (r.created, r.source) == ("2026-11-12", "claude-box")
    assert by_id(records)["aaaaaa"].created == "2026-11-19"
    assert by_id(records)["aaaaaa"].source == "claude-box"
    # catches: dropping or overwriting items that repeat or lack an id


def test_refuses_when_already_migrated(cfg, tree):
    (tree.dir / "agent-todos.jsonl").write_text("", encoding="utf-8")
    before = (tree.dir / "todos.md").read_text(encoding="utf-8")
    with pytest.raises(TodoError, match="already migrated: agent-todos.jsonl exists") as e:
        run_migrate(cfg, tree)
    assert e.value.code == 1
    assert (tree.dir / "todos.md").read_text(encoding="utf-8") == before
    # catches: double migration


def test_refuses_when_todos_md_missing(cfg, tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(TodoError) as e:
        run_migrate(cfg, DryTree(d))
    assert e.value.code == 1
    # catches: committing a migration without a todos.md


class RecordingTree(DryTree):
    def __init__(self, d):
        super().__init__(d)
        self.commits = []

    def commit(self, base, files, message):
        self.commits.append((files, message))
        super().commit(base, files, message)


def test_one_commit_with_four_files_and_owner_sections_intact(cfg, tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    old = FIX.read_text(encoding="utf-8")
    (d / "todos.md").write_text(old, encoding="utf-8")
    tree = RecordingTree(d)
    records = run_migrate(cfg, tree)
    assert len(tree.commits) == 1
    files, message = tree.commits[0]
    assert message == "todo: migrate"
    assert sorted(files) == [".gitignore", "agent-todos.jsonl", "agent-todos.md", "todos.md"]
    assert files[".gitignore"] == ".todo-suggestions.jsonl\n"
    assert loads(files["agent-todos.jsonl"]) == sorted(records, key=lambda r: r.id)
    assert files["agent-todos.md"].startswith("---\ntype: log\ntags: [people]\nupdated: ")
    new = files["todos.md"]
    start = old.index("## Agent follow-ups")
    end = old.index("## See also")
    assert new[:start] == old[:start]
    assert new[start:new.index("## See also")] == (
        "## Agent follow-ups\n![[agent-todos]]\nOpen the page to tick: [[agent-todos]]\n\n")
    assert new[new.index("## See also"):] == old[end:]
    assert "Buy a desk lamp" in new and "999999" in new
    assert take_section_items(new)[0] == []
    # catches: touching owner sections, or losing the blank line before the next heading


def test_todos_md_without_following_heading_and_no_trailing_newline(cfg, tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "todos.md").write_text("# T\n\n## Agent follow-ups\n- [ ] One (2026-11-12, claude-box, id 111111)", encoding="utf-8")
    tree = RecordingTree(d)
    run_migrate(cfg, tree)
    assert tree.commits[0][0]["todos.md"] == (
        "# T\n\n## Agent follow-ups\n![[agent-todos]]\nOpen the page to tick: [[agent-todos]]\n")
    # catches: a malformed heading line when the section is last and unterminated


@pytest.mark.parametrize("old,new", [
    (None, ".todo-suggestions.jsonl\n"),
    ("node_modules/\n", "node_modules/\n.todo-suggestions.jsonl\n"),
    ("node_modules/", "node_modules/\n.todo-suggestions.jsonl\n"),
    (".todo-suggestions.jsonl\n", None),
])
def test_gitignore(old, new):
    assert todomigrate._gitignore(old) == new
    # catches: glued lines, duplicate entries, or a missing .gitignore


def test_gitignore_unchanged_is_not_committed(cfg, tmp_path):
    d = tmp_path / "g"
    d.mkdir()
    (d / "todos.md").write_text(FIX.read_text(encoding="utf-8"), encoding="utf-8")
    (d / ".gitignore").write_text("a\n.todo-suggestions.jsonl\n", encoding="utf-8")
    tree = RecordingTree(d)
    run_migrate(cfg, tree)
    assert ".gitignore" not in tree.commits[0][0]
    # catches: rewriting .gitignore when the line is already present


def test_commit_errors(cfg, tree):
    from hermes_wiki.todos import Conflict, StoreError, WriteUncertain
    for exc, text in [(Conflict(), "wiki changed during migration; re-run"),
                      (WriteUncertain(), "check whether the migrate commit landed"),
                      (StoreError("bad"), "GitHub: bad")]:
        def commit(base, files, message, exc=exc):
            raise exc
        tree.commit = commit
        with pytest.raises(TodoError, match=text) as e:
            run_migrate(cfg, tree)
        assert e.value.code == 1
    # catches: leaking raw store exceptions or retrying a possibly landed commit


def _conf(tmp_path, **extra):
    gh = tmp_path / "gh-stub"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/gh.log\nexit 97\n', encoding="utf-8")
    gh.chmod(0o755)
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"root": str(tmp_path), "source": "claude-box", "gh": str(gh),
                                "state_dir": str(tmp_path / "state"), **extra}), encoding="utf-8")
    return conf


def test_cli_prints_one_line_per_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    dry = tmp_path / "state" / "dry"
    dry.mkdir(parents=True)
    (dry / "todos.md").write_text(FIX.read_text(encoding="utf-8"), encoding="utf-8")
    assert cli.main(["todo", "--config", str(_conf(tmp_path)), "migrate"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 7
    assert "4f6cc0 widget pr:FibonAdithya/widget#5 Merge widget PR #5 (zsh fix in the plan)" in lines
    assert "272d63 widget -- Decide what to do about widget PR #4" in lines
    assert (dry / "agent-todos.jsonl").exists()
    assert not (tmp_path / "gh.log").exists()
    assert cli.main(["todo", "--config", str(_conf(tmp_path)), "migrate"]) == 1
    # catches: wrong line format, or calling gh in dry mode


def test_cli_public_only_refused_without_github_call(tmp_path, capsys):
    assert cli.main(["todo", "--config", str(_conf(tmp_path, public_only=True)), "migrate"]) == 2
    assert not (tmp_path / "gh.log").exists()
    assert capsys.readouterr().out == ""
    # catches: migrating (or contacting GitHub) from a public_only config


def test_refuses_without_section(cfg, tmp_path):
    d = tmp_path / "n"
    d.mkdir()
    (d / "todos.md").write_text("# T\n\n## Other\n- [ ] x\n", encoding="utf-8")
    tree = RecordingTree(d)
    with pytest.raises(TodoError, match="no .## Agent follow-ups. section") as e:
        run_migrate(cfg, tree)
    assert e.value.code == 1 and tree.commits == []
    # catches: committing a migration (and an embed) when the heading is absent


def test_merge_hyphen_title_gets_no_check(cfg, tree):
    t = tree.dir / "todos.md"
    t.write_text(t.read_text(encoding="utf-8").replace(
        "- [ ] Old client item without an id",
        "- [ ] Merge-conflict fix (2026-11-16, claude-box, id 777777)\n  - https://github.com/FibonAdithya/widget/pull/9\n- [ ] Old client item without an id"),
        encoding="utf-8")
    r = by_id(run_migrate(cfg, tree, new_id=gen("aaaaaa", "bbbbbb")))["777777"]
    assert r.done_when == []
    # catches: the title guard using \\b, which matches Merge-conflict


def test_history_ignores_other_ids_and_rejects_garbage(cfg, tree):
    hist = HISTORY + [{"m": "todo: done 123456", "d": "2026-01-01T00:00:00Z"},
                      {"m": "todo: done a1b2c3\n\nbody", "d": "2026-11-12T08:00:00Z"}]
    records = run_migrate(cfg, tree, run=FakeRun(history=hist))
    assert by_id(records)["a1b2c3"].closed["by"] == "agent"
    assert by_id(records)["a1b2c3"].closed["at"] == "2026-11-12"
    assert by_id(records)["b2c3d4"].closed["at"] == "2026-11-16"
    # catches: matching the whole message instead of its first line, or applying unrelated commits


def test_history_garbage_line_refuses(cfg, tree):
    class Garbage(FakeRun):
        def __call__(self, argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout="not json\n", stderr="")

    with pytest.raises(TodoError) as e:
        run_migrate(cfg, tree, run=Garbage())
    assert e.value.code == 1 and "commit history" in str(e.value)
    # catches: ignoring a malformed history line and falling back to today


def test_history_bad_date_refuses(cfg, tree):
    hist = HISTORY + [{"m": "todo: done a1b2c3", "d": "not-a-date-at-all"}]
    with pytest.raises(TodoError) as e:
        run_migrate(cfg, tree, run=FakeRun(history=hist))
    assert "commit history" in str(e.value)
    assert not (tree.dir / "agent-todos.jsonl").exists()
    # catches: writing a closed.at that loads() later rejects, which would break every todo command
