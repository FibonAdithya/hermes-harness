"""Read-only view of Hermes's state.db."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import policy
from .model import Session, ToolCall

JOB_SESSION_RE = re.compile(r"^cron_([0-9a-f]{12})_\d{8}_\d{6}$")


class HistoryUnavailable(Exception):
    pass


def _args(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _issued_calls(tool_calls_json: str | None) -> Iterator[tuple[str, str, dict]]:
    """Yield (call_id, name, args) from an assistant row, unwrapping Tool Search."""
    try:
        entries = json.loads(tool_calls_json or "[]")
    except ValueError:
        return
    if not isinstance(entries, list):
        return
    for e in entries:
        if not isinstance(e, dict):
            continue
        fn = e.get("function") or {}
        name = fn.get("name") or "?"
        args = _args(fn.get("arguments"))
        if name == "tool_call":
            name = args.get("name") or "?"
            args = _args(args.get("arguments"))
        yield str(e.get("id")), name, args


def _is_untrusted(name: str, args: dict) -> bool:
    if name == "read_file":
        path = str(args.get("path", ""))
        return not path.startswith(policy.TRUSTED_READ_ROOTS)
    return name in policy.UNTRUSTED_TOOLS or name.startswith(policy.UNTRUSTED_PREFIXES)


class History:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @classmethod
    def open(cls, db_path: Path) -> History:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as e:
            raise HistoryUnavailable(str(e)) from e
        conn.row_factory = sqlite3.Row
        return cls(conn)

    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            raise HistoryUnavailable(str(e)) from e

    def _calls(self, session_ids: list[str] | None, since: float | None = None):
        """Yield (session_id, ToolCall, args, result_text) for every tool row."""
        where, params = "", ()
        if session_ids is not None:
            where = f"where session_id in ({','.join('?' * len(session_ids))})"
            params = tuple(session_ids)
        elif since is not None:
            where = "where session_id in (select session_id from messages where role = 'tool' and timestamp >= ?)"
            params = (since,)
        rows = self._q(
            "select id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp "
            f"from messages {where} order by id", params)
        issued: dict[tuple[str, str], tuple[str, dict]] = {}
        for r in rows:
            if r["role"] == "assistant" and r["tool_calls"]:
                for cid, name, args in _issued_calls(r["tool_calls"]):
                    issued[(r["session_id"], cid)] = (name, args)
            elif r["role"] == "tool":
                if since is not None and session_ids is None and r["timestamp"] < since:
                    continue
                cid = str(r["tool_call_id"])
                name, args = issued.get((r["session_id"], cid), ("?", {}))
                if r["tool_name"]:
                    name = r["tool_name"]
                text = r["content"] or ""
                ref = args.get("message_id")
                yield r["session_id"], ToolCall(
                    call_id=cid,
                    name=name,
                    arg_keys=tuple(sorted(args)),
                    ref=ref if isinstance(ref, str) else None,
                    result_chars=len(text),
                ), args, text

    def sessions(self, since: float, until: float | None = None) -> list[Session]:
        sql = ("select id, source, started_at, input_tokens, output_tokens, cache_read_tokens, "
               "estimated_cost_usd from sessions where started_at >= ?")
        params: tuple = (since,)
        if until is not None:
            sql += " and started_at < ?"
            params += (until,)
        rows = self._q(sql + " order by started_at", params)
        by_session: dict[str, list[ToolCall]] = {r["id"]: [] for r in rows}
        for sid, call, _args_, _text in self._calls(list(by_session)) if by_session else ():
            by_session[sid].append(call)
        out = []
        for r in rows:
            m = JOB_SESSION_RE.match(r["id"])
            out.append(Session(
                id=r["id"],
                source=r["source"],
                job_id=m.group(1) if m else None,
                started_at=r["started_at"],
                est_cost=r["estimated_cost_usd"] or 0.0,
                prompt_tokens=(r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0),
                output_tokens=r["output_tokens"] or 0,
                calls=tuple(by_session[r["id"]]),
            ))
        return out

    def last_activity(self) -> float:
        rows = self._q("select max(timestamp) as t from messages")
        return rows[0]["t"] or 0.0

    def calls_with_args(self, session_id: str) -> list[tuple[str, dict]]:
        return [(c.name, a) for _s, c, a, _t in self._calls([session_id])]

    def untrusted_texts(self, since: float) -> Iterator[str]:
        for _s, c, a, text in self._calls(None, since):
            if _is_untrusted(c.name, a):
                yield text
