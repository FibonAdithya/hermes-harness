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
# The v4-pro list as the Nous models API serves it (MEASURED on the droplet, 2026-10-19:
# `python -m hermes_tuner pricing`). The droplet runs v4-pro through Nous.
NOUS_V4_PRO = Pricing(model="deepseek/deepseek-v4-pro", input_per_m=0.94, output_per_m=1.89,
                      cache_read_per_m=0.08, cache_write_per_m=None,
                      source="provider_models_api", source_url=None, version="nous",
                      fetched_at=0.0)
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
        # A call is (name, args, result) or (name, args, result, turn); turn is a
        # 1-based assistant row, default 1. Each turn up to the last used one gets an
        # assistant row (empty if it issues nothing) followed by its tool rows;
        # `final_turns` empty assistant rows come last.
        calls = [(c + (1,))[:4] for c in s.get("calls", [])]
        t = s["started_at"]
        for turn in range(1, max((c[3] for c in calls), default=0) + 1):
            mine = [(i, c) for i, c in enumerate(calls) if c[3] == turn]
            tc = [{"id": f"{s['id']}-c{i}", "type": "function",
                   "function": {"name": name, "arguments": json.dumps(args)}}
                  for i, (name, args, _r, _t) in mine]
            conn.execute(
                "insert into messages(session_id, role, content, tool_calls, timestamp) "
                "values (?, 'assistant', '', ?, ?)", (s["id"], json.dumps(tc) if tc else None, t))
            for i, (name, _args, result, _t) in mine:
                conn.execute(
                    "insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp) "
                    "values (?, 'tool', ?, ?, ?, ?)", (s["id"], result, f"{s['id']}-c{i}", name, t))
        for _ in range(s.get("final_turns", 0)):
            conn.execute(
                "insert into messages(session_id, role, content, timestamp) "
                "values (?, 'assistant', '', ?)", (s["id"], t))
    conn.commit()
    conn.close()
    return path
