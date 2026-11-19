"""One-off migration of the "## Agent follow-ups" items in todos.md into agent-todos.jsonl."""
import json
import re
import subprocess
from collections.abc import Callable
from datetime import date

from hermes_wiki.config import WikiConfig
from hermes_wiki.todopage import render
from hermes_wiki.todorecords import _PROJECT, Record, dumps, new_id as _new_id, validate_item
from hermes_wiki.todos import (Conflict, StoreError, TodoError, TodoRejected, WriteUncertain,
                               _section, _split, take_section_items)
from hermes_wiki.todostore import JSONL, PAGE, TODOS
from hermes_wiki.todotree import PATHS

GITIGNORE = ".gitignore"
IGNORE_LINE = ".todo-suggestions.jsonl"
INSERTED = ["![[agent-todos]]\n", "Open the page to tick: [[agent-todos]]\n"]

_NAME = r"([A-Za-z0-9_.-]+)"
_REPO_PATTERNS = [re.compile(p) for p in (
    r"github\.com/FibonAdithya/" + _NAME, r"FibonAdithya/" + _NAME,
    r"~/TIG/" + _NAME, r"/TIG/" + _NAME)]
_PREFIX = re.compile(r"^([a-z0-9-]+)[: ]")
_MERGE = re.compile(r"^(Merge|Review and merge)\b")
_PR = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_CHECK_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _candidates(text: str) -> set[str]:
    out = set()
    for pat in _REPO_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1).rstrip(".").lower()
            if name.endswith(".git"):
                name = name[:-4]
            if name:
                out.add(name)
    return out


def _project(title: str, text: str, repos: set[str]) -> str:
    found = _candidates(text)
    m = _PREFIX.match(title)
    if m and m.group(1) in repos:
        found.add(m.group(1))
    if len(found) == 1:
        (name,) = found
        if _PROJECT.fullmatch(name):
            return name
    return "unsorted"


def _done_when(title: str, text: str) -> list[dict]:
    if not _MERGE.match(title):
        return []
    prs = {(m.group(1).rstrip(".,;:)"), int(m.group(2))) for m in _PR.finditer(text)}
    if len(prs) != 1:
        return []
    (repo, num), = prs
    if not _CHECK_REPO.fullmatch(repo) or num < 1:
        return []
    return [{"pr": f"{repo}#{num}", "state": "merged"}]


def _done_dates(cfg: WikiConfig, run) -> dict[str, str]:
    argv = [cfg.gh, "api", "--paginate", f"repos/{cfg.repo}/commits?path=todos.md", "--jq",
            ".[] | {m: .commit.message, d: .commit.committer.date}"]
    try:
        r = run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TodoError(f"GitHub: commit history of todos.md unavailable ({type(e).__name__})") from None
    if r.returncode != 0:
        why = (r.stderr or "").strip().split("\n", 1)[0] or f"gh exited {r.returncode}"
        raise TodoError(f"GitHub: commit history of todos.md unavailable ({why})")
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            first = obj["m"].split("\n", 1)[0]
            day = obj["d"][:10]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TodoError("GitHub: commit history of todos.md unavailable (unexpected response)") from None
        if first.startswith("todo: done "):
            i = first[len("todo: done "):]
            if i not in out or day < out[i]:
                out[i] = day
    return out


def _new_todos(text: str) -> str:
    """todos.md with the section items replaced by the page embed and link lines."""
    _, rest = take_section_items(text)
    lines = _split(rest)
    sec = _section(lines)
    if sec is None:
        return text
    h, end = sec
    body = lines[h + 1:end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if lines[h] and not lines[h].endswith("\n"):
        lines[h] += "\n"
    if body and not body[-1].endswith("\n"):
        body[-1] += "\n"
    new = [lines[h], *INSERTED, *body]
    if end < len(lines):
        new.append("\n")
    return "".join(lines[:h] + new + lines[end:])


def _gitignore(old: str | None) -> str | None:
    if old is None:
        return IGNORE_LINE + "\n"
    if IGNORE_LINE in old.split("\n"):
        return None
    return old + ("" if old == "" or old.endswith("\n") else "\n") + IGNORE_LINE + "\n"


def migrate(cfg: WikiConfig, tree, *, today: date, run, log: Callable[[str], None],
            new_id: Callable[[], str], history: bool = True) -> list[Record]:
    try:
        base, files = tree.head(PATHS + (GITIGNORE,))
    except StoreError as e:
        raise TodoError(f"GitHub: {e}") from None
    if files.get(JSONL) is not None:
        raise TodoError("already migrated: agent-todos.jsonl exists", 1)
    todos_md = files.get(TODOS)
    if todos_md is None:
        raise TodoError("todos.md is missing; nothing to migrate", 1)

    items, _ = take_section_items(todos_md)
    texts = [" ".join([it.title, *d]) for it, d in items]
    repos = set().union(*(_candidates(t) for t in texts)) if texts else set()
    ticked = {it.id for it, _ in items if it.done and it.id}
    closed_on = _done_dates(cfg, run) if history and ticked else {}

    used = {it.id for it, _ in items if it.id}
    seen: set[str] = set()
    records: list[Record] = []
    for (item, details), text in zip(items, texts):
        item_id = item.id
        if item_id is None or item_id in seen:
            item_id = _new_id(used, new_id)
            used.add(item_id)
        kept = item_id == item.id
        seen.add(item_id)
        title = item.title.strip() or "(untitled)"
        project = _project(title, text, repos)
        try:
            title, details, _checks = validate_item(title, list(details), project, [])
        except TodoRejected as e:
            raise TodoRejected([f"item {item_id}: {err}" for err in e.errors]) from None
        r = Record(id=item_id, title=title, details=details, project=project,
                   created=item.created or today.isoformat(), source=item.source or cfg.source,
                   done_when=_done_when(title, " ".join([title, *details])))
        if item.done:
            agent = kept and item_id in closed_on
            r.status = "done"
            r.closed = {"at": closed_on[item_id] if agent else today.isoformat(),
                        "by": "agent" if agent else "owner", "reason": "ticked before migration"}
        records.append(r)

    out = {JSONL: dumps(records), PAGE: render(records, today), TODOS: _new_todos(todos_md)}
    ignore = _gitignore(files.get(GITIGNORE))
    if ignore is not None:
        out[GITIGNORE] = ignore
    try:
        tree.commit(base, out, "todo: migrate")
    except Conflict:
        raise TodoError("wiki changed during migration; re-run", 1) from None
    except WriteUncertain:
        raise TodoError("timed out; check whether the migrate commit landed before re-running", 1) from None
    except StoreError as e:
        raise TodoError(f"GitHub: {e}") from None
    log(f"migrated {len(records)} items")
    return records
