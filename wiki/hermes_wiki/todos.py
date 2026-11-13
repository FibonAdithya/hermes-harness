"""Pure text edits for the "Agent follow-ups" section of the owner's todos.md.

Everything outside the section is left byte-for-byte unchanged. Lines are split
on "\\n" only (not str.splitlines), so exotic line separators inside an owner
line never create fake lines.
"""
import base64
import hashlib
import json
import os
import random
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from hermes_wiki.config import WikiConfig
from hermes_wiki.notes import _CONTROL
from hermes_wiki.secretscan import find_secrets

PATH = "todos.md"
ATTEMPTS = 5
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
        m = _ITEM.match(lines[i].rstrip("\r\n"))
        if not m:
            continue
        done = m.group(1) in "xX"
        if done and not include_done:
            continue
        rest = m.group(2)
        t = _TITLE_ID.match(rest)
        out.append(Item(t.group(2), t.group(1), done) if t else Item(None, rest, done))
    return out


class Conflict(Exception):
    pass


class WriteUncertain(Exception):
    pass


class StoreError(Exception):
    pass


def _first_line(text: str) -> str:
    return (text or "").strip().split("\n", 1)[0]


class GhContents:
    def __init__(self, repo: str, gh: str, run):
        self.repo = repo
        self.gh = gh
        self.run = run

    def get(self, path: str) -> tuple[str, str] | None:
        argv = [self.gh, "api", f"repos/{self.repo}/contents/{path}?ref=main"]
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise StoreError("timed out reading todos.md") from None
        except OSError as e:
            raise StoreError(str(e)) from None
        if r.returncode != 0:
            if "(HTTP 404)" in (r.stderr or ""):
                return None
            raise StoreError(_first_line(r.stderr) or f"gh exited {r.returncode}")
        try:
            data = json.loads(r.stdout)
            return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
        except (ValueError, KeyError, TypeError) as e:
            raise StoreError(f"unexpected response: {type(e).__name__}") from None

    def put(self, path: str, text: str, sha: str, message: str) -> None:
        content = base64.b64encode(text.encode("utf-8")).decode("ascii")
        argv = [self.gh, "api", "-X", "PUT", f"repos/{self.repo}/contents/{path}",
                "-f", f"message={message}", "-f", f"content={content}",
                "-f", f"sha={sha}", "-f", "branch=main"]
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise WriteUncertain() from None
        except OSError as e:
            raise StoreError(str(e)) from None
        if r.returncode != 0:
            if "(HTTP 409)" in (r.stderr or ""):
                raise Conflict()
            raise StoreError(_first_line(r.stderr) or f"gh exited {r.returncode}")


class DryContents:
    def __init__(self, dir: Path):
        self.dir = Path(dir)

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def get(self, path: str) -> tuple[str, str] | None:
        f = self.dir / path
        if not f.exists():
            return None
        text = f.read_text(encoding="utf-8")
        return text, self._sha(text)

    def put(self, path: str, text: str, sha: str, message: str) -> None:
        cur = self.get(path)
        if cur is not None and cur[1] != sha:
            raise Conflict()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / path).write_text(text, encoding="utf-8")


def store_for(cfg: WikiConfig, run) -> GhContents | DryContents:
    if os.environ.get("HERMES_WIKI_DRY_PUBLISH") == "1":
        return DryContents(cfg.state_dir / "dry")
    return GhContents(cfg.repo, cfg.gh, run)


def update(store, edit: Callable[[str], tuple[str, str] | None], *, sleep,
           item_id: Callable[[], str] = lambda: "") -> bool:
    for attempt in range(ATTEMPTS):
        try:
            got = store.get(PATH)
        except StoreError as e:
            raise TodoError(f"GitHub: {e}") from None
        if got is None:
            raise TodoError("todos.md not found on main")
        text, sha = got
        out = edit(text)
        if out is None:
            return False
        new_text, message = out
        try:
            store.put(PATH, new_text, sha, message)
            return True
        except Conflict:
            if attempt == ATTEMPTS - 1:
                raise TodoError(
                    f"todos.md kept changing; gave up after {ATTEMPTS} attempts") from None
            sleep(0.5 * 2 ** attempt + random.uniform(0, 0.5))
        except WriteUncertain:
            iid = item_id()
            what = f"item {iid}" if iid else "the write"
            raise TodoError(
                f"timed out writing todos.md; {what} may or may not have landed") from None
        except StoreError as e:
            raise TodoError(f"GitHub: {e}") from None
    raise AssertionError("unreachable")


def cmd_add(cfg: WikiConfig, store, title: str, details: list[str], *,
            today: date | None = None, new_id: Callable[[], str] | None = None,
            sleep=time.sleep) -> str:
    title, details = validate(title, details)
    today = today or datetime.now(timezone.utc).date()
    new_id = new_id or (lambda: secrets.token_hex(3))
    landed: list[str] = []

    def edit(text: str) -> tuple[str, str]:
        new_text, item_id = add_item(text, title, details, source=cfg.source,
                                     today=today, new_id=new_id)
        landed[:] = [item_id]
        return new_text, f"todo: add {item_id}"

    update(store, edit, sleep=sleep, item_id=lambda: landed[0] if landed else "")
    return landed[0]


def cmd_done(cfg: WikiConfig, store, item_id: str, *, sleep=time.sleep) -> bool:
    def edit(text: str) -> tuple[str, str] | None:
        new_text = tick_item(text, item_id)
        return None if new_text is None else (new_text, f"todo: done {item_id}")

    return update(store, edit, sleep=sleep, item_id=lambda: item_id)


def cmd_list(cfg: WikiConfig, store, include_done: bool) -> list[Item]:
    if cfg.public_only:
        raise TodoError("todo list is not available on a public_only wiki", code=2)
    try:
        got = store.get(PATH)
    except StoreError as e:
        raise TodoError(f"GitHub: {e}") from None
    if got is None:
        raise TodoError("todos.md not found on main")
    return list_items(got[0], include_done)
