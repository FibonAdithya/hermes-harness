import base64
import inspect
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from hermes_wiki import config as cf
from hermes_wiki import notes as nt
from hermes_wiki.frontmatter import parse
from wikifixture import build_wiki

TODAY = date(2026, 10, 26)


@pytest.fixture
def cfg(tmp_path):
    root = build_wiki(tmp_path / "wiki")
    return cf.WikiConfig(root=root, source="claude-laptop")


class FakePublisher:
    def __init__(self):
        self.calls = []

    def __call__(self, repo, path, content, **kw):
        self.calls.append((repo, path, content))


def test_note_path_shape():
    assert (
        nt.note_path("claude-box", "gotcha", date(2026, 10, 26), "0a1b2c3d")
        == "inbox/claude-box/2026-10-26-gotcha-0a1b2c3d.md"
    )


def test_add_note_publishes_valid_note(cfg):
    pub = FakePublisher()
    path = nt.add_note(
        cfg, kind="fact", body="Deploys need approval.\n", tags=["hermes"],
        session="s1", today=TODAY, rand="0a1b2c3d", publisher=pub,
    )
    assert path == "inbox/claude-laptop/2026-10-26-fact-0a1b2c3d.md"
    assert len(pub.calls) == 1
    repo, p, content = pub.calls[0]
    assert repo == cfg.repo and p == path
    meta, body = parse(content)
    assert meta["source"] == cfg.source
    assert meta["session"] == "s1"
    assert meta["created"] == "2026-10-26"
    assert list(meta)[:3] == ["kind", "tags", "repos"]
    assert body == "Deploys need approval.\n"


def test_review_by_only_when_given(cfg):
    pub = FakePublisher()
    nt.add_note(cfg, kind="fact", body="x y", tags=[], today=TODAY, rand="0a1b2c3d", publisher=pub)
    assert "review_by" not in parse(pub.calls[0][2])[0]
    nt.add_note(cfg, kind="fact", body="x y", tags=[], review_by="2026-12-01",
                today=TODAY, rand="0a1b2c3e", publisher=pub)
    assert parse(pub.calls[1][2])[0]["review_by"] == "2026-12-01"


def test_source_comes_from_config_not_caller(cfg):
    assert "source" not in inspect.signature(nt.add_note).parameters
    pub = FakePublisher()
    nt.add_note(cfg, kind="fact", body="a b", tags=[], today=TODAY, rand="0a1b2c3d", publisher=pub)
    assert parse(pub.calls[0][2])[0]["source"] == cfg.source


@pytest.mark.parametrize(
    "tags,body",
    [(["nope"], "fine body"), (["hermes"], "token ghp_" + "a" * 36)],
)
def test_add_note_rejects_without_publishing(cfg, tags, body):
    pub = FakePublisher()
    with pytest.raises(nt.NoteRejected) as ei:
        nt.add_note(cfg, kind="fact", body=body, tags=tags, today=TODAY, rand="0a1b2c3d", publisher=pub)
    assert ei.value.errors
    assert pub.calls == []


def test_publish_builds_gh_call():
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, "", "")

    nt.publish("FibonAdithya/hermes-wiki", "inbox/a.md", "é\n", run=run)
    argv = seen["argv"]
    assert argv[:5] == ["gh", "api", "-X", "PUT", "repos/FibonAdithya/hermes-wiki/contents/inbox/a.md"]
    expected = base64.b64encode("é\n".encode("utf-8")).decode()
    assert f"content={expected}" in argv
    assert "branch=main" in argv
    assert seen["kw"]["timeout"] == 30


def test_publish_uses_configured_gh():
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    nt.publish("o/r", "inbox/a.md", "x", run=run, gh="/opt/gh")
    assert seen["argv"][0] == "/opt/gh"


def test_add_note_reports_publish_failure():
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "could not resolve host")

    with pytest.raises(nt.PublishError, match="could not resolve host"):
        nt.publish("o/r", "inbox/a.md", "x", run=run)


def test_publish_timeout():
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    with pytest.raises(nt.PublishError, match="timeout"):
        nt.publish("o/r", "inbox/a.md", "x", run=run)


def test_config_load_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"root": "/w", "source": "claude-box"}))
    c = cf.load(p)
    assert c.repo == "FibonAdithya/hermes-wiki"
    assert c.public_only is False
    assert c.root == Path("/w")
    assert c.path == p
    assert c.state_dir == Path("~/.local/state/hermes-wiki").expanduser()


