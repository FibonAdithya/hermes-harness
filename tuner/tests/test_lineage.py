"""Root of a session's compression lineage in state.db."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_tuner.lineage import LineageError, root_session_id


def db(tmp_path: Path, rows: list[tuple[str, str | None]]) -> Path:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("create table sessions (id text primary key, parent_session_id text)")
    conn.executemany("insert into sessions values (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_root_follows_parents(tmp_path):
    p = db(tmp_path, [("root", None), ("c1", "root"), ("c2", "c1")])
    assert root_session_id(p, "c2") == "root"
    assert root_session_id(p, "c1") == "root"
    assert root_session_id(p, "root") == "root"


def test_unknown_session_is_none(tmp_path):
    p = db(tmp_path, [("root", None)])
    assert root_session_id(p, "nope") is None


def test_parent_row_missing_returns_parent_id(tmp_path):
    p = db(tmp_path, [("c1", "gone")])
    assert root_session_id(p, "c1") == "gone"


def test_cycle_raises(tmp_path):
    p = db(tmp_path, [("a", "b"), ("b", "a")])
    # Detected as a cycle, not by running into the hop cap.
    with pytest.raises(LineageError, match="cycle"):
        root_session_id(p, "a")


def test_hop_cap_boundary(tmp_path):
    rows = [("s0", None)] + [(f"s{i}", f"s{i - 1}") for i in range(1, 22)]
    p = db(tmp_path, rows)
    assert root_session_id(p, "s20") == "s0"  # exactly 20 hops
    with pytest.raises(LineageError):
        root_session_id(p, "s21")  # 21 hops


def test_missing_db_raises_and_does_not_create_it(tmp_path):
    p = tmp_path / "absent.db"
    with pytest.raises(LineageError):
        root_session_id(p, "x")
    assert not p.exists()
