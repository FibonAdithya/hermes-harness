"""Read Claude Code JSONL transcripts line by line."""
import json
from pathlib import Path

_NOT_OWNER_PREFIXES = ("<command-", "<local-command", "<system-reminder", "<task-notification")
_OMITTED = "[earlier turns omitted]\n"


def _events(path: Path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def _content(obj: dict):
    msg = obj.get("message")
    return msg.get("content") if isinstance(msg, dict) else None


def _blocks(content) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _owner_text(obj: dict) -> str | None:
    """The owner's text for a user line, or None when the line is not owner text."""
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    content = _content(obj)
    if isinstance(content, str):
        if content.lstrip().startswith(_NOT_OWNER_PREFIXES):
            return None
        return content
    blocks = _blocks(content)
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    texts = [b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(texts) if texts else None


def owner_messages(path: Path) -> int:
    return sum(1 for obj in _events(path) if _owner_text(obj) is not None)


def condense(path: Path, cap: int = 60000) -> str:
    parts: list[str] = []
    for obj in _events(path):
        owner = _owner_text(obj)
        if owner is not None:
            parts.append(f"OWNER: {owner}")
            continue
        if obj.get("type") != "assistant":
            continue
        content = _content(obj)
        if isinstance(content, str):
            parts.append(f"ASSISTANT: {content}")
            continue
        for b in _blocks(content):
            kind = b.get("type")
            if kind == "text" and isinstance(b.get("text"), str):
                parts.append(f"ASSISTANT: {b['text']}")
            elif kind == "tool_use":
                first = (json.dumps(b.get("input", {})).splitlines() or [""])[0][:200]
                parts.append(f"TOOL {b.get('name', '?')}: {first}")
    out = "\n".join(parts)
    if len(out) > cap:
        out = _OMITTED + out[len(out) - cap:] if cap > 0 else _OMITTED
    return out