def test_config_load_expands_tilde(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"root": "~/w", "source": "claude-box", "state_dir": "~/s", "pull": True}))
    c = cf.load(p)
    assert c.root == Path("~/w").expanduser()
    assert c.state_dir == Path("~/s").expanduser()
    assert c.pull is True


@pytest.mark.parametrize(
    "kw",
    [
        {"session": "s1\nsource: claude-box"},
        {"verified": "claim\r\ntags: [x]"},
        {"rand": "../../x"},
        {"session": "s1\x0csource: claude-box"},
        {"verified": "claim\x85tags: [x]"},
        {"session": "s1\u2028source: claude-box"},
        {"verified": "claim\u2029source: claude-box"},
        {"session": "s1\x1csource: claude-box"},
        {"session": 5},
        {"verified": ["a"]},
        {"session": "key ghp_" + "a" * 36},
        {"verified": "key ghp_" + "a" * 36},
    ],
)
def test_add_note_rejects_header_injection_and_bad_rand(cfg, kw):
    pub = FakePublisher()
    args = {"today": TODAY, "rand": "0a1b2c3d", **kw}
    with pytest.raises(nt.NoteRejected):
        nt.add_note(cfg, kind="fact", body="a b", tags=[], publisher=pub, **args)
    assert pub.calls == []


def test_add_note_rejects_long_body_and_meta(cfg):
    pub = FakePublisher()
    nt.add_note(cfg, kind="fact", body="x" * 4000, tags=["hermes"], verified="v" * 500,
                session="s" * 500, today=TODAY, rand="0a1b2c3d", publisher=pub)
    assert len(pub.calls) == 1
    for kw in ({"body": "x" * 4001}, {"verified": "v" * 501}, {"session": "s" * 501}):
        args = {"body": "short body", **kw}
        with pytest.raises(nt.NoteRejected):
            nt.add_note(cfg, kind="fact", tags=["hermes"], today=TODAY, rand="0a1b2c3d",
                        publisher=pub, **args)
    assert len(pub.calls) == 1


def test_publish_missing_gh_is_a_publish_error():
    def run(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    with pytest.raises(nt.PublishError, match="could not run"):
        nt.publish("o/r", "inbox/a.md", "x", run=run, gh="/nope/gh")


def test_publish_failure_without_stderr_names_exit_code():
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 4, "", "")

    with pytest.raises(nt.PublishError, match="exited 4") as ei:
        nt.publish("o/r", "inbox/a.md", "x", run=run)
    assert "timeout" not in str(ei.value)


# Two agents publishing at the same moment: GitHub rejects the second PUT because
# main moved under it (seen in acceptance check 1, 2026-10-28). No commit was made,
# so sending the same PUT again is safe.
_CONFLICT = "gh: is at 0a47fd8b487c0ccee84537be9f4c4a954fd34997 but expected 981b0fb87164ad46da86eb4ffebce1cc31994681 (HTTP 409)\n"


class ScriptedRun:
    def __init__(self, *results):
        self.results = list(results)
        self.argvs = []

    def __call__(self, argv, **kw):
        self.argvs.append(argv)
        rc, err = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, "", err)


def test_publish_retries_a_409_branch_conflict():
    run = ScriptedRun((1, _CONFLICT), (1, _CONFLICT), (0, ""))
    sleeps = []
    nt.publish("o/r", "inbox/a.md", "x", run=run, sleep=sleeps.append)
    assert len(run.argvs) == 3
    assert run.argvs[0] == run.argvs[1] == run.argvs[2]
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_publish_gives_up_after_bounded_409_retries():
    run = ScriptedRun(*[(1, _CONFLICT)] * 10)
    sleeps = []
    with pytest.raises(nt.PublishError, match="HTTP 409"):
        nt.publish("o/r", "inbox/a.md", "x", run=run, sleep=sleeps.append)
    assert len(run.argvs) == nt.PUBLISH_ATTEMPTS
    assert 1 < nt.PUBLISH_ATTEMPTS < 10
    assert len(sleeps) == nt.PUBLISH_ATTEMPTS - 1


@pytest.mark.parametrize("err", ["gh: Invalid request. \"sha\" wasn't supplied. (HTTP 422)\n",
                                 "could not resolve host\n"])
def test_publish_does_not_retry_other_errors(err):
    run = ScriptedRun((1, err), (0, ""))
    sleeps = []
    with pytest.raises(nt.PublishError):
        nt.publish("o/r", "inbox/a.md", "x", run=run, sleep=sleeps.append)
    assert len(run.argvs) == 1 and sleeps == []
