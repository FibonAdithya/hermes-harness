"""Todo records and their JSONL codec (one JSON object per line, sorted by id)."""
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import date

from hermes_wiki.todos import TodoError, TodoRejected, validate

STATUSES = ("open", "suggested", "done")
CLOSERS = ("check", "agent", "owner")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
_PART = r"[A-Za-z0-9_.-]+"
_CHECK = re.compile(rf"(pr|issue):({_PART}/{_PART})#([1-9][0-9]*)")
_ORIGIN = re.compile(
    r"(?:git@github\.com:|https://github\.com/)[^/\s]+/([^/\s]+?)(?:\.git)?/?")
_ID = re.compile(r"[0-9a-f]{6}")


@dataclass
class Record:
    id: str
    title: str
    details: list[str]
    project: str
    created: str
    source: str
    status: str = "open"
    done_when: list[dict] = field(default_factory=list)
    closed: dict | None = None
    suggestion: dict | None = None


_REQUIRED = ("id", "title", "details", "project", "created", "source")


def _iso_day(value) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _record(obj, n: int) -> Record:
    def bad(why: str):
        return TodoError(f"line {n}: {why}")

    if not isinstance(obj, dict):
        raise bad("not a JSON object")
    for k in _REQUIRED:
        if k not in obj:
            raise bad(f"missing field {k}")
    for k in ("id", "title", "project", "created", "source", "status"):
        if k in obj and not isinstance(obj[k], str):
            raise bad(f"{k} must be a string")
    if not _ID.fullmatch(obj["id"]):
        raise bad("id must be 6 lowercase hex characters")
    d = obj["details"]
    if not isinstance(d, list) or not all(isinstance(x, str) for x in d):
        raise bad("details must be a list of strings")
    if obj.get("status", "open") not in STATUSES:
        raise bad(f"status must be one of {', '.join(STATUSES)}")
    dw = obj.get("done_when", [])
    if not isinstance(dw, list) or not all(isinstance(x, dict) for x in dw):
        raise bad("done_when must be a list of objects")
    for k in ("closed", "suggestion"):
        if obj.get(k) is not None and not isinstance(obj[k], dict):
            raise bad(f"{k} must be an object or null")
    status = obj.get("status", "open")
    if status == "done":
        c = obj.get("closed")
        if not isinstance(c, dict) or not _iso_day(c.get("at")):
            raise bad("a done item needs closed.at as an ISO date")
        if c.get("by") not in CLOSERS:
            raise bad(f"closed.by must be one of {', '.join(CLOSERS)}")
    if status == "suggested":
        s = obj.get("suggestion")
        if not isinstance(s, dict) or not _iso_day(s.get("at")):
            raise bad("a suggested item needs suggestion.at as an ISO date")
    known = {f.name for f in fields(Record)}
    extra = set(obj) - known
    if extra:
        raise bad(f"unknown field {sorted(extra)[0]}")
    return Record(**obj)


def loads(text: str) -> list[Record]:
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise TodoError(f"line {n}: invalid JSON ({e})") from None
        out.append(_record(obj, n))
    return out


def dumps(records: list[Record]) -> str:
    return "".join(json.dumps(asdict(r), sort_keys=True, ensure_ascii=False) + "\n"
                   for r in sorted(records, key=lambda r: r.id))


def validate_item(title: str, details: list[str], project: str,
                  checks: list[str]) -> tuple[str, list[str], list[dict]]:
    errors: list[str] = []
    try:
        title, details = validate(title, details)
    except TodoRejected as e:
        errors.extend(e.errors)
    if not _PROJECT.fullmatch(project):
        errors.append("project: must match [a-z0-9][a-z0-9._-]{0,39}")
    parsed: list[dict] = []
    for c in checks:
        m = _CHECK.fullmatch(c)
        if not m:
            errors.append(f"check {c!r}: expected pr:OWNER/REPO#N or issue:OWNER/REPO#N")
            continue
        kind, repo, num = m.groups()
        parsed.append({kind: f"{repo}#{num}", "state": "merged" if kind == "pr" else "closed"})
    if errors:
        raise TodoRejected(errors)
    return title, details, parsed


def reopen(r: Record, log: Callable[[str], None]) -> None:
    """Reopen a done item. A check-closed item loses its checks, or the nightly closes it again."""
    if (r.closed or {}).get("by") == "check" and r.done_when:
        r.done_when = []
        log(f"cleared checks on reopened item {r.id}")
    r.status = "open"
    r.closed = None


def new_id(used: set[str], gen: Callable[[], str]) -> str:
    for _ in range(100):
        i = gen()
        if i not in used:
            return i
    raise TodoError("could not generate an unused id")


def project_from_origin(url: str | None) -> str:
    m = _ORIGIN.fullmatch((url or "").strip())
    if not m:
        return "unsorted"
    name = m.group(1).lower()
    return name if _PROJECT.fullmatch(name) else "unsorted"
