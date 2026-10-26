import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from hermes_wiki import config as cf
from hermes_wiki import extract as ex
from hermes_wiki import notes as nt
from wikifixture import build_wiki

NOW = time.time() - 3600
FIXTURE = Path(__file__).parent / "fixtures" / "transcript.jsonl"


@pytest.fixture
def cfg(tmp_path):
    root = build_wiki(tmp_path / "wiki")
    return cf.WikiConfig(root=root, source="claude-laptop", state_dir=tmp_path / "state",
                         bin="/x/hermes-wiki", path=tmp_path / "config.json")


@pytest.mark.parametrize("env,cwd,n,want", [
    ({"WIKI_NOTE_CHILD": "1"}, "/home/u/p", 9, "child"),
    ({}, "/tmp/x", 9, "tmp"),
    ({}, "/var/pytest-of-u/pytest-0/t", 9, "tmp"),
    ({}, "SUB", 9, "opted-out"),
    ({}, "/home/u/p", 3, "short"),
    ({}, "/home/u/p", 4, None),
])
def test_skip_reasons(tmp_path, monkeypatch, env, cwd, n, want):
    if cwd == "SUB":
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".no-wiki-notes").write_text("")
        (repo / "a" / "b").mkdir(parents=True)
        cwd = str(repo / "a" / "b")
        monkeypatch.setattr(ex, "_is_tmp", lambda c: False)  # tmp_path lives under /tmp
    hook = {"cwd": cwd, "transcript_path": str(FIXTURE)}
    assert ex.skip_reason(hook, {**env, "HOME": str(tmp_path)}, n) == want


def test_optout_stops_at_git_root(tmp_path):
    (tmp_path / ".no-wiki-notes").write_text("")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert ex._opted_out(str(repo), "/nonexistent") is False


def test_optout_stops_at_home(tmp_path):
    (tmp_path / ".no-wiki-notes").write_text("")
    sub = tmp_path / "home" / "proj"
    sub.mkdir(parents=True)
    assert ex._opted_out(str(sub), str(tmp_path / "home")) is False
    assert ex._opted_out(str(sub), "/nonexistent") is True


def test_skip_no_transcript():
    assert ex.skip_reason({"cwd": "/home/u/p"}, {}, 9) == "no-transcript"


def test_claude_argv_is_subscription_safe(tmp_path):
    argv = ex.claude_argv(tmp_path / "m.json", "sonnet", "/bin/claude")
    assert argv[:2] == ["/bin/claude", "-p"]
    assert "--bare" not in argv
    assert argv[argv.index("--settings") + 1] == '{"disableAllHooks": true}'
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(tmp_path / "m.json")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == ""
    i = argv.index("--allowedTools")
    assert argv[i + 1:i + 5] == ["mcp__wiki__search", "mcp__wiki__read", "mcp__wiki__list_tags",
                                 "mcp__wiki__add_note"]


def test_write_mcp_config(cfg, tmp_path):
    p = tmp_path / "mcp.json"
    ex.write_mcp_config(cfg, "sess1", p)
    assert json.loads(p.read_text()) == {"mcpServers": {"wiki": {
        "command": "/x/hermes-wiki", "args": ["mcp", "--config", str(cfg.path)],
        "env": {"HERMES_WIKI_SESSION": "sess1"}}}}


def _hook(tmp_path, sid="s1", cwd="/home/u/proj"):
    return json.dumps({"session_id": sid, "transcript_path": str(FIXTURE), "cwd": cwd, "reason": "exit"})


def test_session_end_queues_and_spawns_once(cfg, tmp_path):
    calls = []
    out = ex.session_end(_hook(tmp_path), {}, cfg, spawn=calls.append)
    assert out == "queued"
    pending = cfg.state_dir / "pending" / "s1.txt"
    first, rest = pending.read_text().split("\n", 1)
    assert json.loads(first) == {"session": "s1", "cwd": "/home/u/proj"}
    assert "OWNER: Thanks, that is all." in rest
    assert len(calls) == 1
    assert calls[0] == ["/x/hermes-wiki", "drain", "--limit", "4", "--config", str(cfg.path)]


def test_session_end_skip_does_not_spawn(cfg, tmp_path):
    calls = []
    assert ex.session_end(_hook(tmp_path), {"WIKI_NOTE_CHILD": "1"}, cfg, spawn=calls.append) == "child"
    assert calls == []
    assert not (cfg.state_dir / "pending").exists()


def test_session_end_never_raises(cfg):
    assert ex.session_end("not json", {}, cfg, spawn=lambda a: None) == "error"
    assert (cfg.state_dir / "log" / "hook-errors.log").exists()


