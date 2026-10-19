import json
import sqlite3
import time

import pytest

from factory import make_db
from hermes_tuner.history import History, HistoryUnavailable

G = "mcp__google__get_gmail_message_content"


def _sess(sid, calls=(), **kw):
    return {"id": sid, "source": "cron", "started_at": 1000.0, "cost": 0.1,
            "in": 1, "cache": 0, "out": 1, "calls": list(calls), **kw}


def test_job_id_parsed_from_cron_session_id(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("cron_3bc4e11d29b1_20260901_080000"),
                                    _sess("20260901_080000_ab12cd34")])
    by_id = {s.id: s for s in History.open(db).sessions(0)}
    assert by_id["cron_3bc4e11d29b1_20260901_080000"].job_id == "3bc4e11d29b1"
    assert by_id["20260901_080000_ab12cd34"].job_id is None


def test_calls_carry_names_keys_ref_and_lengths(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a", [(G, {"message_id": "m1", "body_format": "text"}, "x" * 500)])])
    (call,) = History.open(db).sessions(0)[0].calls
    assert call.name == G
    assert call.arg_keys == ("body_format", "message_id")
    assert call.ref == "m1"
    assert call.result_chars == 500


def test_prompt_tokens_include_cache_reads(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a", **{"in": 100, "cache": 900})])
    assert History.open(db).sessions(0)[0].prompt_tokens == 1000


def test_untrusted_texts_excludes_skill_view_and_wiki_reads(tmp_path):
    calls = [
        ("skill_view", {"name": "x"}, "T-skill"),
        ("tool_describe", {"name": "x"}, "T-describe"),
        ("read_file", {"path": "/root/hermes-wiki/a.md"}, "T-wiki"),
        (G, {"message_id": "m"}, "U-gmail"),
        ("web_extract", {"url": "u"}, "U-web"),
        ("read_file", {"path": "/root/repos/x"}, "U-file"),
    ]
    db = make_db(tmp_path / "s.db", [_sess("a", calls)])
    got = set(History.open(db).untrusted_texts(0))
    assert got == {"U-gmail", "U-web", "U-file"}


def test_open_is_read_only(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a")])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        History.open(db)._conn.execute("insert into sessions(id, source, started_at) values ('x', 'cli', 0)")


def test_busy_database_raises_history_unavailable(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a")])
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("pragma journal_mode=DELETE")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        with pytest.raises(HistoryUnavailable):
            History.open(db).sessions(0)
        assert time.monotonic() - t0 < 10
    finally:
        locker.execute("ROLLBACK")
        locker.close()


def _bridged_db(tmp_path, inner_args):
    db = make_db(tmp_path / "s.db", [_sess("a")])
    conn = sqlite3.connect(db)
    tc = [{"id": "b1", "function": {"name": "tool_call", "arguments": json.dumps(
        {"name": G, "arguments": inner_args})}}]
    conn.execute("insert into messages(session_id, role, tool_calls, timestamp) values ('a','assistant',?,1)",
                 (json.dumps(tc),))
    conn.execute("insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp) "
                 "values ('a','tool','hello','b1',?,1)", (G,))
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("inner", [{"message_id": "m9"}, json.dumps({"message_id": "m9"})])
def test_tool_search_bridge_calls_are_unwrapped(tmp_path, inner):
    db = _bridged_db(tmp_path, inner)
    h = History.open(db)
    (call,) = h.sessions(0)[0].calls
    assert (call.name, call.arg_keys, call.ref) == (G, ("message_id",), "m9")
    assert h.calls_with_args("a") == [(G, {"message_id": "m9"})]


def test_bridge_name_used_when_tool_name_missing(tmp_path):
    db = _bridged_db(tmp_path, {"message_id": "m9"})
    conn = sqlite3.connect(db)
    conn.execute("update messages set tool_name = NULL")
    conn.commit()
    conn.close()
    (call,) = History.open(db).sessions(0)[0].calls
    assert call.name == G


def test_tool_row_without_name_kept_as_unknown(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a")])
    conn = sqlite3.connect(db)
    conn.execute("insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp) "
                 "values ('a','tool','zz','nope',NULL,1)")
    conn.commit()
    conn.close()
    (call,) = History.open(db).sessions(0)[0].calls
    assert call.name == "?"
    assert call.arg_keys == () and call.ref is None


# --- compression lineage -------------------------------------------------

ROOT = "cron_3bc4e11d29b1_20260901_080000"
CHILD = "20260901_081500_ab12cd"
GRAND = "20260901_083000_cd34ef"


def _chain_db(tmp_path, root_start=1000.0, child_start=2000.0, grand_start=3000.0,
              child_parent_reason="compression"):
    return make_db(tmp_path / "s.db", [
        _sess(ROOT, [("a", {}, "r1"), ("b", {}, "r2")], started_at=root_start, cost=0.10,
              end_reason="compression", **{"in": 10, "out": 1}),
        _sess(CHILD, [("c", {}, "r3")], started_at=child_start, cost=0.05, parent=ROOT,
              end_reason=child_parent_reason, **{"in": 20, "out": 2}),
        _sess(GRAND, [("d", {}, "r4")], started_at=grand_start, cost=0.02, parent=CHILD,
              **{"in": 30, "out": 3}),
    ])


def test_compression_chain_merged_into_root_run(tmp_path):
    (s,) = History.open(_chain_db(tmp_path)).sessions(0)
    assert s.id == ROOT and s.job_id == "3bc4e11d29b1" and s.source == "cron"
    assert s.started_at == 1000.0
    assert s.est_cost == pytest.approx(0.17)
    assert (s.prompt_tokens, s.output_tokens) == (60, 6)
    assert [c.name for c in s.calls] == ["a", "b", "c", "d"]


@pytest.mark.parametrize("reason", ["branched", None])
def test_branch_child_not_merged(tmp_path, reason):
    db = make_db(tmp_path / "s.db", [
        _sess("p", started_at=1000.0, end_reason=reason),
        _sess("20260901_081500_ab12cd", [("x", {}, "r")], started_at=1100.0, parent="p"),
    ])
    got = {s.id: s for s in History.open(db).sessions(0)}
    assert set(got) == {"p", "20260901_081500_ab12cd"}
    assert len(got["20260901_081500_ab12cd"].calls) == 1 and got["p"].calls == ()


def test_window_applies_to_root(tmp_path):
    db = _chain_db(tmp_path)
    h = History.open(db)
    (s,) = h.sessions(500, until=1500)           # root inside, children after until
    assert s.id == ROOT and len(s.calls) == 4 and s.est_cost == pytest.approx(0.17)
    assert h.sessions(1500) == []                # root before since: no half-run
    assert h.sessions(0, until=500) == []


def test_calls_with_args_covers_chain(tmp_path):
    h = History.open(_chain_db(tmp_path))
    assert [n for n, _a in h.calls_with_args(ROOT)] == ["a", "b", "c", "d"]


def test_chain_cycle_terminates(tmp_path):
    db = _chain_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("update sessions set parent_session_id = ? where id = ?", (GRAND, ROOT))
    conn.execute("update sessions set end_reason = 'compression' where id = ?", (GRAND,))
    conn.commit()
    conn.close()
    # Every member is a continuation of another: no root, nothing hangs.
    assert History.open(db).sessions(0) == []


def test_unknown_tool_name_is_untrusted(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a")])
    conn = sqlite3.connect(db)
    conn.execute("insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp) "
                 "values ('a','tool','U-unknown','nope',NULL,1)")
    conn.commit()
    conn.close()
    assert set(History.open(db).untrusted_texts(0)) == {"U-unknown"}


@pytest.mark.parametrize("path", ["/root/hermes-wiki/../.env", "/root/hermes-wiki/a/../../x",
                                  "hermes-wiki/a.md", "", None, 7, ["/root/hermes-wiki/a.md"]])
def test_read_file_bad_path_is_untrusted(tmp_path, path):
    db = make_db(tmp_path / "s.db", [_sess("a", [("read_file", {"path": path}, "U-file")])])
    assert set(History.open(db).untrusted_texts(0)) == {"U-file"}


def test_read_file_normalised_trusted_path_stays_trusted(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a", [
        ("read_file", {"path": "/root/hermes-wiki/x/../a.md"}, "T-wiki")])])
    assert set(History.open(db).untrusted_texts(0)) == set()


def test_null_tool_call_id_not_joined(tmp_path):
    db = make_db(tmp_path / "s.db", [_sess("a")])
    conn = sqlite3.connect(db)
    tc = [{"id": "None", "function": {"name": "read_file", "arguments": json.dumps({"message_id": "m"})}}]
    conn.execute("insert into messages(session_id, role, tool_calls, timestamp) values ('a','assistant',?,1)",
                 (json.dumps(tc),))
    conn.execute("insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp) "
                 "values ('a','tool','zz',NULL,NULL,1)")
    conn.commit()
    conn.close()
    (call,) = History.open(db).sessions(0)[0].calls
    assert call.name == "?" and call.arg_keys == () and call.ref is None
