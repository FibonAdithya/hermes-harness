"""Todo commands on the agent-todos store, with a fallback to todos.md until migrated."""
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from hermes_wiki import todos
from hermes_wiki.config import WikiConfig
from hermes_wiki.notes import _CONTROL
from hermes_wiki.secretscan import find_secrets
from hermes_wiki.todorecords import (Record, loads, new_id as _new_id, project_from_origin, reopen,
                                     validate_item)
from hermes_wiki.todostore import JSONL, Change, NotMigrated, transact
from hermes_wiki.todotree import DryTree, GhTree
from hermes_wiki.todos import StoreError, TodoError

MIGRATE_FIRST = "run hermes-wiki todo migrate first"
IGNORED = "hermes-wiki: --project/--done-when ignored until hermes-wiki todo migrate"


def tree_for(cfg: WikiConfig, run) -> GhTree | DryTree:
    if os.environ.get("HERMES_WIKI_DRY_PUBLISH") == "1":
        return DryTree(cfg.state_dir / "dry")
    # The droplet nightly reads PRs with the all-repos read-only token (GH_CONFIG_DIR) and
    # writes the wiki with a second token scoped to hermes-wiki (runbooks/wiki.md).
    write_dir = os.environ.get("HERMES_WIKI_TREE_GH_CONFIG_DIR")
    env = {**os.environ, "GH_CONFIG_DIR": write_dir} if write_dir else None
    return GhTree(cfg.repo, cfg.gh, run, env=env)


def _log(cfg: WikiConfig) -> Callable[[str], None]:
    if cfg.public_only:
        return lambda msg: None
    return lambda msg: print(f"hermes-wiki: {msg}", file=sys.stderr)


def _transact(cfg, tree, change, *, today, new_id, sleep) -> bool:
    return transact(tree, change, today=today, source=cfg.source, new_id=new_id,
                    sleep=sleep, log=_log(cfg))


def _strict(cfg, tree, change, *, today, new_id, sleep) -> bool:
    try:
        return _transact(cfg, tree, change, today=today, new_id=new_id, sleep=sleep)
    except NotMigrated:
        raise TodoError(MIGRATE_FIRST, 1) from None


def _find(records: list[Record], item_id: str) -> Record:
    for r in records:
        if r.id == item_id:
            return r
    raise TodoError(f"no item with id {item_id}", 1)


def _project_of(cwd: Path, run) -> str:
    try:
        r = run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unsorted"
    if r.returncode != 0:
        return "unsorted"
    return project_from_origin(r.stdout)


def cmd_add(cfg: WikiConfig, tree, title: str, details: list[str], project: str | None,
            checks: list[str], cwd: Path, *, today: date, new_id: Callable[[], str],
            sleep, run) -> str:
    resolved = project if project is not None else _project_of(cwd, run)
    title, details, done_when = validate_item(title, details, resolved, checks)
    landed: list[str] = []
    change = Change(message="todo: add", fn=lambda records, today_: records)

    def fn(records: list[Record], today_: date) -> list[Record]:
        item_id = _new_id({r.id for r in records}, new_id)
        landed[:] = [item_id]
        change.message = f"todo: add {item_id}"  # the id is only known here
        records.append(Record(id=item_id, title=title, details=details, project=resolved,
                              created=today.isoformat(), source=cfg.source,
                              done_when=done_when))
        return records

    change.fn = fn
    try:
        _transact(cfg, tree, change, today=today, new_id=new_id, sleep=sleep)
    except NotMigrated:
        if project is not None or checks:
            print(IGNORED, file=sys.stderr)
        return todos.cmd_add(cfg, todos.store_for(cfg, run), title, details,
                             today=today, new_id=new_id, sleep=sleep)
    return landed[0]


