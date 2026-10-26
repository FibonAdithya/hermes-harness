"""Filtered public export of a wiki checkout: no private pages, no dangling private links."""
import os
import re
import shutil
from pathlib import Path

from .pages import iter_pages, links, resolve
from .schema import is_private

_LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_HEAD_FILE = ".source-head"


def _strip_links(text: str, visible) -> str:
    def repl(m):
        target = m.group(1).strip()
        return m.group(0) if visible(target) else target

    return _LINK.sub(repl, text)


def _unique_dir(parent: Path, base: str) -> Path:
    path = parent / base
    n = 0
    while path.exists() or path.is_symlink():
        n += 1
        path = parent / f"{base}-{n}"
    return path


def export_public(src: Path, dest: Path, head: str, now: float, keep_seconds: int = 86400) -> int:
    """Build a public copy of src and point the symlink dest at it.

    Returns the number of pages exported, or -1 when dest already holds `head`.
    """
    src, dest = Path(src), Path(dest)
    if dest.exists() and not dest.is_symlink():
        raise RuntimeError(f"{dest} is a real directory, not a symlink; refusing to replace it")
    try:
        if (dest / _HEAD_FILE).read_text(encoding="utf-8").strip() == head:
            return -1
    except OSError:
        pass

    pages = iter_pages(src)
    exported = [p for p in pages if not p.infra and not is_private(p.meta)]
    visible_pages = exported + [p for p in pages if p.infra]

    def visible(target: str) -> bool:
        return resolve(target, visible_pages) is not None

    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    new = _unique_dir(parent, f".{dest.name}.{int(now)}")
    new.mkdir()
    try:
        for p in exported:
            out = new / p.path.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_strip_links(p.path.read_text(encoding="utf-8", errors="replace"), visible),
                           encoding="utf-8")
        for name in ("TAGS", "CONVENTIONS"):
            if (src / f"{name}.md").is_file():
                shutil.copyfile(src / f"{name}.md", new / f"{name}.md")
        index = src / "index.md"
        if index.is_file():
            kept = [
                line for line in index.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                if all(visible(t) for t in links(line))
            ]
            (new / "index.md").write_text("".join(kept), encoding="utf-8")
        (new / _HEAD_FILE).write_text(head + "\n", encoding="utf-8")

        tmp_link = parent / f".{dest.name}.swap"
        if tmp_link.is_symlink():
            tmp_link.unlink()
        os.symlink(new.name, tmp_link)
        os.replace(tmp_link, dest)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise

    # Old exports stay for keep_seconds: a running task container bind-mounts one.
    for old in parent.glob(f".{dest.name}.*"):
        if old == new or old.is_symlink() or not old.is_dir():
            continue
        if now - old.stat().st_mtime > keep_seconds:
            shutil.rmtree(old, ignore_errors=True)
    return len(exported)
