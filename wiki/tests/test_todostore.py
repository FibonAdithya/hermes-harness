import hashlib
from dataclasses import replace
from datetime import date

import pytest

from hermes_wiki import todos, todostore
from hermes_wiki.todopage import data_marker, render
from hermes_wiki.todorecords import Record, dumps, loads
from hermes_wiki.todos import Conflict, StoreError, TodoError, WriteUncertain
from hermes_wiki.todostore import Change, NotMigrated, transact

TODAY = date(2026, 11, 19)
JSONL, PAGE, TODOS = "agent-todos.jsonl", "agent-todos.md", "todos.md"

SECTION_HEAD = ("## Agent follow-ups\n"
                "![[agent-todos]]\n"
                "Open the page to tick: [[agent-todos]]\n")
TODOS_MD = ("---\ntype: log\ntags: [people]\nupdated: 2026-11-07\n---\n\n# Todos\n\n"
            + SECTION_HEAD +
            "\n## Moving house\n- [ ] Book the van\n  - call two companies\n")


def rec(id, title=None, status="open", closed=None, created="2026-11-10"):
    return Record(id=id, title=title or f"Item {id}", details=[], project="hermes",
                  created=created, source="claude-box", status=status, closed=closed)


def files(records, page=None, todos_md=TODOS_MD):
    return {JSONL: dumps(records),
            PAGE: render(records, TODAY) if page is None else page,
            TODOS: todos_md}


class FakeTree:
    def __init__(self, heads, commits=None):
        self._heads = list(heads)
        self._commits = list(commits or [])
        self.commits = []

    def head(self, paths=None):
        return self._heads.pop(0)

    def commit(self, base, files, message):
        self.commits.append((base, dict(files), message))
        out = self._commits.pop(0) if self._commits else None
        if out is not None:
            raise out


def ids(gen_values):
    it = iter(gen_values)
    return lambda: next(it)


def run(tree, fn=lambda rs, today: None, message="todo: add 123456",
        new_id=None, sleeps=None, logs=None):
    return transact(tree, Change(message=message, fn=fn), today=TODAY, source="claude-laptop",
                    new_id=new_id or ids([]), sleep=(sleeps if sleeps is not None else []).append,
                    log=(logs if logs is not None else []).append)


def committed(tree, n=-1):
    return {r.id: r for r in loads(tree.commits[n][1][JSONL])}


def close(rs, id, by="agent"):
    for r in rs:
        if r.id == id:
            r.status = "done"
            r.closed = {"at": TODAY.isoformat(), "by": by, "reason": "fixed"}
    return rs


def tick(page, id, box="x"):
    lines = page.split("\n")
    hits = [i for i, ln in enumerate(lines) if ln.endswith(f"id {id})")]
    assert len(hits) == 1
    i = hits[0]
    lines[i] = f"- [{box}]" + lines[i][5:]
    return "\n".join(lines)


def test_owner_tick_and_agent_change_land_together():  # catches tick import skipped when the change touches another item
    rs = [rec("aaaaaa"), rec("bbbbbb")]
    page = tick(render(rs, TODAY), "aaaaaa")
    tree = FakeTree([("base1", files(rs, page))])
    assert run(tree, fn=lambda rs, today: close(rs, "bbbbbb"), message="todo: done bbbbbb") is True
    assert len(tree.commits) == 1
    got = committed(tree)
    assert got["aaaaaa"].status == "done" and got["bbbbbb"].status == "done"
    assert got["aaaaaa"].closed == {"at": "2026-11-19", "by": "owner", "reason": "ticked in Obsidian"}
    assert got["bbbbbb"].closed["by"] == "agent"
    assert tree.commits[0][0] == "base1" and tree.commits[0][2] == "todo: done bbbbbb"


