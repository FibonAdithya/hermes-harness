import json
from datetime import date

import pytest

from hermes_wiki import cli, importer
from hermes_wiki import config as cf
from hermes_wiki.notes import NoteRejected
from wikifixture import build_wiki

TODAY = date(2026, 10, 26)


@pytest.mark.parametrize("name,expected", [
    ("-tmp-talos-agentic-plikv7nk", True),
    ("-tmp-pytest-of-fibonadithya-pytest-332-x", True),
    ("TIG-Talos--claude-worktrees-cpu-fallback", True),
    ("-herdr-worktrees-wgan-synthetic-writeup-latex", True),
    ("-home-fibonadithya-hermes-harness", False),
    ("TIG-Talos", False),
])
def test_skip_project_dir(name, expected):
    assert importer.skip_project_dir(name) is expected


@pytest.mark.parametrize("kind,expected", [
    ("feedback", "decision"), ("user", "person"), ("project", "status"),
    ("reference", "fact"), ("weird", "fact"),
])
def test_memory_kind_reads_nested_type(kind, expected):
    text = f"---\nname: x\nmetadata:\n  type: {kind}\n---\nbody\n"
    assert importer.memory_kind(text) == expected


def test_memory_kind_flat_and_missing_and_body_type_ignored():
    assert importer.memory_kind("---\ntype: user\n---\nb") == "person"
    assert importer.memory_kind("no frontmatter\ntype: user\n") == "fact"
    assert importer.memory_kind("---\nname: x\n---\ntype: user\n") == "fact"


@pytest.fixture
def cfg(tmp_path):
    return cf.WikiConfig(root=build_wiki(tmp_path / "wiki"), source="claude-laptop")


def _mem(projects, d, name, text):
    p = projects / d / "memory"
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(text, encoding="utf-8")


def test_import_truncates_and_tags_source(tmp_path, cfg):
    projects = tmp_path / "projects"
    words = " ".join(f"w{i}" for i in range(500))
    _mem(projects, "-home-u-proj", "a.md", f"---\nmetadata:\n  type: feedback\n---\n{words}\n")
    _mem(projects, "-home-u-proj", "MEMORY.md", "- index\n")
    _mem(projects, "-tmp-skipme", "b.md", "hello there\n")
    calls = []

    def add(c, **kw):
        calls.append((c, kw))
        return "inbox/import-claude-memory/x.md"

    out = importer.import_claude_memory(projects, cfg, add=add, today=TODAY)
    assert len(calls) == 1
    c, kw = calls[0]
    assert c.source == "import-claude-memory"
    assert cfg.source == "claude-laptop"
    assert kw["kind"] == "decision"
    assert kw["tags"] == [] and kw["repos"] == [] and kw["verified"] == "claim"
    assert kw["session"] == "import:-home-u-proj"
    assert kw["today"] == TODAY
    body = kw["body"]
    assert body.startswith("Imported from Claude Code project -home-u-proj.\n\n")
    assert "type: feedback" not in body
    assert len(body.split()) == 300
    assert body.endswith("[truncated]")
    assert out == [(str(projects / "-home-u-proj" / "memory" / "a.md"), "inbox/import-claude-memory/x.md")]


def test_import_short_body_not_truncated(tmp_path, cfg):
    projects = tmp_path / "projects"
    _mem(projects, "d", "a.md", "---\ntype: user\n---\nshort fact\n")
    seen = []
    importer.import_claude_memory(projects, cfg, add=lambda c, **kw: seen.append(kw["body"]) or "p")
    assert seen[0] == "Imported from Claude Code project d.\n\nshort fact"
    assert "[truncated]" not in seen[0]


def test_import_reports_secret_rejection(tmp_path, cfg):
    projects = tmp_path / "projects"
    _mem(projects, "d", "a.md", "---\ntype: user\n---\ntoken ghp_" + "a" * 36 + "\n")
    _mem(projects, "d", "b.md", "---\ntype: user\n---\nclean note\n")
    from hermes_wiki.notes import add_note

    def add(c, **kw):
        return add_note(c, publisher=lambda *a, **k: None, **kw)

    out = dict(importer.import_claude_memory(projects, cfg, add=add, today=TODAY))
    a, b = str(projects / "d/memory/a.md"), str(projects / "d/memory/b.md")
    assert out[a].startswith("skipped: ")
    assert out[b].startswith("inbox/import-claude-memory/2026-10-26-person-")


def _cli_setup(tmp_path):
    root = build_wiki(tmp_path / "wiki")
    conf = tmp_path / "c.json"
    state = tmp_path / "state"
    conf.write_text(json.dumps({"root": str(root), "source": "claude-laptop", "state_dir": str(state)}))
    return conf, state, tmp_path / "projects"