def _pending(cfg, name, mtime, text="OWNER: hi"):
    d = cfg.state_dir / "pending"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.txt"
    p.write_text(json.dumps({"session": name, "cwd": "/c"}) + "\n" + text + f" {name}\n")
    os.utime(p, (mtime, mtime))
    return p


class FakeRun:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.code, "", "")


def test_drain_newest_first_and_limit(cfg):
    files = [_pending(cfg, f"s{i}", NOW + i) for i in range(6)]
    run = FakeRun(0)
    res = ex.drain(cfg, limit=4, run=run)
    assert res == [("s5", True), ("s4", True), ("s3", True), ("s2", True)]
    assert [f.exists() for f in files] == [True, True, False, False, False, False]
    for argv, kw in run.calls:
        assert kw["env"]["WIKI_NOTE_CHILD"] == "1"
        assert "OWNER: hi" in kw["input"]
        assert kw["timeout"] == 900
        assert argv[0] == cfg.claude and "--bare" not in argv


def test_drain_passes_session_to_mcp_config(cfg):
    _pending(cfg, "abc", NOW)
    seen = {}

    def run(argv, **kw):
        seen.update(json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text()))
        return subprocess.CompletedProcess(argv, 0, "", "")

    ex.drain(cfg, run=run)
    assert seen["mcpServers"]["wiki"]["env"]["HERMES_WIKI_SESSION"] == "abc"


def test_drain_keeps_pending_on_failure(cfg):
    p = _pending(cfg, "s0", NOW)
    assert ex.drain(cfg, run=FakeRun(1)) == [("s0", False)]
    assert p.exists()


def test_drain_keeps_pending_on_timeout(cfg):
    p = _pending(cfg, "s0", NOW)

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 900)

    assert ex.drain(cfg, run=run) == [("s0", False)]
    assert p.exists()


def test_drain_lock_held_returns_empty(cfg):
    _pending(cfg, "s0", NOW)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.state_dir / "drain.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        run = FakeRun(0)
        assert ex.drain(cfg, run=run) == []
        assert run.calls == []
    finally:
        os.close(fd)


def test_extract_dir_uses_newest_and_drains_with_limit4(cfg, tmp_path):
    proj = tmp_path / "projects" / "p"
    (proj / "subagents").mkdir(parents=True)
    old, new, sub = proj / "old.jsonl", proj / "new.jsonl", proj / "subagents" / "agent-1.jsonl"
    for f in (old, new, sub):
        shutil.copy(FIXTURE, f)
    sub.write_text("SUBAGENT-TRANSCRIPT\n")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    os.utime(sub, (3000, 3000))
    _pending(cfg, "other", NOW)
    run = FakeRun(0)
    assert ex.extract_dir(cfg, tmp_path / "projects", "task-7", run=run) is True
    assert len(run.calls) == 2  # the new file and the older pending retry
    inputs = [kw["input"] for _, kw in run.calls]
    assert any("OWNER: Thanks, that is all." in i for i in inputs)
    assert not any("SUBAGENT-TRANSCRIPT" in i for i in inputs)


def test_extract_dir_failure_keeps_pending_for_retry(cfg, tmp_path):
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    shutil.copy(FIXTURE, proj / "a.jsonl")
    assert ex.extract_dir(cfg, tmp_path / "projects", "task-7", run=FakeRun(1)) is False
    assert (cfg.state_dir / "pending" / "task-7.txt").exists()


def test_extract_dir_no_transcript(cfg, tmp_path):
    (tmp_path / "empty").mkdir()
    assert ex.extract_dir(cfg, tmp_path / "empty", "t", run=FakeRun(0)) is False


def test_prompt_has_no_injection_phrases():
    text = ex.load_prompt().lower()
    for bad in ("ignore previous", "ignore all", "disregard", "you are now", "new instructions"):
        assert bad not in text
    assert text.rstrip().endswith("most sessions should produce zero notes.")


def test_dry_publish_writes_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")

    def boom(*a, **k):
        raise AssertionError("gh must not run")

    nt.publish("o/r", "inbox/claude-laptop/a.md", "hello é\n", run=boom, dry_dir=tmp_path / "dry")
    assert (tmp_path / "dry" / "inbox/claude-laptop/a.md").read_text(encoding="utf-8") == "hello é\n"