def test_owner_tick_closes_suggested_item():  # catches importing ticks only for open items
    rs = [replace(rec("aaaaaa"), status="suggested",
                  suggestion={"at": "2026-11-18", "reason": "PR merged", "evidence": "x"})]
    page = tick(render(rs, TODAY), "aaaaaa")
    tree = FakeTree([("b", files(rs, page))])
    run(tree)
    assert committed(tree)["aaaaaa"].status == "done"
    assert committed(tree)["aaaaaa"].closed["by"] == "owner"


def test_owner_untick_reopens_done_item():  # catches one-way import (ticks close, unticks ignored)
    rs = [rec("aaaaaa", status="done", closed={"at": "2026-11-18", "by": "agent", "reason": "r"})]
    page = tick(render(rs, TODAY), "aaaaaa", box=" ")
    assert data_marker(page) == hashlib.sha256(dumps(rs).encode("utf-8")).hexdigest()
    logs = []
    tree = FakeTree([("b", files(rs, page))])
    assert run(tree, logs=logs) is False
    got = committed(tree)["aaaaaa"]
    assert got.status == "open" and got.closed is None
    assert tree.commits[0][2] == "todo: import owner edits"
    assert not any("stale" in m for m in logs)


def test_stale_page_does_not_reopen():  # catches an old Obsidian copy reopening every item agents closed since
    old = [rec("aaaaaa"), rec("cccccc")]
    stale = tick(render(old, TODAY), "cccccc")  # owner ticked cccccc on an old copy
    now = close([rec("aaaaaa"), rec("cccccc")], "aaaaaa")  # agent closed aaaaaa since
    logs = []
    tree = FakeTree([("b", files(now, stale))])
    run(tree, logs=logs)
    got = committed(tree)
    assert got["aaaaaa"].status == "done" and got["aaaaaa"].closed["by"] == "agent"
    assert got["cccccc"].status == "done" and got["cccccc"].closed["by"] == "owner"
    assert "agent-todos.md was stale; unticks ignored" in logs


def test_page_without_marker_counts_as_stale():  # catches treating a missing marker as fresh
    rs = [rec("aaaaaa", status="done", closed={"at": "2026-11-18", "by": "agent", "reason": "r"})]
    page = tick(render(rs, TODAY), "aaaaaa", box=" ")
    page = "\n".join(ln for ln in page.split("\n") if not ln.startswith("<!-- data:"))
    assert data_marker(page) is None
    tree = FakeTree([("b", files(rs, page))])
    run(tree)
    assert all(JSONL not in c[1] for c in tree.commits)  # nothing reopened


def test_unknown_page_ids_ignored():  # catches creating records from page ticks
    rs = [rec("aaaaaa")]
    page = render(rs, TODAY) + "- [x] Ghost (2026-11-01, id ffffff)\n"
    tree = FakeTree([("b", files(rs, page))])
    run(tree)
    assert all(JSONL not in c[1] for c in tree.commits)


def test_page_and_jsonl_always_committed_together():  # catches writing the page separately or rendering pre-change records
    rs = [rec("aaaaaa"), rec("bbbbbb")]
    stray = TODOS_MD.replace(SECTION_HEAD, SECTION_HEAD + "- [ ] New (2026-11-12, claude-box, id 4f6cc0)\n")
    scenarios = [
        FakeTree([("b", files(rs))]),
        FakeTree([("b", files(rs, tick(render(rs, TODAY), "aaaaaa")))]),
        FakeTree([("b", files(rs, todos_md=stray))]),
    ]
    run(scenarios[0], fn=lambda rs, today: close(rs, "bbbbbb"))
    run(scenarios[1])
    run(scenarios[2])
    for tree in scenarios:
        assert tree.commits
        for _, fs, _ in tree.commits:
            assert JSONL in fs and PAGE in fs
            assert data_marker(fs[PAGE]) == hashlib.sha256(fs[JSONL].encode("utf-8")).hexdigest()


