"""Wiki constants and note validation."""
import re
from datetime import datetime
from pathlib import Path

from . import secretscan

PAGE_TYPES = {"person", "project", "org", "commitment", "topic", "how-to", "decision", "log"}
NOTE_KINDS = {"fact", "decision", "gotcha", "how-to", "status", "person", "idea"}
SOURCES = {"claude-laptop", "claude-box", "import-claude-memory"}
PRIVATE_TYPES = {"person"}
PRIVATE_TAGS = {"finance", "investing", "health", "people"}
INFRA_PAGES = {"index", "CONVENTIONS", "TAGS"}
PAGE_WORD_CAP = 1500
NOTE_WORD_CAP = 300
MAX_TAGS = 40

_TAG_LINE = re.compile(r"^- ([a-z0-9-]+)\b")
_SUGGEST = re.compile(r"^[a-z0-9-]+$")
_REPO = re.compile(r"^[A-Za-z0-9._-]+$")


def word_count(text: str) -> int:
    return len(text.split())


def load_tags(root: Path) -> set[str]:
    try:
        text = (Path(root) / "TAGS.md").read_text(encoding="utf-8")
    except OSError:
        return set()
    tags = set()
    for line in text.splitlines():
        m = _TAG_LINE.match(line)
        if m:
            tags.add(m.group(1))
    return tags


def is_private(meta: dict | None) -> bool:
    if meta is None:
        return True
    typ = meta.get("type")
    if typ is not None and not isinstance(typ, str):
        return True
    if typ in PRIVATE_TYPES:
        return True
    tags = meta.get("tags")
    if tags is None:
        return False
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return True
    return bool(PRIVATE_TAGS & set(tags))


def _date_ok(v) -> bool:
    if not isinstance(v, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _str_list(v) -> list[str] | None:
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return v
    return None


def validate_note(meta: dict, body: str, allowed_tags: set[str]) -> list[str]:
    errors: list[str] = []
    if meta.get("kind") not in NOTE_KINDS:
        errors.append(f"kind: {meta.get('kind')!r} not one of {sorted(NOTE_KINDS)}")
    if meta.get("source") not in SOURCES:
        errors.append(f"source: {meta.get('source')!r} not one of {sorted(SOURCES)}")

    tags = _str_list(meta.get("tags"))
    if tags is None:
        errors.append("tags: must be a list of strings")
    else:
        for t in tags:
            if t not in allowed_tags:
                errors.append(f"tags: {t!r} is not in TAGS.md")

    if "suggest_tags" in meta:
        st = _str_list(meta["suggest_tags"])
        if st is None:
            errors.append("suggest_tags: must be a list of strings")
        else:
            for t in st:
                if not _SUGGEST.fullmatch(t):
                    errors.append(f"suggest_tags: {t!r} must match [a-z0-9-]+")

    if "repos" in meta:
        repos = _str_list(meta["repos"])
        if repos is None:
            errors.append("repos: must be a list of strings")
        else:
            for r in repos:
                if not _REPO.fullmatch(r) or r.strip(".") == "":
                    errors.append(f"repos: {r!r} is not a safe repo name")

    if not _date_ok(meta.get("created")):
        errors.append("created: must be a YYYY-MM-DD date")
    if "review_by" in meta and not _date_ok(meta["review_by"]):
        errors.append("review_by: must be a YYYY-MM-DD date")
    verified = meta.get("verified")
    if not isinstance(verified, str) or not verified.strip():
        errors.append("verified: required")

    n = word_count(body)
    if n == 0:
        errors.append("body: must not be empty")
    elif n > NOTE_WORD_CAP:
        errors.append(f"body: {n} words exceeds the {NOTE_WORD_CAP}-word cap")
    for sid in secretscan.find_secrets(body):
        errors.append(f"body: looks like a secret ({sid})")
    return errors