def _validate_reason(reason: str) -> str:
    reason = reason.strip()
    errors = []
    if not reason:
        errors.append("reason: empty")
    elif len(reason) > todos.MAX_DETAIL:
        errors.append(f"reason: longer than {todos.MAX_DETAIL} characters")
    if _CONTROL.search(reason):
        errors.append("reason: must be one line without control characters")
    errors += [f"secret pattern: {n}" for n in find_secrets(reason)]
    if errors:
        raise todos.TodoRejected(errors)
    return reason


def cmd_done(cfg: WikiConfig, tree, item_id: str, reason: str | None, *, today: date,
             new_id, sleep, run) -> bool:
    if reason is not None:
        reason = _validate_reason(reason)
    def fn(records, today_):
        r = _find(records, item_id)
        if r.status == "done":
            return None
        r.status = "done"
        r.closed = {"at": today.isoformat(), "by": "agent", "reason": reason or "done"}
        return records

    try:
        return _transact(cfg, tree, Change(f"todo: done {item_id}", fn),
                         today=today, new_id=new_id, sleep=sleep)
    except NotMigrated:
        return todos.cmd_done(cfg, todos.store_for(cfg, run), item_id, sleep=sleep)


def cmd_edit(cfg: WikiConfig, tree, item_id: str, title: str | None, add_details: list[str],
             project: str | None, checks: list[str], *, clear_done_when: bool = False,
             clear_details: bool = False, today: date, new_id, sleep) -> bool:
    def fn(records, today_):
        r = _find(records, item_id)
        kept = [] if clear_details else r.details  # clearing comes before the additions
        new_title, details, parsed = validate_item(
            r.title if title is None else title, kept + add_details,
            r.project if project is None else project, checks)
        r.title, r.details = new_title, details
        r.project = r.project if project is None else project
        if clear_done_when:
            r.done_when = []
        for c in parsed:
            if c not in r.done_when:
                r.done_when.append(c)
        return records

    return _strict(cfg, tree, Change(f"todo: edit {item_id}", fn),
                   today=today, new_id=new_id, sleep=sleep)


def cmd_rm(cfg: WikiConfig, tree, item_id: str, *, today: date, new_id, sleep) -> None:
    def fn(records, today_):
        r = _find(records, item_id)
        return [x for x in records if x is not r]

    _strict(cfg, tree, Change(f"todo: rm {item_id}", fn), today=today, new_id=new_id, sleep=sleep)


def cmd_reopen(cfg: WikiConfig, tree, item_id: str, *, today: date, new_id, sleep) -> None:
    def fn(records, today_):
        r = _find(records, item_id)
        if r.status == "open":
            raise TodoError(f"{item_id} is already open", 1)
        if r.status == "suggested":
            r.suggestion = dict(r.suggestion or {}, rejected=True)
            r.status = "open"
        else:
            reopen(r, logs.append)  # logged once below, not on every conflict retry
        return records

    logs: list[str] = []
    _strict(cfg, tree, Change(f"todo: reopen {item_id}", fn),
            today=today, new_id=new_id, sleep=sleep)
    log = _log(cfg)
    for m in dict.fromkeys(logs):
        log(m)


def cmd_list(cfg: WikiConfig, tree, include_done: bool, as_json: bool,
             project: str | None, *, run) -> list[Record]:
    if cfg.public_only:
        raise TodoError("todo list is not available on a public_only wiki", code=2)
    try:
        _, files = tree.head()
    except StoreError as e:
        raise TodoError(f"GitHub: {e}") from None
    text = files.get(JSONL)
    if text is None:
        if as_json:
            raise TodoError(MIGRATE_FIRST, 1)
        # old items have no project, so --project is not applied to them
        items = todos.cmd_list(cfg, todos.store_for(cfg, run), include_done)
        records = [Record(id=i.id or "-", title=i.title, details=[], project="unsorted",
                          created=i.created or "", source=i.source or "",
                          status="done" if i.done else "open") for i in items]
    else:
        records = loads(text)
    return [r for r in records
            if (include_done or r.status != "done") and (project is None or text is None or r.project == project)]