def test_stray_item_imported_and_removed_from_todos_md():  # catches dropping the embed or the owner's sections
    rs = [rec("aaaaaa")]
    md = TODOS_MD.replace(SECTION_HEAD, SECTION_HEAD
                          + "- [ ] Fix the thing (2026-11-12, claude-box, id 4f6cc0)\n"
                          + "  - see PR 12\n"
                          + "- [x] Typed by hand\n")
    logs = []
    tree = FakeTree([("b", files(rs, todos_md=md))])
    assert run(tree, new_id=ids(["aaaaaa", "dddddd"]), logs=logs) is False
    base, fs, msg = tree.commits[0]
    assert fs[TODOS] == TODOS_MD
    got = committed(tree)
    assert got["4f6cc0"] == Record(id="4f6cc0", title="Fix the thing", details=["see PR 12"],
                                   project="unsorted", created="2026-11-12", source="claude-box")
    hand = got["dddddd"]
    assert (hand.title, hand.created, hand.source, hand.status) == (
        "Typed by hand", "2026-11-19", "claude-laptop", "done")
    assert hand.closed == {"at": "2026-11-19", "by": "owner", "reason": "ticked in todos.md"}
    assert msg == "todo: import owner edits"
    assert any("4f6cc0" in m for m in logs) and any("dddddd" in m for m in logs)


def test_stray_id_clash_gets_fresh_id():  # catches silently dropping a stray whose id is taken by another title
    rs = [rec("aaaaaa", title="Existing")]
    md = TODOS_MD.replace(SECTION_HEAD, SECTION_HEAD + "- [ ] Other (2026-11-12, claude-box, id aaaaaa)\n")
    tree = FakeTree([("b", files(rs, todos_md=md))])
    run(tree, new_id=ids(["aaaaaa", "dddddd"]))
    got = committed(tree)
    assert sorted(got) == ["aaaaaa", "dddddd"]
    assert got["aaaaaa"].title == "Existing" and got["dddddd"].title == "Other"


def test_stray_same_id_and_title_dropped():  # catches double import after a partial failure
    rs = [rec("aaaaaa", title="Existing")]
    md = TODOS_MD.replace(SECTION_HEAD, SECTION_HEAD + "- [ ] Existing (2026-11-12, claude-box, id aaaaaa)\n")
    tree = FakeTree([("b", files(rs, todos_md=md))])
    run(tree)
    assert set(tree.commits[0][1]) == {TODOS}
    assert tree.commits[0][1][TODOS] == TODOS_MD


def test_stray_with_empty_title_imported_as_untitled():  # catches dropping an empty stray
    md = TODOS_MD.replace(SECTION_HEAD, SECTION_HEAD + "- [ ] \n")
    tree = FakeTree([("b", files([], todos_md=md))])
    run(tree, new_id=ids(["dddddd"]))
    assert committed(tree)["dddddd"].title == "(untitled)"


def test_noop_makes_no_commit():  # catches empty commits nightly
    rs = [rec("aaaaaa"), rec("bbbbbb", status="done",
                              closed={"at": "2026-11-18", "by": "agent", "reason": "r"})]
    tree = FakeTree([("b", files(rs))])
    assert run(tree) is False
    assert tree.commits == []


def test_conflict_rereads_and_reapplies():  # catches retrying stale state or reusing the first attempt's records
    v1 = [rec("aaaaaa")]
    v2 = [rec("aaaaaa"), rec("eeeeee")]
    tree = FakeTree([("base1", files(v1)), ("base2", files(v2))], commits=[Conflict(), None])
    seen, sleeps = [], []

    def fn(rs, today):
        seen.append(rs)
        return rs + [rec("bbbbbb")]

    assert run(tree, fn=fn, sleeps=sleeps) is True
    assert [c[0] for c in tree.commits] == ["base1", "base2"]
    assert sorted(committed(tree)) == ["aaaaaa", "bbbbbb", "eeeeee"]
    assert seen[0] is not seen[1] and [r.id for r in seen[1]] == ["aaaaaa", "eeeeee"]
    assert len(sleeps) == 1 and 0.5 <= sleeps[0] <= 1.0


