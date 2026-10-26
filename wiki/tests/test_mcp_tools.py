import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_wiki import config as cf
from hermes_wiki import freshness
from hermes_wiki.frontmatter import parse
from hermes_wiki.mcp_server import make_tools
from wikifixture import build_wiki


@pytest.fixture
def cfg(tmp_path):
    root = build_wiki(tmp_path / "wiki")
    return cf.WikiConfig(root=root, source="claude-laptop", state_dir=tmp_path / "state")


def test_tools_wrap_errors(cfg):
    tools = make_tools(cfg)
    assert "error" in tools["read"]("../x")

    out = json.loads(tools["add_note"]("fact", "Body.", ["no-such-tag"]))
    assert "error" in out

    def failing(repo, path, content, **kw):
        raise __import__("hermes_wiki.notes", fromlist=["x"]).PublishError("boom-message")

    out = json.loads(make_tools(cfg, publisher=failing)["add_note"]("fact", "Body.", ["hermes"]))
    assert "boom-message" in out["error"]


def test_public_only_applies_to_tools(cfg):
    open_tools = make_tools(cfg)
    assert any(h["page"] == "gamma" for h in json.loads(open_tools["search"]()))
    assert "private details" in open_tools["read"]("gamma")

    pub = make_tools(cf.WikiConfig(root=cfg.root, source="claude-laptop", public_only=True))
    assert all(h["page"] != "gamma" for h in json.loads(pub["search"]()))
    assert "error" in pub["read"]("gamma")
    assert "people" not in json.loads(pub["list_tags"]())
    assert "people" in json.loads(open_tools["list_tags"]())


def test_session_from_env(cfg, monkeypatch):
    calls = []
    pub = lambda repo, path, content: calls.append(content)
    monkeypatch.setenv("HERMES_WIKI_SESSION", "abc")
    out = json.loads(make_tools(cfg, publisher=pub)["add_note"]("fact", "Body.", ["hermes"]))
    assert out["path"].startswith("inbox/claude-laptop/")
    assert parse(calls[0])[0]["session"] == "abc"
    monkeypatch.delenv("HERMES_WIKI_SESSION")
    make_tools(cfg, publisher=pub)["add_note"]("fact", "Body.", ["hermes"])
    assert parse(calls[1])[0]["session"] == ""


def test_pull_warning_is_prefixed(cfg, monkeypatch):
    pull_cfg = cf.WikiConfig(root=cfg.root, source="claude-laptop", pull=True, state_dir=cfg.state_dir)
    monkeypatch.setattr(
        freshness.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="no network", stdout=""),
    )
    tools = make_tools(pull_cfg, now=lambda: 1000.0)
    assert tools["search"]().startswith("wiki copy may be stale")
    assert tools["read"]("beta").startswith("wiki copy may be stale")


def test_maybe_pull_respects_interval(tmp_path):
    stamp = tmp_path / "stamp"
    stamp.write_text("")
    os.utime(stamp, (900, 900))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    assert freshness.maybe_pull(tmp_path, stamp, now=1000.0, run=run) is None
    assert calls == []
    os.utime(stamp, (699, 699))
    assert freshness.maybe_pull(tmp_path, stamp, now=1000.0, run=run) is None
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "-C", str(tmp_path)]
    assert stamp.stat().st_mtime == 1000.0


def test_maybe_pull_failure_warns_and_throttles_retries(tmp_path):
    stamp = tmp_path / "state" / "stamp"
    calls = []

    def failing(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stderr="diverged", stdout="")

    msg = freshness.maybe_pull(tmp_path, stamp, now=1000.0, run=failing)
    assert msg.startswith("wiki copy may be stale") and "diverged" in msg
    assert stamp.stat().st_mtime == 1000.0
    # Within the interval: no new pull, and the last warning is still returned.
    assert freshness.maybe_pull(tmp_path, stamp, now=1299.0, run=failing) == msg
    assert len(calls) == 1
    assert freshness.maybe_pull(tmp_path, stamp, now=1300.0, run=failing) == msg
    assert len(calls) == 2

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 20)

    assert freshness.maybe_pull(tmp_path, stamp, now=2000.0, run=timeout).startswith("wiki copy may be stale")
    assert stamp.stat().st_mtime == 2000.0
    ok = lambda argv, **kw: SimpleNamespace(returncode=0, stderr="", stdout="")  # noqa: E731
    assert freshness.maybe_pull(tmp_path, stamp, now=3000.0, run=ok) is None
    assert freshness.maybe_pull(tmp_path, stamp, now=3001.0, run=failing) is None  # success clears the warning
