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
    capsys.readouterr()
    assert cli.main(["todo", "done", item_id, "--config", conf]) == 0
    assert "already done" in capsys.readouterr().err
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
    assert f"{b} Second" in out and f"{a} First" in out


def test_config_after_subcommand_args(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    env.seed()
    assert cli.main(["todo", "add", "T", "--config", str(env.conf())]) == 0


def test_missing_todos_rc1(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    assert cli.main(["todo", "add", "T", "--config", str(env.conf())]) == 1
    assert not env.dry.exists()