def test_publish_without_dry_env_calls_gh(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WIKI_DRY_PUBLISH", raising=False)
    calls = []
    nt.publish("o/r", "inbox/a.md", "x", run=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
               dry_dir=tmp_path / "dry")
    assert len(calls) == 1 and not (tmp_path / "dry").exists()


def test_drain_input_has_prompt_and_session_dir(cfg):
    _pending(cfg, "s0", NOW)
    run = FakeRun(0)
    ex.drain(cfg, run=run)
    inp = run.calls[0][1]["input"]
    assert inp.startswith(ex.load_prompt())
    assert "Session directory: /c\n\nOWNER: hi" in inp
    assert run.calls[0][1]["encoding"] == "utf-8"


def test_drain_runs_in_state_dir(cfg):
    _pending(cfg, "s0", NOW)
    run = FakeRun(0)
    ex.drain(cfg, run=run)
    assert run.calls[0][1]["cwd"] == cfg.state_dir


def test_drain_child_env_strips_claude_vars(cfg, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_OTHER", "keep")
    _pending(cfg, "s0", NOW)
    run = FakeRun(0)
    ex.drain(cfg, run=run)
    env = run.calls[0][1]["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["CLAUDE_OTHER"] == "keep" and env["WIKI_NOTE_CHILD"] == "1"


def test_drain_logs_returncode_and_stderr(cfg):
    _pending(cfg, "s0", NOW)

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, "o" * 5000, "boom-error")

    ex.drain(cfg, run=run)
    log = (cfg.state_dir / "log" / "drain.log").read_text()
    assert "s0" in log and "rc=3" in log and "boom-error" in log
    assert "o" * 2000 in log and "o" * 2001 not in log


def test_drain_deletes_stale_pending_without_running(cfg):
    stale = _pending(cfg, "old", time.time() - 8 * 86400)
    fresh = _pending(cfg, "new", NOW)
    run = FakeRun(0)
    res = ex.drain(cfg, run=run)
    assert not stale.exists() and not fresh.exists()
    assert len(run.calls) == 1 and res == [("new", True)]
    assert "old" in (cfg.state_dir / "log" / "drain.log").read_text()


def test_drain_not_stale_at_six_days(cfg):
    p = _pending(cfg, "six", time.time() - 6 * 86400)
    ex.drain(cfg, run=FakeRun(1))
    assert p.exists()


def test_drain_continues_after_unexpected_error(cfg):
    bad = _pending(cfg, "bad", NOW + 10)  # newest, runs first
    bad.write_bytes(b"\xff\xfe not utf8\n")
    os.utime(bad, (NOW + 10, NOW + 10))
    _pending(cfg, "good", NOW)
    run = FakeRun(0)
    res = ex.drain(cfg, run=run)
    assert res == [("bad", False), ("good", True)]
    assert len(run.calls) == 1
    assert "bad" in (cfg.state_dir / "log" / "hook-errors.log").read_text()


def test_spawn_detached_uses_state_dir_cwd(cfg, monkeypatch):
    seen = {}
    monkeypatch.setattr(ex.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    ex._spawn_detached(["x"], cfg, "s1")
    assert seen["cwd"] == cfg.state_dir


def _git(*args):
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


def _main_with_worktree(tmp_path):
    main, wt = tmp_path / "main", tmp_path / "elsewhere" / "wt"
    main.mkdir()
    _git("init", "-q", str(main))
    _git("-C", str(main), "commit", "-q", "--allow-empty", "--no-verify", "-m", "c")
    _git("-C", str(main), "worktree", "add", "-q", "-b", "wt", str(wt))
    assert (wt / ".git").is_file()
    (wt / "sub").mkdir()
    return main, wt


def test_optout_in_main_checkout_covers_its_worktree(tmp_path):
    main, wt = _main_with_worktree(tmp_path)
    home = str(tmp_path)
    assert ex._opted_out(str(wt / "sub"), home) is False
    (main / ".no-wiki-notes").write_text("")
    assert ex._opted_out(str(wt / "sub"), home) is True
    assert ex._opted_out(str(wt), home) is True


def test_optout_relative_gitdir(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
    assert ex._opted_out(str(wt), str(tmp_path)) is False
    (main / ".no-wiki-notes").write_text("")
    assert ex._opted_out(str(wt), str(tmp_path)) is True


def test_optout_git_file_walk_continues_to_parent(tmp_path):
    # A .git file is not a repository root: the walk goes on up to a .git directory or $HOME.
    (tmp_path / "outer" / ".no-wiki-notes").parent.mkdir()
    (tmp_path / "outer" / ".no-wiki-notes").write_text("")
    wt = tmp_path / "outer" / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nonexistent/.git/worktrees/wt\n")
    assert ex._opted_out(str(wt), str(tmp_path)) is True
    (wt / ".git").write_text("garbage\n")
    assert ex._opted_out(str(wt), str(tmp_path)) is True
