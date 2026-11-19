import io
import json
import re
from pathlib import Path

import pytest

from hermes_wiki import cli

FIXTURE = Path(__file__).parent / "fixtures" / "todos.md"


@pytest.fixture
def env(tmp_path):
    gh_log = tmp_path / "gh.log"
    gh = tmp_path / "gh-stub"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {gh_log}\nexit 97\n', encoding="utf-8")
    gh.chmod(0o755)
    root = tmp_path / "wiki"
    root.mkdir()

    def make_conf(**extra):
        conf = tmp_path / "c.json"
        conf.write_text(json.dumps({"root": str(root), "source": "claude-box", "state_dir": str(tmp_path / "state"),
                                    "gh": str(gh), **extra}), encoding="utf-8")
        return conf

    dry = tmp_path / "state" / "dry" / "todos.md"

    def seed():
        dry.parent.mkdir(parents=True, exist_ok=True)
        dry.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    return type("Env", (), {"conf": make_conf, "dry": dry, "seed": staticmethod(seed), "gh_log": gh_log})


def test_add_prints_only_id_and_writes_dry_file(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    assert cli.main(["todo", "--config", str(env.conf()), "add", "Fix the thing"]) == 0
    out = capsys.readouterr().out
    assert re.fullmatch(r"[0-9a-f]{6}\n", out)
    assert f"id {out.strip()})" in env.dry.read_text(encoding="utf-8")
    assert not env.gh_log.exists()


def test_public_only_list_exit2_no_gh_call(env, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_WIKI_DRY_PUBLISH", raising=False)
    assert cli.main(["todo", "list", "--config", str(env.conf(public_only=True))]) == 2
    assert not env.gh_log.exists()


def test_public_only_add_prints_exactly_id(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    assert cli.main(["todo", "add", "T", "--config", str(env.conf(public_only=True))]) == 0
    cap = capsys.readouterr()
    assert re.fullmatch(r"[0-9a-f]{6}\n", cap.out)
    assert "desk lamp" not in cap.out + cap.err


def test_detail_stdin_combines_after_flags(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO("b\n\n c \n"))
    env.seed()
    argv = ["todo", "add", "T", "--detail", "a", "--detail-stdin", "--config", str(env.conf())]
    assert cli.main(argv) == 0
    lines = env.dry.read_text(encoding="utf-8").splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("- [ ] T ("))
    assert lines[i + 1:i + 4] == ["  - a", "  - b", "  - c"]


def test_secret_exit2_and_pattern_name(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    before = env.dry.read_text(encoding="utf-8")
    rc = cli.main(["todo", "add", "token ghp_" + "a" * 36, "--config", str(env.conf())])
    assert rc == 2
    assert "github-token" in capsys.readouterr().err
    assert env.dry.read_text(encoding="utf-8") == before


def test_done_then_done_again(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    conf = str(env.conf())
    cli.main(["todo", "add", "T", "--config", conf])
    item_id = capsys.readouterr().out.strip()
    assert cli.main(["todo", "done", item_id, "--config", conf]) == 0
    assert f"- [x] T (" in env.dry.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""
    assert cli.main(["todo", "done", item_id, "--config", conf]) == 0
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "already done" in cap.err
    assert cli.main(["todo", "done", "ffffff", "--config", conf]) == 1


def test_list_all(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    conf = str(env.conf())
    cli.main(["todo", "add", "First", "--config", conf])
    a = capsys.readouterr().out.strip()
    cli.main(["todo", "add", "Second", "--config", conf])
    b = capsys.readouterr().out.strip()
    cli.main(["todo", "done", a, "--config", conf])
    capsys.readouterr()
    assert cli.main(["todo", "list", "--config", conf]) == 0
    out = capsys.readouterr().out
    assert f"{b} Second" in out and f"{a} First" not in out
    assert cli.main(["todo", "list", "--all", "--config", conf]) == 0
    out = capsys.readouterr().out
    assert f"{b} Second" in out and f"{a} [done] First" in out


def test_config_after_subcommand_args(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    assert cli.main(["todo", "add", "T", "--config", str(env.conf())]) == 0


def test_missing_todos_rc1(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    assert cli.main(["todo", "add", "T", "--config", str(env.conf())]) == 1
    assert not env.dry.exists()


@pytest.fixture
def migrated(env, monkeypatch):
    from datetime import date
    from hermes_wiki.todopage import render
    from hermes_wiki.todorecords import dumps

    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    d = env.dry.parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-todos.jsonl").write_text("", encoding="utf-8")
    (d / "agent-todos.md").write_text(render([], date(2026, 11, 19)), encoding="utf-8")
    return d / "agent-todos.jsonl"


def _recs(jl):
    from hermes_wiki.todorecords import loads
    return loads(jl.read_text(encoding="utf-8"))


def test_migrated_flags_pass_through(env, migrated, capsys):
    conf = str(env.conf())
    assert cli.main(["todo", "add", "T", "--project", "x", "--done-when", "pr:a/b#1", "--config", conf]) == 0
    i = capsys.readouterr().out.strip()
    r = _recs(migrated)[0]
    assert r.project == "x" and r.done_when == [{"pr": "a/b#1", "state": "merged"}]
    assert cli.main(["todo", "done", i, "--reason", "r", "--config", conf]) == 0
    assert _recs(migrated)[0].closed["reason"] == "r"
    assert cli.main(["todo", "reopen", i, "--config", conf]) == 0
    assert cli.main(["todo", "edit", i, "--add-detail", "d", "--config", conf]) == 0
    assert _recs(migrated)[0].details == ["d"]
    assert cli.main(["todo", "add", "Other", "--project", "y", "--config", conf]) == 0
    other = capsys.readouterr().out.strip()
    assert cli.main(["todo", "list", "--json", "--project", "x", "--config", conf]) == 0
    assert json.loads(capsys.readouterr().out.strip())["id"] == i
    assert cli.main(["todo", "rm", i, "--config", conf]) == 0
    assert [r.id for r in _recs(migrated)] == [other]
    assert cli.main(["todo", "rm", i, "--config", conf]) == 1
    assert "no item with id" in capsys.readouterr().err  # catches flags parsed but not passed through


def test_edit_clear_flags_pass_through(env, migrated, capsys):
    conf = str(env.conf())
    assert cli.main(["todo", "add", "T", "--project", "x", "--detail", "d1", "--done-when", "pr:a/b#1",
                     "--config", conf]) == 0
    i = capsys.readouterr().out.strip()
    assert cli.main(["todo", "edit", i, "--clear-done-when", "--done-when", "pr:a/b#2", "--config", conf]) == 0
    r = _recs(migrated)[0]
    assert r.done_when == [{"pr": "a/b#2", "state": "merged"}] and r.details == ["d1"]
    assert cli.main(["todo", "edit", i, "--clear-details", "--add-detail", "d2", "--config", conf]) == 0
    r = _recs(migrated)[0]
    assert r.details == ["d2"] and r.done_when == [{"pr": "a/b#2", "state": "merged"}]  # catches flags parsed but not passed, or swapped


def test_unmigrated_new_commands_exit_1(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    before = env.dry.read_text(encoding="utf-8")
    for argv in (["edit", "aaaaaa", "--title", "x"], ["rm", "aaaaaa"], ["reopen", "aaaaaa"], ["list", "--json"]):
        assert cli.main(["todo", *argv, "--config", str(env.conf())]) == 1
        assert "run hermes-wiki todo migrate first" in capsys.readouterr().err
    assert env.dry.read_text(encoding="utf-8") == before  # catches new commands touching todos.md
