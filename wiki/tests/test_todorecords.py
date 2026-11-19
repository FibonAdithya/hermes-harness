import pytest

from hermes_wiki import todorecords as tr
from hermes_wiki.todos import TodoError, TodoRejected


def rec(i, **kw):
    base = dict(id=i, title=f"t{i}", details=["d"], project="chain",
                created="2026-11-19", source="claude-box")
    base.update(kw)
    return tr.Record(**base)


def test_roundtrip_sorted_by_id():
    a = rec("aaaaaa", done_when=[{"pr": "a/b#1", "state": "merged"}],
            closed={"at": "2026-11-19", "by": "owner"}, status="done")
    b = rec("bbbbbb", title="café")
    out = tr.dumps([b, a])
    assert out.endswith("\n")
    assert out.splitlines()[0].startswith('{"closed"')
    assert '"id": "aaaaaa"' in out.splitlines()[0]
    assert "café" in out
    assert tr.loads(out) == [a, b]
    # catches unsorted output, lost fields, ensure_ascii escaping


def test_dumps_empty_is_empty_string():
    assert tr.dumps([]) == ""
    assert tr.loads("") == []
    # catches returning a bare newline for no records


def test_loads_ignores_blank_lines():
    text = "\n" + tr.dumps([rec("aaaaaa")]) + "\n\n"
    assert [r.id for r in tr.loads(text)] == ["aaaaaa"]
    # catches blank lines raising


def test_loads_reports_line_number():
    text = tr.dumps([rec("aaaaaa")]) + "{bad\n"
    with pytest.raises(TodoError, match="line 2"):
        tr.loads(text)
    # catches swallowing bad lines


def test_loads_rejects_bad_fields_with_line_number():
    good = tr.dumps([rec("aaaaaa")])
    for bad in ['{"id": "bbbbbb"}',
                tr.dumps([rec("bbbbbb", status="weird")]),
                tr.dumps([rec("bbbbbb", details="notalist")]),
                "[1]"]:
        with pytest.raises(TodoError, match="line 2"):
            tr.loads(good + bad)
    # catches accepting bad status, wrong field types, missing fields


def test_default_done_when_not_shared():
    a, b = rec("aaaaaa"), rec("bbbbbb")
    a.done_when.append({"pr": "a/b#1", "state": "merged"})
    assert b.done_when == []
    # catches a shared mutable default list


def test_validate_check_forms():
    _, _, checks = tr.validate_item("t", [], "chain",
                                    ["pr:FibonAdithya/chain#4", "issue:a/b#1"])
    assert checks == [{"pr": "FibonAdithya/chain#4", "state": "merged"},
                      {"issue": "a/b#1", "state": "closed"}]
    for bad in ["issue:a/b#0", "pr:a#1", "pr:a/b#x", "x:a/b#1"]:
        with pytest.raises(TodoRejected):
            tr.validate_item("t", [], "chain", [bad])
    # catches accepting N=0 or repo without owner


def test_validate_project_regex():
    for ok in ["chain", "hermes-harness", "a.b_c", "a" * 40]:
        tr.validate_item("t", [], ok, [])
    for bad in ["Chain", "-x", "a" * 41, ""]:
        with pytest.raises(TodoRejected):
            tr.validate_item("t", [], bad, [])
    # catches off-by-one on 40


def test_validate_limits_match_existing():
    tr.validate_item("a" * 120, ["b" * 300] * 15, "chain", [])
    for title, details in [("a" * 121, []), ("t", ["d"] * 16), ("t", ["b" * 301]),
                           ("t", ["ghp_" + "a" * 36]), ("t", ["x\ny"])]:
        with pytest.raises(TodoRejected):
            tr.validate_item(title, details, "chain", [])
    # catches boundary drift from todos.validate


def test_validate_strips_and_collects_all_errors():
    t, d, _ = tr.validate_item("  t  ", [" d "], "chain", [])
    assert (t, d) == ("t", ["d"])
    with pytest.raises(TodoRejected) as e:
        tr.validate_item("", [], "Bad", ["pr:a#1"])
    assert len(e.value.errors) == 3
    # catches stopping at the first error or skipping the strip


def test_new_id():
    ids = iter(["aaaaaa", "bbbbbb"])
    assert tr.new_id({"aaaaaa"}, lambda: next(ids)) == "bbbbbb"
    with pytest.raises(TodoError):
        tr.new_id({"aaaaaa"}, lambda: "aaaaaa")
    # catches returning a used id or looping forever


def test_project_from_origin():
    assert tr.project_from_origin("git@github.com:O/Chain.git") == "chain"
    assert tr.project_from_origin("https://github.com/O/chain") == "chain"
    for u in [None, "file:///x", "https://gitlab.com/a/b", ""]:
        assert tr.project_from_origin(u) == "unsorted"
    # catches returning owner or .git suffix


@pytest.mark.parametrize("kw", [
    dict(status="done"),
    dict(status="done", closed={"by": "owner"}),
    dict(status="done", closed={"at": "2026-13-40", "by": "owner"}),
    dict(status="done", closed={"at": 20261119, "by": "owner"}),
    dict(status="done", closed={"at": "2026-11-19", "by": "someone"}),
    dict(status="done", closed={"at": "2026-11-19"}),
    dict(status="suggested"),
    dict(status="suggested", suggestion={"reason": "r"}),
    dict(status="suggested", suggestion={"at": "yesterday"}),
    dict(status="suggested", suggestion={"at": None}),
])
def test_loads_rejects_done_without_valid_closed_and_suggested_without_valid_suggestion(kw):
    good = tr.dumps([rec("aaaaaa")])
    with pytest.raises(TodoError, match="line 2"):
        tr.loads(good + tr.dumps([rec("bbbbbb", **kw)]))
    # catches loads accepting a done/suggested record whose closed/suggestion the nightly cannot read


def test_loads_accepts_valid_closed_and_suggestion():
    rs = [rec("aaaaaa", status="done", closed={"at": "2026-11-19", "by": by, "reason": "r"})
          for by in ("check", "agent", "owner")]
    rs = [tr.Record(**{**r.__dict__, "id": f"aaaaa{n}"}) for n, r in enumerate(rs)]
    rs.append(rec("bbbbbb", status="suggested", suggestion={"at": "2026-11-18", "evidence": "e"}))
    rs.append(rec("cccccc", suggestion={"at": "2026-11-01", "rejected": True}))
    assert tr.loads(tr.dumps(rs)) == rs
    # catches over-strict validation refusing a check/agent/owner close or an open item's old suggestion
