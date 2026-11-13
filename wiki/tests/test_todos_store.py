import base64
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from hermes_wiki import todos
from hermes_wiki.config import WikiConfig

FIX = Path(__file__).parent / "fixtures" / "todos.md"
TODAY = date(2026, 11, 13)


def fix() -> str:
    return FIX.read_text(encoding="utf-8")


class FakeStore:
    def __init__(self, gets, puts):
        self._gets = list(gets)
        self._puts = list(puts)
        self.gets = []
        self.puts = []

    def get(self, path):
        self.gets.append(path)
        return self._gets.pop(0)

    def put(self, path, text, sha, message):
        self.puts.append((text, sha, message))
        out = self._puts.pop(0)
        if out is not None:
            raise out


@pytest.fixture
def cfg(tmp_path):
    return WikiConfig(root=tmp_path, source="claude-box", state_dir=tmp_path / "state")


@pytest.fixture
def sleeps():
    return []


def one_item(state="[ ]"):
    return fix().replace("# Todos\n\n", f"# Todos\n\n## Agent follow-ups\n- {state} One (2026-11-12, claude-box, id 111111)\n\n", 1)


def test_conflict_regets_and_applies_edit_to_new_content(cfg, sleeps):  # spec 5; catches retrying the stale body, no retry
    v1 = fix(); v2 = v1.replace("- [ ] Buy a desk lamp", "- [x] Buy a desk lamp")
    st = FakeStore(gets=[(v1, "sha1"), (v2, "sha2")], puts=[todos.Conflict(), None])
    todos.cmd_add(cfg, st, "T", [], today=TODAY, new_id=lambda: "abcdef", sleep=sleeps.append)
    assert [p[1] for p in st.puts] == ["sha1", "sha2"]
    assert "- [x] Buy a desk lamp" in st.puts[1][0] and "id abcdef)" in st.puts[1][0]
    assert st.puts[1][2] == "todo: add abcdef" and len(sleeps) == 1


def test_five_conflicts_give_up(cfg, sleeps):  # off-by-one on ATTEMPTS
    st = FakeStore(gets=[(fix(), f"s{n}") for n in range(5)], puts=[todos.Conflict()] * 5)
    with pytest.raises(todos.TodoError) as e:
        todos.cmd_add(cfg, st, "T", [], sleep=sleeps.append)
    assert e.value.code == 1 and len(st.puts) == 5 and len(sleeps) == 4


def test_put_timeout_not_retried(cfg, sleeps):  # spec 6; catches blind retry
    st = FakeStore(gets=[(fix(), "s1")] * 2, puts=[todos.WriteUncertain(), None])
    with pytest.raises(todos.TodoError, match="may or may not") as e:
        todos.cmd_add(cfg, st, "T", [], sleep=sleeps.append, new_id=lambda: "abcdef")
    assert e.value.code == 1 and len(st.puts) == 1
    assert "item abcdef may or may not" in str(e.value)


def test_done_put_timeout_names_the_id(cfg, sleeps):
    st = FakeStore(gets=[(one_item(), "s1")], puts=[todos.WriteUncertain()])
    with pytest.raises(todos.TodoError) as e:
        todos.cmd_done(cfg, st, "111111", sleep=sleeps.append)
    assert e.value.code == 1 and "item 111111 may or may not" in str(e.value)


def test_get_store_error_is_exit_1_for_list_and_add(cfg, sleeps):  # other GitHub failure -> 1
    class RaisingGet(FakeStore):
        def get(self, path):
            raise todos.StoreError("boom")

    for call in (lambda: todos.cmd_list(cfg, RaisingGet([], []), False),
                 lambda: todos.cmd_add(cfg, RaisingGet([], []), "T", [], sleep=sleeps.append)):
        with pytest.raises(todos.TodoError, match="^GitHub: boom") as e:
            call()
        assert e.value.code == 1


def test_put_store_error_is_exit_1(cfg, sleeps):
    st = FakeStore(gets=[(fix(), "s1")], puts=[todos.StoreError("boom")])
    with pytest.raises(todos.TodoError, match="^GitHub: boom") as e:
        todos.cmd_add(cfg, st, "T", [], sleep=sleeps.append)
    assert e.value.code == 1


def test_secret_title_makes_no_store_call(cfg, sleeps):  # spec 7; catches scan after write / scan skipped
    st = FakeStore(gets=[], puts=[])
    with pytest.raises(todos.TodoRejected):
        todos.cmd_add(cfg, st, "key sk-" + "a" * 24, [], sleep=sleeps.append)
    assert st.gets == [] and st.puts == []


def test_done_unknown_id_and_duplicate_heading_make_no_put(cfg, sleeps):  # spec 9, 10 through the loop
    st = FakeStore(gets=[(one_item(), "s1")], puts=[])
    with pytest.raises(todos.TodoError, match="999999"):
        todos.cmd_done(cfg, st, "999999", sleep=sleeps.append)
    assert st.puts == []
    dup = one_item() + "\n## Agent follow-ups\n- [ ] Two (2026-11-12, claude-box, id 222222)\n"
    st = FakeStore(gets=[(dup, "s1")], puts=[])
    with pytest.raises(todos.TodoError, match="duplicate heading"):
        todos.cmd_done(cfg, st, "111111", sleep=sleeps.append)
    assert st.puts == []


