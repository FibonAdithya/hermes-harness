"""Filtered search and read over the wiki's pages."""
from pathlib import Path

from .pages import Page, iter_pages, links, resolve, valid_name
from .schema import is_private, normal_tag


class PageNotFound(Exception):
    pass


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def _updated(meta: dict | None) -> str:
    v = (meta or {}).get("updated")
    return v if isinstance(v, str) else ""


def _passes(page: Page, tags, type, repos, updated_after) -> bool:
    if not (tags or type or repos or updated_after):
        return True
    meta = page.meta
    if meta is None:
        return False
    if tags and not set(tags) <= set(_as_list(meta.get("tags"))):
        return False
    typ = meta.get("type")
    if type and not (isinstance(typ, str) and normal_tag(typ) == normal_tag(type)):
        return False
    if repos and not set(repos) & set(_as_list(meta.get("repos"))):
        return False
    if updated_after and not (_updated(meta) and _updated(meta) >= updated_after):
        return False
    return True


def search(
    root: Path,
    query: str = "",
    tags: list[str] = (),
    type: str | None = None,
    repos: list[str] = (),
    updated_after: str | None = None,
    limit: int = 10,
    public_only: bool = False,
) -> list[dict]:
    terms = [t.lower() for t in query.split()]
    hits = []
    for page in iter_pages(root):
        if page.infra or (public_only and is_private(page.meta)):
            continue
        if not _passes(page, list(tags), type, list(repos), updated_after):
            continue
        low = page.body.lower()
        if not all(t in low for t in terms):
            continue
        count = sum(low.count(t) for t in terms)
        lines = [ln.strip() for ln in page.body.splitlines() if any(t in ln.lower() for t in terms)][:3]
        hits.append((count, page, lines))
    hits.sort(key=lambda h: h[1].name)
    hits.sort(key=lambda h: _updated(h[1].meta), reverse=True)
    hits.sort(key=lambda h: -h[0])
    out = []
    for _, page, lines in hits[: max(limit, 0)]:
        meta = page.meta or {}
        out.append(
            {
                "page": page.name,
                "type": meta.get("type"),
                "tags": _as_list(meta.get("tags")),
                "updated": meta.get("updated"),
                "lines": lines,
            }
        )
    return out


def read(root: Path, page: str, follow_links: bool = False, public_only: bool = False) -> str:
    missing = PageNotFound(f"no page {page!r}")
    if not valid_name(page):
        raise missing
    pages = iter_pages(root)
    hit = next((p for p in pages if p.name == page), None)
    if hit is None or hit.infra or (public_only and is_private(hit.meta)):
        raise missing
    text = hit.path.read_text(encoding="utf-8", errors="replace")
    if not follow_links:
        return text
    parts = [text]
    seen = {hit.name}
    for target in links(hit.body):
        if len(seen) > 5:
            break
        linked = resolve(target, pages)
        if linked is None or linked.infra or linked.name in seen:
            continue
        if public_only and is_private(linked.meta):
            continue
        seen.add(linked.name)
        excerpt = " ".join(linked.body.split()[:400])
        parts.append(f"\n\n--- linked: {linked.name} ---\n{excerpt}")
    return "".join(parts)
