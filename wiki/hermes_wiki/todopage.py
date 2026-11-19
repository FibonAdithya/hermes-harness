"""Render agent-todos.md from records, and read the owner's ticks back from it."""
import hashlib
import re
from datetime import date

from hermes_wiki import frontmatter
from hermes_wiki.todorecords import Record, dumps

DONE_WINDOW_DAYS = 7
_TICK = re.compile(r"^- \[([ xX])\] .* id ([0-9a-f]{6})\)$", re.MULTILINE)
_MARKER = re.compile(r"^<!-- data: ([0-9a-f]{64}) -->$", re.MULTILINE)
_NOTE = ("<!-- Generated from agent-todos.jsonl by hermes-wiki todo. "
         "Tick boxes here; other edits are lost. -->")


def _newest_first(records: list[Record]) -> list[Record]:
    by_id = sorted(records, key=lambda r: r.id)
    return sorted(by_id, key=lambda r: r.created, reverse=True)


def _latest_date(records: list[Record]) -> str | None:
    dates = []
    for r in records:
        dates.append(r.created)
        for d in (r.closed, r.suggestion):
            if d and d.get("at"):
                dates.append(d["at"])
    return max(dates) if dates else None


def _item(r: Record, box: str, with_project: bool, lead: str | None) -> list[str]:
    head = f"{r.project}, {r.created}" if with_project else r.created
    lines = [f"- [{box}] {r.title} ({head}, id {r.id})"]
    if lead:
        lines.append(f"  - {lead}")
    lines.extend(f"  - {d}" for d in r.details)
    return lines


def render(records: list[Record], today: date) -> str:
    meta = {"type": "log", "tags": ["people"]}
    latest = _latest_date(records)
    if latest:
        meta["updated"] = latest
    sha = hashlib.sha256(dumps(records).encode("utf-8")).hexdigest()
    head = frontmatter.dump(meta, f"<!-- data: {sha} -->\n{_NOTE}\n")

    groups: list[list[str]] = []
    suggested = _newest_first([r for r in records if r.status == "suggested"])
    if suggested:
        lines = ["## Probably done, confirm"]
        for r in suggested:
            s = r.suggestion or {}
            lines += _item(r, " ", True,
                           f"Looks done: {s.get('reason', '')} (curator, {s.get('at', '')})")
        groups.append(lines)

    open_ = [r for r in records if r.status == "open"]
    projects = sorted({r.project for r in open_}, key=lambda p: (p == "unsorted", p))
    for p in projects:
        lines = [f"## {p}"]
        for r in _newest_first([r for r in open_ if r.project == p]):
            lines += _item(r, " ", False, None)
        groups.append(lines)

    recent = []
    for r in records:
        if r.status != "done" or not r.closed:
            continue
        if (today - date.fromisoformat(r.closed["at"])).days < DONE_WINDOW_DAYS:
            recent.append(r)
    if recent:
        lines = ["## Recently done"]
        for r in _newest_first(recent):
            c = r.closed
            lines += _item(r, "x", True, f"Closed {c['at']} by {c.get('by', '')}: {c.get('reason', '')}")
        groups.append(lines)

    body = "\n\n".join("\n".join(g) for g in groups)
    return head + ("\n" + body + "\n" if body else "")


def data_marker(page_text: str) -> str | None:
    m = _MARKER.search(page_text)
    return m.group(1) if m else None


def ticks(page_text: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for m in _TICK.finditer(page_text):
        checked = m.group(1) in "xX"
        out[m.group(2)] = out.get(m.group(2), True) and checked
    return out
