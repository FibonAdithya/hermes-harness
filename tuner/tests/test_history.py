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
