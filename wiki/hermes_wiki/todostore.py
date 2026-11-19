"""Every agent-todos write: import owner edits, apply one change, commit all files at once.

Each attempt reads agent-todos.jsonl, agent-todos.md and todos.md at one commit,
imports the owner's ticks from the page and any stray items old clients left
under "## Agent follow-ups" in todos.md, applies the change, and commits the
files whose text changed in a single commit on top of that head.
"""
import copy
import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from hermes_wiki.todopage import data_marker, owner_actions, render
from hermes_wiki.todorecords import Record, dumps, loads, new_id as _new_id, reopen
from hermes_wiki.todos import (ATTEMPTS, Conflict, StoreError, TodoError, WriteUncertain,
                               _ID_FORMAT, take_section_items)

JSONL, PAGE, TODOS = "agent-todos.jsonl", "agent-todos.md", "todos.md"
IMPORT_MESSAGE = "todo: import owner edits"
STALE_LOG = "agent-todos.md was stale; unticks ignored"


class NotMigrated(TodoError):
    def __init__(self):
        super().__init__("not migrated", 1)


@dataclass
class Change:
    message: str
    fn: Callable[[list[Record], date], list[Record] | None]


def _import_ticks(records: list[Record], page: str, jsonl: str, today: date,
                  log: Callable[[str], None]) -> None:
    fresh = data_marker(page) == hashlib.sha256(jsonl.encode("utf-8")).hexdigest()
    closes, reopens = owner_actions(page)
    ignored = False
    for r in records:
        if r.id in closes and r.status in ("open", "suggested"):
            r.status = "done"
            r.closed = {"at": today.isoformat(), "by": "owner", "reason": "ticked in Obsidian"}
        elif r.id in reopens and r.status == "done":
            if fresh:
                reopen(r, log)
            else:
                ignored = True
    if ignored:
        log(STALE_LOG)


def _import_strays(records: list[Record], todos_md: str, today: date, source: str,
                   gen: Callable[[], str], log: Callable[[str], None]) -> str:
    items, rest = take_section_items(todos_md)
    titles = {r.id: r.title.strip() for r in records}
    by_id = {r.id: r for r in records}
    for item, details in items:
        title = item.title.strip() or "(untitled)"
        item_id = item.id
        if item_id is not None and item_id in titles:
            if titles[item_id] == title:
                dup = by_id[item_id]
                if item.done and dup.status in ("open", "suggested"):
                    dup.status = "done"
                    dup.closed = {"at": today.isoformat(), "by": "owner",
                                  "reason": "ticked in todos.md"}
                continue
            item_id = None
        if item_id is None:
            item_id = _new_id(set(titles), gen)
        r = Record(id=item_id, title=title, details=list(details), project="unsorted",
                   created=item.created or today.isoformat(), source=item.source or source)
        if item.done:
            r.status = "done"
            r.closed = {"at": today.isoformat(), "by": "owner", "reason": "ticked in todos.md"}
        records.append(r)
        titles[item_id] = title
        by_id[item_id] = r
        log(f"imported {item_id} from todos.md")
    return rest


def _uncertain_what(message: str) -> str:
    last = message.rsplit(" ", 1)[-1]
    return f"item {last}" if _ID_FORMAT.fullmatch(last) else "the write"


def transact(store, change: Change, *, today: date, source: str, new_id: Callable[[], str],
             sleep, log: Callable[[str], None]) -> bool:
    for attempt in range(ATTEMPTS):
        try:
            base, files = store.head()
        except StoreError as e:
            raise TodoError(f"GitHub: {e}") from None
        jsonl = files.get(JSONL)
        if jsonl is None:
            raise NotMigrated()
        page = files.get(PAGE)
        todos_md = files.get(TODOS)

        records = loads(jsonl)
        _import_ticks(records, page or "", jsonl, today, log)
        new_todos = todos_md
        if todos_md is not None:
            new_todos = _import_strays(records, todos_md, today, source, new_id, log)

        out = change.fn(copy.deepcopy(records), today)
        applied = out is not None
        if applied:
            records = out

        new_jsonl = dumps(records)
        changed: dict[str, str] = {}
        if new_jsonl != jsonl:
            changed[JSONL] = new_jsonl
        if new_todos != todos_md:
            changed[TODOS] = new_todos
        if not applied and not changed:
            return False  # nothing to import and no change: never commit a page refresh alone
        new_page = render(records, today)
        if JSONL in changed or new_page != page:
            changed[PAGE] = new_page
        if not changed:
            return False

        try:
            store.commit(base, changed, change.message if applied else IMPORT_MESSAGE)
            return applied
        except Conflict:
            if attempt == ATTEMPTS - 1:
                raise TodoError(
                    f"agent-todos kept changing; gave up after {ATTEMPTS} attempts") from None
            sleep(0.5 * 2 ** attempt + random.uniform(0, 0.5))
        except WriteUncertain:
            what = _uncertain_what(change.message) if applied else "the write"
            raise TodoError(
                f"timed out writing agent-todos; {what} may or may not have landed") from None
        except StoreError as e:
            raise TodoError(f"GitHub: {e}") from None
    raise AssertionError("unreachable")
