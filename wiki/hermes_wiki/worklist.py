"""Deterministic worklist for the nightly curator: what to file, split, review and fix."""
from collections import Counter
from datetime import date
from pathlib import Path

from . import pages as pagelib
from . import schema, secretscan
from .frontmatter import parse

_LAST = "￿"  # notes with no usable `created` sort after dated ones


def _notes(root: Path, all_pages, tags: set[str]) -> list[dict]:
    out = []
    inbox = root / "inbox"
    for path in inbox.rglob("*.md") if inbox.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse(text)
        rel = path.relative_to(root).as_posix()
        if meta is None:
            meta, invalid = {}, ["no frontmatter"]
        else:
            invalid = schema.validate_note(meta, body, tags)
        out.append({"path": rel, "meta": meta, "body": body,
                    "related": pagelib.related(body, all_pages), "invalid": invalid})
    return out


def _created(note: dict) -> str:
    c = note["meta"].get("created")
    return c if isinstance(c, str) and c else _LAST


def _list(v) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _lint(all_pages, tags: set[str]) -> list[dict]:
    out = []
    by_name = {p.name: p for p in all_pages}
    indexed = set()
    index = by_name.get("index")
    if index is not None:
        for target in pagelib.links(index.body):
            hit = pagelib.resolve(target, all_pages)
            if hit is None:
                out.append({"page": "index", "problem": f"index line points to missing page: {target}"})
            else:
                indexed.add(hit.name)
    for p in all_pages:
        if p.meta is not None:
            for t in _list(p.meta.get("tags")):
                if t not in tags:
                    out.append({"page": p.name, "problem": f"unknown tag: {t}"})
        if p.name != "index":
            for target in pagelib.links(p.body):
                if pagelib.resolve(target, all_pages) is None:
                    out.append({"page": p.name, "problem": f"broken link: [[{target}]]"})
        if not p.infra and p.name not in indexed:
            out.append({"page": p.name, "problem": "missing from index.md"})
        for sid in secretscan.find_secrets(p.body):
            out.append({"page": p.name, "problem": f"secret pattern: {sid}"})
    return out


def build(root: Path, today: date, note_limit: int = 30, backfill_limit: int = 10) -> dict:
    root = Path(root)
    all_pages = pagelib.iter_pages(root)
    tags = schema.load_tags(root)
    cap = schema.PAGE_WORD_CAP

    notes = sorted(_notes(root, all_pages, tags), key=lambda n: (_created(n), n["path"]))
    content = [p for p in all_pages if not p.infra]

    over_cap = [{"page": p.name, "words": p.words} for p in content
                if p.meta is not None and p.meta.get("type") != "log" and p.words > cap]
    cutoff = today.isoformat()
    past_review = []
    for p in content:
        rb = p.meta.get("review_by") if p.meta else None
        if isinstance(rb, str) and rb < cutoff:
            past_review.append({"page": p.name, "review_by": rb})
    nofm = sorted((p for p in content if p.meta is None), key=lambda p: (0, -p.words, p.name) if p.words > cap else (1, 0, p.name))

    inbound = pagelib.inbound(all_pages)
    infra = {p.name for p in all_pages if p.infra}
    orphans = [p.name for p in content if not (inbound[p.name] - infra - {p.name})]

    proposals = Counter(t for n in notes for t in _list(n["meta"].get("suggest_tags")))
    return {
        "notes": notes[:note_limit],
        "notes_remaining": max(0, len(notes) - note_limit),
        "over_cap": over_cap,
        "past_review": past_review,
        "backfill": [p.name for p in nofm[:backfill_limit]],
        "orphans": orphans,
        "tag_proposals": dict(sorted(proposals.items())),
        "lint": _lint(all_pages, tags),
    }
