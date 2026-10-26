"""One-time import of Claude Code per-project memory files into inbox notes."""
import dataclasses
import re
from datetime import date
from pathlib import Path

from . import notes
from .config import WikiConfig
from .schema import NOTE_WORD_CAP

IMPORT_SOURCE = "import-claude-memory"
_KINDS = {"user": "person", "feedback": "decision", "project": "status", "reference": "fact"}
_TYPE = re.compile(r"^\s*type:\s*(\w+)")
_SKIP_MARKERS = ("pytest-of-", "-claude-worktrees-", "herdr-worktrees")
_WORD = re.compile(r"\S+")


def skip_project_dir(name: str) -> bool:
    return name.startswith("-tmp-") or any(m in name for m in _SKIP_MARKERS)


def _split(text: str) -> tuple[list[str], str]:
    """Return (frontmatter lines, body). No leading `---` block means no frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return [], text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return lines[1:i], "".join(lines[i + 1:])
    return [], text


def memory_kind(text: str) -> str:
    for line in _split(text)[0]:
        m = _TYPE.match(line)
        if m:
            return _KINDS.get(m.group(1), "fact")
    return "fact"


def _truncate(text: str, cap: int) -> str:
    words = list(_WORD.finditer(text))
    if len(words) <= cap:
        return text
    return text[: words[cap - 2].end()] + " [truncated]"  # the marker is one word inside the cap


def validate_only(cfg: WikiConfig, **kw) -> str:
    """Stand-in for add_note that runs every check and publishes nothing."""
    return notes.add_note(cfg, publisher=lambda *a, **k: None, **kw)


def import_claude_memory(projects: Path, cfg: WikiConfig, add=notes.add_note,
                         today: date | None = None) -> list[tuple[str, str]]:
    cfg = dataclasses.replace(cfg, source=IMPORT_SOURCE)
    results: list[tuple[str, str]] = []
    for pdir in sorted(Path(projects).iterdir()):
        if not pdir.is_dir() or skip_project_dir(pdir.name):
            continue
        for f in sorted((pdir / "memory").glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            try:
                text = f.read_text(encoding="utf-8")
                body = _truncate(f"Imported from Claude Code project {pdir.name}.\n\n" + _split(text)[1].strip(),
                                 NOTE_WORD_CAP)
                kw = dict(kind=memory_kind(text), body=body, tags=[], repos=[], verified="claim",
                          session=f"import:{pdir.name}"[:80])
                if today is not None:
                    kw["today"] = today
                results.append((str(f), add(cfg, **kw)))
            except (notes.NoteRejected, notes.PublishError) as e:
                results.append((str(f), f"skipped: {e}"))
            except (OSError, UnicodeDecodeError) as e:
                results.append((str(f), f"skipped: unreadable ({type(e).__name__})"))
    return results
