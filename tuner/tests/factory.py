"""Builds a state.db containing the subset of the real schema the tuner reads."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_tuner.model import Pricing

# The v4-pro list (Hermes snapshot of DeepSeek's pricing page, deepseek-pricing-2026-07,
# agent/usage_pricing.py in the pinned Hermes source).
V4_PRO = Pricing(model="deepseek-v4-pro", input_per_m=0.435, output_per_m=0.87,
                 cache_read_per_m=0.003625, cache_write_per_m=None,
                 source="official_docs_snapshot",
                 source_url="https://api-docs.deepseek.com/quick_start/pricing",
                 version="deepseek-pricing-2026-07", fetched_at=0.0)
# One rate for every token kind (FakeBridge's default list): test arithmetic stays simple.
FLAT_RATE = 0.42
FLAT = Pricing(model="fake-model", input_per_m=FLAT_RATE, output_per_m=FLAT_RATE,
               cache_read_per_m=None, cache_write_per_m=None, source="fake", source_url=None,
               version="fake-flat", fetched_at=0.0)


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