def test_five_conflicts_give_up():  # catches off-by-one on attempts
    rs = [rec("aaaaaa")]
    tree = FakeTree([("b", files(rs))] * 5, commits=[Conflict()] * 5)
    sleeps = []
    with pytest.raises(TodoError) as e:
        run(tree, fn=lambda rs, today: close(rs, "aaaaaa"), sleeps=sleeps)
    assert len(tree.commits) == 5 and len(sleeps) == 4 and e.value.code == 1
    assert str(e.value) == "agent-todos kept changing; gave up after 5 attempts"


def test_not_migrated_raises():  # catches creating the jsonl implicitly
    tree = FakeTree([("b", {JSONL: None, PAGE: None, TODOS: TODOS_MD})])
    with pytest.raises(NotMigrated) as e:
        run(tree, fn=lambda rs, today: rs + [rec("bbbbbb")])
    assert isinstance(e.value, TodoError) and e.value.code == 1 and str(e.value) == "not migrated"
    assert tree.commits == []


def test_write_uncertain_names_id_and_is_not_retried():  # catches retrying a write that may have landed
    rs = [rec("aaaaaa")]
    tree = FakeTree([("b", files(rs))] * 2, commits=[WriteUncertain()])
    with pytest.raises(TodoError) as e:
        run(tree, fn=lambda rs, today: close(rs, "aaaaaa"), message="todo: done aaaaaa")
    assert str(e.value) == "timed out writing agent-todos; item aaaaaa may or may not have landed"
    assert e.value.code == 1 and len(tree.commits) == 1


@pytest.mark.parametrize("where", ["head", "commit"])
def test_store_error_becomes_github_error(where):  # catches StoreError escaping as a traceback
    rs = [rec("aaaaaa")]

    class Broken(FakeTree):
        def head(self, paths=None):
            if where == "head":
                raise StoreError("HTTP 500")
            return super().head(paths)

    tree = Broken([("b", files(rs))], commits=[StoreError("HTTP 500")])
    with pytest.raises(TodoError) as e:
        run(tree, fn=lambda rs, today: close(rs, "aaaaaa"))
    assert str(e.value) == "GitHub: HTTP 500" and e.value.code == 1


def test_absent_page_is_written_with_the_change():  # catches crashing when agent-todos.md is missing
    rs = [rec("aaaaaa")]
    tree = FakeTree([("b", {JSONL: dumps(rs), PAGE: None, TODOS: TODOS_MD})])
    assert run(tree, fn=lambda rs, today: close(rs, "aaaaaa")) is True
    assert set(tree.commits[0][1]) == {JSONL, PAGE}


def test_page_only_difference_without_change_makes_no_commit():  # catches a page-refresh commit on every read-only run
    rs = [rec("aaaaaa")]
    tree = FakeTree([("b", files(rs, page=render(rs, TODAY) + "owner scribble\n"))])
    assert run(tree) is False and tree.commits == []


# take_section_items

def test_take_section_items_keeps_non_items_and_outside_bytes():  # catches removing embed/link lines or other sections
    md = TODOS_MD.replace(SECTION_HEAD, SECTION_HEAD
                          + "- [ ] One (2026-11-12, claude-box, id 111111)\n"
                          + "  - d1\n"
                          + "  - - [x] looks like an item (2026-11-12, x, id 222222)\n"
                          + "- [X] Two\n")
    items, rest = todos.take_section_items(md)
    assert rest == TODOS_MD
    assert items == [
        (todos.Item("111111", "One", False, created="2026-11-12", source="claude-box"),
         ["d1", "- [x] looks like an item (2026-11-12, x, id 222222)"]),
        (todos.Item(None, "Two", True), []),
    ]


def test_take_section_items_without_section_is_identity():  # catches taking items from the owner's other sections
    assert todos.take_section_items(TODOS_MD.replace(SECTION_HEAD, "")) == (
        [], TODOS_MD.replace(SECTION_HEAD, ""))
