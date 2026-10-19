"""Compression lineage in Hermes's state.db.

When Hermes compresses a conversation it moves the live agent to a new session
id and records the old one as `sessions.parent_session_id` of the new row. A
cron run's later tool calls can therefore carry an id that is not `cron_…`;
its root is.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MAX_HOPS = 20


class LineageError(Exception):
    """state.db could not be read, or the chain has a cycle or exceeds MAX_HOPS."""


def root_session_id(db_path: Path, session_id: str, max_hops: int = MAX_HOPS) -> str | None:
    """The root of `session_id`'s parent chain, or None if the session is unknown.

    Follows at most `max_hops` parent links. Opens the database read-only and
    never creates it. Raises LineageError on any database error, a cycle, or a
    chain longer than `max_hops`.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        raise LineageError(str(exc)) from exc
    try:
        current, seen, hops = session_id, {session_id}, 0
        while True:
            row = conn.execute("select parent_session_id from sessions where id = ?",
                               (current,)).fetchone()
            if row is None:
                return None if current == session_id else current
            parent = row[0]
            if not parent:
                return current
            hops += 1
            if hops > max_hops:
                raise LineageError(f"lineage of {session_id!r} exceeds {max_hops} hops")
            if parent in seen:
                raise LineageError(f"lineage of {session_id!r} has a cycle")
            seen.add(parent)
            current = parent
    except sqlite3.Error as exc:
        raise LineageError(str(exc)) from exc
    finally:
        conn.close()
