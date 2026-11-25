import base64
import json
import subprocess

import pytest

from hermes_wiki import todotree
from hermes_wiki.todos import Conflict, StoreError, WriteUncertain

SHA = "a" * 40
TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_COMMIT = "d" * 40


def cp(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class FakeRun:
    """Scripted gh: routes by (method, endpoint); `fail` maps a route key to a
    CompletedProcess or exception to return/raise instead."""

    def __init__(self, files=None, fail=None):
        self.files = files or {}
        self.fail = fail or {}
        self.calls = []
        self.kws = []

    def __call__(self, argv, **kw):
        self.kws.append(kw)
        assert kw.get("capture_output") is True and kw.get("text") is True
        assert kw.get("timeout") == 30
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        endpoint = next(a for a in argv[2:] if a.startswith("repos/"))
        body = json.loads(kw["input"]) if "input" in kw else None
        self.calls.append((method, endpoint, body, argv))
        if "--input" in argv:
            assert argv[argv.index("--input") + 1] == "-"
        key = (method, endpoint.split("?")[0].split("/", 3)[3])
        if key in self.fail:
            out = self.fail[key]
            if isinstance(out, Exception):
                raise out
            return out
        if key == ("GET", "git/ref/heads/main"):
            return cp(json.dumps({"object": {"sha": SHA}}))
        if key[0] == "GET" and key[1].startswith("contents/"):
            path = key[1][len("contents/"):]
            if path not in self.files:
                return cp(stderr="gh: Not Found (HTTP 404)", rc=1)
            raw = base64.encodebytes(self.files[path].encode()).decode()
            return cp(json.dumps({"content": raw, "encoding": "base64"}))
        if key == ("GET", f"git/commits/{SHA}"):
            return cp(json.dumps({"tree": {"sha": TREE}}))
        if key == ("POST", "git/trees"):
            return cp(json.dumps({"sha": NEW_TREE}))
        if key == ("POST", "git/commits"):
            return cp(json.dumps({"sha": NEW_COMMIT}))
        if key == ("PATCH", "git/refs/heads/main"):
            return cp(json.dumps({"object": {"sha": NEW_COMMIT}}))
        raise AssertionError(f"unexpected call {key}")


def tree(run):
    return todotree.GhTree("o/r", "gh", run)


def test_commit_sends_one_tree_with_all_files():
    run = FakeRun()
    tree(run).commit(SHA, {"a": "1", "b": "2"}, "m")
    posts = [c for c in run.calls if c[0] == "POST"]
    assert [c[1] for c in posts] == ["repos/o/r/git/trees", "repos/o/r/git/commits"]
    t = posts[0][2]
    assert t["base_tree"] == TREE
    assert t["tree"] == [
        {"path": "a", "mode": "100644", "type": "blob", "content": "1"},
        {"path": "b", "mode": "100644", "type": "blob", "content": "2"},
    ]
    assert posts[1][2] == {"message": "m", "tree": NEW_TREE, "parents": [SHA]}
    patch = [c for c in run.calls if c[0] == "PATCH"]
    assert len(patch) == 1
    assert patch[0][2] == {"sha": NEW_COMMIT, "force": False}
    # catches force=true, two commits, missing base_tree


@pytest.mark.parametrize("stderr,exc", [
    ("gh: Update is not a fast forward (HTTP 422)", Conflict),
    ("gh: Update is not a Fast Forward (HTTP 422)", Conflict),
    ("gh: Object does not exist (HTTP 422)", StoreError),
    ("gh: boom (HTTP 500)", StoreError),
])
def test_422_is_conflict_other_errors_are_store_errors(stderr, exc):
    run = FakeRun(fail={("PATCH", "git/refs/heads/main"): cp(stderr=stderr, rc=1)})
    with pytest.raises(exc) as ei:
        tree(run).commit(SHA, {"a": "1"}, "m")
    assert type(ei.value) is exc
    # catches retrying every 422 as a conflict, or treating 500 as a conflict


def test_timeout_only_on_ref_update_is_uncertain():
    t = subprocess.TimeoutExpired("gh", 30)
    run = FakeRun(fail={("POST", "git/trees"): t})
    with pytest.raises(StoreError):
        tree(run).commit(SHA, {"a": "1"}, "m")
    run = FakeRun(fail={("PATCH", "git/refs/heads/main"): t})
    with pytest.raises(WriteUncertain):
        tree(run).commit(SHA, {"a": "1"}, "m")
    # catches reporting "may have landed" when nothing could have


def test_head_reads_files_at_the_commit_not_main():
    run = FakeRun(files={"agent-todos.jsonl": "x"})
    sha, files = tree(run).head()
    assert sha == SHA
    gets = [c[1] for c in run.calls if "/contents/" in c[1]]
    assert gets == [f"repos/o/r/contents/{p}?ref={SHA}" for p in todotree.PATHS]
    assert files["agent-todos.jsonl"] == "x"
    # catches TOCTOU via ref=main


def test_head_missing_file_is_none():
    run = FakeRun(files={"agent-todos.md": "md\nline\n", "todos.md": "t"})
    sha, files = tree(run).head()
    assert files == {"agent-todos.jsonl": None, "agent-todos.md": "md\nline\n", "todos.md": "t"}
    # catches raising on pre-migration repos


def test_head_other_failure_is_store_error():
    run = FakeRun(fail={("GET", "contents/todos.md"): cp(stderr="gh: x (HTTP 500)", rc=1)})
    with pytest.raises(StoreError):
        tree(run).head()
    # catches mapping every failure to None (silently overwriting a file)


def test_dry_tree_conflict(tmp_path):
    d = todotree.DryTree(tmp_path / "dry")
    base, files = d.head()
    assert files == dict.fromkeys(todotree.PATHS)
    d.commit(base, {"agent-todos.jsonl": "1"}, "m")
    assert (tmp_path / "dry" / "agent-todos.jsonl").read_text() == "1"
    base2, _ = d.head()
    assert base2 != base
    (tmp_path / "dry" / "todos.md").write_text("external")
    with pytest.raises(Conflict):
        d.commit(base2, {"agent-todos.jsonl": "2"}, "m")
    assert (tmp_path / "dry" / "agent-todos.jsonl").read_text() == "1"
    # catches dry mode ignoring base


def test_head_refuses_files_too_large_for_contents_api():
    run = FakeRun(files={"todos.md": "x"})
    orig = run.__call__

    def big(argv, **kw):
        if any(a.startswith("repos/o/r/contents/agent-todos.md") for a in argv):
            return cp(json.dumps({"content": "", "encoding": "none"}))
        return orig(argv, **kw)

    with pytest.raises(StoreError, match="agent-todos.md is too large"):
        tree(big).head()
    # catches decode without checking encoding


@pytest.mark.parametrize("step,route", [
    ("commit", ("GET", f"git/commits/{SHA}")),
    ("trees", ("POST", "git/trees")),
    ("commits", ("POST", "git/commits")),
])
@pytest.mark.parametrize("kind", ["rc", "malformed", "oserror"])
def test_commit_errors_before_ref_update_are_store_errors(step, route, kind):
    out = {
        "rc": cp(json.dumps({"tree": {"sha": TREE}, "sha": NEW_TREE}), stderr="gh: boom (HTTP 500)", rc=1),
        "malformed": cp(json.dumps({"unexpected": 1})),
        "oserror": OSError("no gh"),
    }[kind]
    run = FakeRun(fail={route: out})
    with pytest.raises(StoreError):
        tree(run).commit(SHA, {"a": "1"}, "m")
    assert not [c for c in run.calls if c[0] == "PATCH"]
    # catches `_json` ignoring returncode / unguarded response parsing


def test_env_reaches_every_gh_call():
    run = FakeRun(files={"agent-todos.jsonl": ""})
    env = {"PATH": "/bin", "GH_CONFIG_DIR": "/wiki-token"}
    t = todotree.GhTree("o/r", "gh", run, env=env)
    base, _ = t.head(("agent-todos.jsonl",))
    t.commit(base, {"agent-todos.jsonl": "x\n"}, "m")
    kws = run.kws
    assert len(kws) == 6
    assert all(kw.get("env") is env for kw in kws)
    # catches: an env that reaches the reads but not the writes, or no call at all


def test_no_env_means_inherited_environment():
    run = FakeRun(files={"agent-todos.jsonl": ""})
    todotree.GhTree("o/r", "gh", run).head(("agent-todos.jsonl",))
    assert run.kws and all("env" not in kw for kw in run.kws)
    # catches: the laptop and box (no second token) getting a replaced environment
