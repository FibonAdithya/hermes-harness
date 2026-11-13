"""Pure text edits for the "Agent follow-ups" section of the owner's todos.md.

Everything outside the section is left byte-for-byte unchanged. Lines are split
on "\\n" only (not str.splitlines), so exotic line separators inside an owner
line never create fake lines.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from hermes_wiki.notes import _CONTROL
from hermes_wiki.secretscan import find_secrets

HEADING = "## Agent follow-ups"
MAX_TITLE = 120
MAX_DETAIL = 300
MAX_DETAILS = 15

_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
_TITLE_ID = re.compile(r"^(.*) \(\d{4}-\d{2}-\d{2}, [^,()]*, id ([0-9a-f]{6})\)$")
_ID_IN_TEXT = re.compile(r"id ([0-9a-f]{6})\)")
_ID_FORMAT = re.compile(r"[0-9a-f]{6}")


class TodoError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class TodoRejected(TodoError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors), 2)
        self.errors = errors


@dataclass(frozen=True)
class Item:
    id: str | None
    title: str
    done: bool


def validate(title: str, details: list[str]) -> tuple[str, list[str]]:
    errors: list[str] = []
    title = title.strip()
    details = [d.strip() for d in details]
    if not title:
        errors.append("title: empty")
    elif len(title) > MAX_TITLE:
        errors.append(f"title: longer than {MAX_TITLE} characters")
    if _CONTROL.search(title):
        errors.append("title: must be one line without control characters")
    if len(details) > MAX_DETAILS:
        errors.append(f"details: more than {MAX_DETAILS}")
    for n, d in enumerate(details, 1):
        if not d:
            errors.append(f"detail {n}: empty")
        elif len(d) > MAX_DETAIL:
            errors.append(f"detail {n}: longer than {MAX_DETAIL} characters")
        if _CONTROL.search(d):
            errors.append(f"detail {n}: must be one line without control characters")
    for name in find_secrets("\n".join([title, *details])):
        errors.append(f"secret pattern: {name}")
    if errors:
        raise TodoRejected(errors)
    return title, details


def _split(text: str) -> list[str]:
    return _LINE.findall(text)


def _section(lines: list[str]) -> tuple[int, int] | None:
    """(heading index, end index) with end exclusive, or None if absent."""
    heads = [i for i, ln in enumerate(lines) if ln.rstrip() == HEADING]
    if len(heads) > 1:
        raise TodoError(f"duplicate heading: {HEADING}")
    if not heads:
        return None
    h = heads[0]
    end = len(lines)
    for i in range(h + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return h, end


def _ensure_eol(lines: list[str], idx: int) -> None:
    if 0 <= idx < len(lines) and not lines[idx].endswith("\n"):
        lines[idx] += "\n"


def add_item(text: str, title: str, details: list[str], *, source: str, today: date,
             new_id: Callable[[], str]) -> tuple[str, str]:
    lines = _split(text)
    sec = _section(lines)
    used = set(_ID_IN_TEXT.findall("".join(lines[sec[0]:sec[1]]))) if sec else set()
    for _ in range(100):
        item_id = new_id()
        if item_id not in used:
            break
    else:
        raise TodoError("could not generate an unused id")
    item = [f"- [ ] {title} ({today:%Y-%m-%d}, {source}, id {item_id})\n"]
    item += [f"  - {d}\n" for d in details]

    if sec:
        h, end = sec
        p = end
        while p > h + 1 and lines[p - 1].strip() == "":
            p -= 1
        _ensure_eol(lines, p - 1)
        lines[p:p] = item
    else:
        start = 0
        if lines and lines[0].rstrip() == "---":
            for k in range(1, len(lines)):
                if lines[k].rstrip() == "---":
                    start = k + 1
                    break
        h1 = next((i for i in range(start, len(lines)) if lines[i].startswith("# ")), None)
        scan = h1 + 1 if h1 is not None else start
        p = next((i for i in range(scan, len(lines)) if lines[i].startswith("## ")), len(lines))
        _ensure_eol(lines, p - 1)
        block = [HEADING + "\n", *item]
        if p > 0 and lines[p - 1].strip() != "":
            block.insert(0, "\n")
        if p < len(lines):
            block.append("\n")
        lines[p:p] = block
    return "".join(lines), item_id


def tick_item(text: str, item_id: str) -> str | None:
    lines = _split(text)
    sec = _section(lines)
    if sec is None:
        raise TodoError(f"no {HEADING} section")
    if not _ID_FORMAT.fullmatch(item_id):
        raise TodoError(f"no item with id {item_id}")
    h, end = sec
    hits = []
    for i in range(h + 1, end):
        body = lines[i].rstrip("\n")
        if _ITEM.match(body) and body.rstrip().endswith(f"id {item_id})"):
            hits.append(i)
    if not hits:
        raise TodoError(f"no item with id {item_id}")
    if len(hits) > 1:
        raise TodoError(f"more than one item with id {item_id}")
    i = hits[0]
    if lines[i][3] in "xX":
        return None
    lines[i] = "- [x]" + lines[i][5:]
    return "".join(lines)


def list_items(text: str, include_done: bool) -> list[Item]:
    lines = _split(text)
    sec = _section(lines)
    if sec is None:
        return []
    out = []
    for i in range(sec[0] + 1, sec[1]):
        m = _ITEM.match(lines[i].rstrip("\n"))
        if not m:
            continue
        done = m.group(1) in "xX"
        if done and not include_done:
            continue
        rest = m.group(2)
        t = _TITLE_ID.match(rest)
        out.append(Item(t.group(2), t.group(1), done) if t else Item(None, rest, done))
    return out