def test_done_already_ticked_makes_no_put(cfg, sleeps):
    st = FakeStore(gets=[(one_item("[x]"), "s1")], puts=[])
    assert todos.cmd_done(cfg, st, "111111", sleep=sleeps.append) is False
    assert st.puts == []


def test_done_puts_tick_with_message(cfg, sleeps):
    text = one_item()
    st = FakeStore(gets=[(text, "shaX")], puts=[None])
    assert todos.cmd_done(cfg, st, "111111", sleep=sleeps.append) is True
    assert st.puts[0][0] == text.replace("- [ ] One", "- [x] One", 1)
    assert st.puts[0][0].count("- [x]") == text.count("- [x]") + 1
    assert st.puts[0][1] == "shaX"
    assert st.puts[0][2] == "todo: done 111111"


def test_missing_file_creates_nothing(cfg, sleeps):
    st = FakeStore(gets=[None], puts=[])
    with pytest.raises(todos.TodoError) as e:
        todos.cmd_add(cfg, st, "T", [], sleep=sleeps.append)
    assert e.value.code == 1 and st.puts == []


def test_list_public_only_makes_no_store_call(tmp_path):  # spec 13 (module half)
    cfg = WikiConfig(root=tmp_path, source="x", state_dir=tmp_path / "state", public_only=True)
    st = FakeStore(gets=[], puts=[])
    with pytest.raises(todos.TodoError) as e:
        todos.cmd_list(cfg, st, False)
    assert e.value.code == 2 and st.gets == []


def test_gh_get_decodes_wrapped_base64():  # Review Focus 3
    body = fix()
    out = json.dumps({"content": base64.encodebytes(body.encode()).decode(), "sha": "abc"})
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, "")
    assert todos.GhContents("o/r", "gh", run).get("todos.md") == (body, "abc")


def test_gh_get_404_is_none_and_argv():
    seen = []

    def run404(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
    assert todos.GhContents("o/r", "gh", run404).get("todos.md") is None
    assert seen[0] == ["gh", "api", "repos/o/r/contents/todos.md?ref=main"]

    def run500(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "boom (HTTP 500)\nmore\n")
    with pytest.raises(todos.StoreError) as e:
        todos.GhContents("o/r", "gh", run500).get("todos.md")
    assert "more" not in str(e.value)

    def runos(argv, **kw):
        raise OSError("no gh")
    with pytest.raises(todos.StoreError):
        todos.GhContents("o/r", "gh", runos).get("todos.md")


def test_gh_put_argv_and_errors():
    seen = []

    def ok(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    todos.GhContents("o/r", "gh", ok).put("todos.md", "hi\n", "SHA1", "todo: add abcdef")
    argv = seen[0]
    assert argv[:5] == ["gh", "api", "-X", "PUT", "repos/o/r/contents/todos.md"]
    assert "sha=SHA1" in argv and "branch=main" in argv and "message=todo: add abcdef" in argv
    assert "content=" + base64.b64encode(b"hi\n").decode() in argv

    def r409(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "conflict (HTTP 409)")
    with pytest.raises(todos.Conflict):
        todos.GhContents("o/r", "gh", r409).put("todos.md", "x", "s", "m")

    def rto(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)
    with pytest.raises(todos.WriteUncertain):
        todos.GhContents("o/r", "gh", rto).put("todos.md", "x", "s", "m")

    def r1(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "nope (HTTP 422)")
    with pytest.raises(todos.StoreError):
        todos.GhContents("o/r", "gh", r1).put("todos.md", "x", "s", "m")

    def ros(argv, **kw):
        raise OSError("no gh")
    with pytest.raises(todos.StoreError):
        todos.GhContents("o/r", "gh", ros).put("todos.md", "x", "s", "m")


def test_dry_store_round_trip_and_stale_sha(tmp_path):
    st = todos.DryContents(tmp_path / "d")
    assert st.get("todos.md") is None
    st.put("todos.md", "one\n", "ignored-when-absent", "m")
    text, sha = st.get("todos.md")
    assert text == "one\n"
    st.put("todos.md", "two\n", sha, "m")
    assert st.get("todos.md")[0] == "two\n"
    with pytest.raises(todos.Conflict):
        st.put("todos.md", "three\n", sha, "m")


def test_store_for_honours_dry_env(cfg, monkeypatch):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    s = todos.store_for(cfg, subprocess.run)
    assert isinstance(s, todos.DryContents) and s.dir == cfg.state_dir / "dry"
    monkeypatch.delenv("HERMES_WIKI_DRY_PUBLISH")
    assert isinstance(todos.store_for(cfg, subprocess.run), todos.GhContents)
