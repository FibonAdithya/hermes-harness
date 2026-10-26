"""Minimal frontmatter parser/dumper (stdlib only; no YAML)."""
import re

_KEY = re.compile(r"^([a-z_]+):\s*(.*)$")
_COMMENT = re.compile(r"\s+#.*$")
_MAX_LINES = 40


def _value(raw: str):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1].replace('\\"', '"')
    raw = _COMMENT.sub("", raw).strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [p.strip() for p in inner.split(",") if p.strip()] if inner else []
    return raw


def parse(text: str) -> tuple[dict | None, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, text
    end = None
    for i in range(1, min(len(lines), _MAX_LINES)):
        if lines[i].rstrip("\r\n") == "---":
            end = i
            break
    if end is None:
        return None, text
    meta: dict = {}
    for line in lines[1:end]:
        stripped = line.rstrip("\r\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        m = _KEY.match(stripped)
        if not m:
            return None, text
        meta[m.group(1)] = _value(m.group(2))
    return meta, "".join(lines[end + 1 :])


def _scalar(v: str) -> str:
    if any(c in v for c in ':#"[') or v != v.strip():
        return '"' + v.replace('"', '\\"') + '"'
    return v


def dump(meta: dict, body: str) -> str:
    out = ["---\n"]
    for key, val in meta.items():
        if isinstance(val, list):
            out.append(f"{key}: [{', '.join(str(x) for x in val)}]\n")
        else:
            out.append(f"{key}: {_scalar(str(val))}\n")
    out.append("---\n")
    return "".join(out) + body
