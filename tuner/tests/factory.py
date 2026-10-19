"""Builds a state.db containing the subset of the real schema the tuner reads."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def make_db(path: Path, sessions: list[dict]) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table sessions (
            id text primary key, source text, started_at real,
            input_tokens integer, output_tokens integer, cache_read_tokens integer,
            cache_write_tokens integer,
            estimated_cost_usd real, model text,
            parent_session_id text, end_reason text);
        create table messages (
            id integer primary key autoincrement, session_id text, role text,
            content text, tool_call_id text, tool_calls text, tool_name text,
            timestamp real);
    """)
    for s in sessions:
        conn.execute(
            "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s["source"], s["started_at"], s.get("in", 0), s.get("out", 0),
             s.get("cache", 0), s.get("cw", 0), s.get("cost", 0.0), s.get("model", "m"),
             s.get("parent"), s.get("end_reason")))
        calls = s.get("calls", [])
        t = s["started_at"]
        if calls:
            tc = [{"id": f"{s['id']}-c{i}", "type": "function",
                   "function": {"name": name, "arguments": json.dumps(args)}}
                  for i, (name, args, _r) in enumerate(calls)]
            conn.execute(
                "insert into messages(session_id, role, content, tool_calls, timestamp) "
                "values (?, 'assistant', '', ?, ?)", (s["id"], json.dumps(tc), t))
            for i, (name, _args, result) in enumerate(calls):
                conn.execute(
                    "insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp) "
                    "values (?, 'tool', ?, ?, ?, ?)", (s["id"], result, f"{s['id']}-c{i}", name, t))
    conn.commit()
    conn.close()
    return path
