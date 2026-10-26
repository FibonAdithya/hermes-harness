"""Tests for droplet/wiki-git-sync.sh, using real git against a bare origin."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "droplet" / "wiki-git-sync.sh"


@pytest.fixture
def env(tmp_path):
    e = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    (tmp_path / "home").mkdir()
    return e


def git(cwd, env, *args):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def repos(tmp_path, env):
    origin = tmp_path / "origin.git"
    git(tmp_path, env, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, env, "init", "-q", "-b", "main", str(seed))
    (seed / "alpha.md").write_text("one\ntwo\nthree\n")
    git(seed, env, "add", "alpha.md")
    git(seed, env, "commit", "-q", "-m", "init")
    git(seed, env, "push", "-q", str(origin), "main")
    droplet, outside = tmp_path / "droplet", tmp_path / "outside"
    for d in (droplet, outside):
        git(tmp_path, env, "clone", "-q", "-b", "main", str(origin), str(d))
    return origin, droplet, outside


def run(droplet, env):
    return subprocess.run(
        ["bash", str(SCRIPT), str(droplet)], env=env, capture_output=True, text=True
    )


def outside_push(outside, env, relpath, text, msg="outside"):
    p = outside / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(outside, env, "add", relpath)
    git(outside, env, "commit", "-q", "-m", msg)
    git(outside, env, "push", "-q", "origin", "HEAD:main")


def test_outside_note_and_local_edit_both_land_linear(repos, env):
    origin, droplet, outside = repos
    outside_push(outside, env, "inbox/claude-laptop/n.md", "note\n")
    (droplet / "alpha.md").write_text("one\ntwo\nthree\nfour\n")
    r = run(droplet, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "pushed"
    files = git(origin, env, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "inbox/claude-laptop/n.md" in files
    assert git(origin, env, "show", "main:alpha.md").endswith("four")
    assert git(origin, env, "rev-list", "--merges", "main") == ""


def test_untracked_and_unstaged_work_survives(repos, env):
    origin, droplet, outside = repos
    outside_push(outside, env, "inbox/claude-laptop/n.md", "note\n")
    (droplet / "new-page.md").write_text("fresh\n")
    (droplet / "alpha.md").write_text("ONE\ntwo\nthree\n")
    r = run(droplet, env)
    assert r.returncode == 0, r.stderr
    files = git(origin, env, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "new-page.md" in files and "inbox/claude-laptop/n.md" in files
    assert git(origin, env, "show", "main:alpha.md").startswith("ONE")


def test_conflict_aborts_without_push(repos, env):
    origin, droplet, outside = repos
    outside_push(outside, env, "alpha.md", "one\nOUTSIDE\nthree\n")
    outside_sha = git(outside, env, "rev-parse", "HEAD")
    (droplet / "alpha.md").write_text("one\nDROPLET\nthree\n")
    r = run(droplet, env)
    assert r.returncode == 1
    assert "FATAL: rebase conflict" in r.stderr
    assert git(origin, env, "rev-parse", "main") == outside_sha
    gd = droplet / ".git"
    assert not (gd / "rebase-merge").exists()
    assert not (gd / "rebase-apply").exists()
    assert "wiki: auto-snapshot" in git(droplet, env, "log", "--format=%s")


def test_nothing_to_do(repos, env):
    _, droplet, _ = repos
    r = run(droplet, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "no changes"


def test_pulls_even_without_local_changes(repos, env):
    _, droplet, outside = repos
    outside_push(outside, env, "inbox/claude-laptop/n.md", "note\n")
    r = run(droplet, env)
    assert r.returncode == 0, r.stderr
    assert (droplet / "inbox/claude-laptop/n.md").exists()
    assert r.stdout.strip() == "no changes"