def test_dry_run_cli_never_publishes(tmp_path, monkeypatch, capsys):
    from hermes_wiki import notes
    conf, state, projects = _cli_setup(tmp_path)
    _mem(projects, "d", "a.md", "---\ntype: user\n---\nclean note\n")
    _mem(projects, "d", "b.md", "---\ntype: user\n---\ntoken ghp_" + "a" * 36 + "\n")
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")

    def boom(*a, **k):
        raise AssertionError("published")

    monkeypatch.setattr(notes, "publish", boom)
    monkeypatch.setattr(notes.subprocess, "run", boom)
    assert cli.main(["import-claude-memory", "--projects", str(projects), "--dry-run", "--config", str(conf)]) == 0
    out = capsys.readouterr().out
    assert "inbox/import-claude-memory/" in out
    assert "skipped: " in out
    assert "would import 1, skipped 1, failed 0" in out
    assert not state.exists()  # no dry dir, no imported.json


def _publisher_failing_after(n, calls):
    def pub(repo, path, content, **kw):
        if len(calls) >= n:
            raise notes_mod.PublishError("network")
        calls.append(path)
    return pub


from hermes_wiki import notes as notes_mod  # noqa: E402


def test_rerun_after_partial_failure_publishes_only_remaining(tmp_path, cfg):
    projects = tmp_path / "projects"
    for n in "abc":
        _mem(projects, "d", f"{n}.md", f"---\ntype: user\n---\nnote {n}\n")
    ledger = tmp_path / "state" / "imported.json"
    calls = []

    def add_fail(c, **kw):
        return notes_mod.add_note(c, publisher=_publisher_failing_after(2, calls), **kw)

    out = importer.import_claude_memory(projects, cfg, add=add_fail, today=TODAY, ledger=ledger)
    assert [r.startswith("inbox/") for _, r in out] == [True, True, False]
    assert out[2][1].startswith("failed: ")
    assert len(json.loads(ledger.read_text())) == 2
    calls2 = []

    def add_ok(c, **kw):
        return notes_mod.add_note(c, publisher=lambda r, p, t, **k: calls2.append(p), **kw)

    out2 = importer.import_claude_memory(projects, cfg, add=add_ok, today=TODAY, ledger=ledger)
    assert len(calls2) == 1
    assert [r for _, r in out2][:2] == ["skipped: already imported"] * 2
    assert len(json.loads(ledger.read_text())) == 3


def test_changed_file_is_reimported(tmp_path, cfg):
    projects = tmp_path / "projects"
    _mem(projects, "d", "a.md", "---\ntype: user\n---\nv1\n")
    ledger = tmp_path / "imported.json"
    seen = []
    add = lambda c, **kw: seen.append(kw["body"]) or "p"  # noqa: E731
    importer.import_claude_memory(projects, cfg, add=add, ledger=ledger)
    _mem(projects, "d", "a.md", "---\ntype: user\n---\nv2\n")
    importer.import_claude_memory(projects, cfg, add=add, ledger=ledger)
    assert len(seen) == 2


def _fake_gh(tmp_path):
    """A stand-in for `gh`: logs each call; fails while <tmp>/fail exists and one call has succeeded."""
    log, flag = tmp_path / "gh.log", tmp_path / "fail"
    script = tmp_path / "fake-gh"
    script.write_text(f"#!/bin/sh\nif [ -e '{flag}' ] && [ -s '{log}' ]; then echo boom >&2; exit 1; fi\n"
                      f"echo call >> '{log}'\n")
    script.chmod(0o755)
    return script, log, flag


def test_cli_exit_1_on_publish_failure_and_resume(tmp_path, capsys):
    conf, state, projects = _cli_setup(tmp_path)
    gh, log, flag = _fake_gh(tmp_path)  # never the real gh
    data = json.loads(conf.read_text())
    data["gh"] = str(gh)
    conf.write_text(json.dumps(data))
    _mem(projects, "d", "a.md", "---\ntype: user\n---\nnote a\n")
    _mem(projects, "d", "b.md", "---\ntype: user\n---\nnote b\n")
    flag.write_text("")
    argv = ["import-claude-memory", "--projects", str(projects), "--config", str(conf)]
    assert cli.main(argv) == 1
    assert "imported 1, skipped 0, failed 1" in capsys.readouterr().out
    flag.unlink()
    assert cli.main(argv) == 0
    assert "imported 1, skipped 1, failed 0" in capsys.readouterr().out
    assert len(log.read_text().splitlines()) == 2


def test_missing_projects_dir_exits_2(tmp_path, capsys):
    conf, _, projects = _cli_setup(tmp_path)
    assert cli.main(["import-claude-memory", "--projects", str(projects), "--config", str(conf)]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1


def test_install_hook_cli(tmp_path):
    root = build_wiki(tmp_path / "wiki")
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"root": str(root), "source": "claude-laptop"}))
    s = tmp_path / "settings.json"
    assert cli.main(["install-hook", "--settings", str(s), "--command", "x session-end", "--config", str(conf)]) == 0
    assert "x session-end" in s.read_text()
