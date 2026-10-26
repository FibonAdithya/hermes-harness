"""Page index: walk the wiki, parse pages, resolve links."""
import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import parse
from .schema import INFRA_PAGES, word_count

_LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_TOKEN = re.compile(r"[a-z0-9]{5,}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_SKIP_TOP = {".git", "inbox"}


@dataclass(frozen=True)
class Page:
    name: str
    path: Path
    meta: dict | None
    body: str
    words: int
    infra: bool


def valid_name(name: str) -> bool:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return False
    return ".." not in name.split("/")


def iter_pages(root: Path) -> list[Page]:
    root = Path(root)
    real_root = root.resolve()
    out = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if rel.parts[0] in _SKIP_TOP or path.is_symlink() or not path.is_file():
            continue
        if real_root not in path.resolve().parents:  # reached through a directory symlink
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = rel.with_suffix("").as_posix()
        meta, body = parse(text)
        out.append(Page(name, path, meta, body, word_count(body), name in INFRA_PAGES))
    return sorted(out, key=lambda p: p.name)


def links(body: str) -> list[str]:
    seen: list[str] = []
    for m in _LINK.finditer(body):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def resolve(name: str, pages: list[Page]) -> Page | None:
    for p in pages:
        if p.name == name:
            return p
    matches = [p for p in pages if p.name.rsplit("/", 1)[-1] == name]
    return matches[0] if len(matches) == 1 else None


def inbound(pages: list[Page]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {p.name: set() for p in pages}
    for p in pages:
        for target in links(p.body):
            hit = resolve(target, pages)
            if hit is not None:
                result[hit.name].add(p.name)
    return result


def related(text: str, pages: list[Page], k: int = 3) -> list[str]:
    tokens = set(_TOKEN.findall(text.lower()))
    scored = []
    for p in pages:
        if p.infra:
            continue
        body = p.body.lower()
        score = sum(1 for t in tokens if t in body)
        if score >= 2:
            scored.append((-score, p.name))
    scored.sort()
    return [name for _, name in scored[:k]]
