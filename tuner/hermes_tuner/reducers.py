"""Deterministic result-reducer transforms.

Stdlib only, plus ``hermes_tuner.policy``: the Task 7 plugin imports this
module inside Hermes's process. No model, network or clock.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from . import policy

_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_QUOTE_HDR_RE = re.compile(r"^On .{0,200} wrote:$")
_FOOTER_MARKERS = (
    "unsubscribe", "view in browser", "manage preferences",
    "you are receiving this", "privacy policy",
)
_FOOTER_FROM = 0.6
_CUT_MARK = " [cut]"
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "pre", "hr", "section", "article",
    "header", "footer", "td", "th",
})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(text: str, params: dict) -> str:
    p = _TextExtractor()
    p.feed(text)
    p.close()
    lines = (ln.strip() for ln in "".join(p.parts).split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _host_kept(host: str, keep: set[str]) -> bool:
    return any(host == k or host.endswith("." + k) for k in keep)


def shorten_urls(text: str, params: dict) -> str:
    keep = {h.lower() for h in params.get("keep_hosts", [])} | set(policy.MEETING_HOSTS)

    def repl(m: re.Match) -> str:
        try:
            host = urlsplit(m.group(0)).hostname
        except ValueError:
            return m.group(0)
        if not host or _host_kept(host, keep):
            return m.group(0)
        return host

    return _URL_RE.sub(repl, text)


def drop_quoted(text: str, params: dict) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        if _QUOTE_HDR_RE.match(line.rstrip("\r\n")):
            break
        if line.startswith(">"):
            continue
        out.append(line)
    return "".join(out)


def drop_footer(text: str, params: dict) -> str:
    start = len(text) * _FOOTER_FROM
    pos = 0
    for line in text.splitlines(keepends=True):
        if pos >= start:
            low = line.lower()
            if any(m in low for m in _FOOTER_MARKERS):
                return text[:pos]
        pos += len(line)
    return text


def collapse_ws(text: str, params: dict) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cap(text: str, params: dict) -> str:
    n = params["chars"]
    return text if len(text) <= n else text[:n] + _CUT_MARK


OPS = {
    "html_to_text": html_to_text,
    "shorten_urls": shorten_urls,
    "drop_quoted": drop_quoted,
    "drop_footer": drop_footer,
    "collapse_ws": collapse_ws,
    "cap": cap,
}
_ALLOWED_PARAMS = {
    "html_to_text": {}, "shorten_urls": {"keep_hosts"}, "drop_quoted": {},
    "drop_footer": {}, "collapse_ws": {}, "cap": {"chars"},
    "drop_fields": {"fields"},
}


def validate_spec(spec: object) -> list[str]:
    if not isinstance(spec, dict):
        return ["spec is not an object"]
    errs: list[str] = []
    if spec.get("tool") not in policy.REDUCER_TOOLS:
        errs.append("tool not allowed")
    if spec.get("job") not in policy.REDUCER_JOBS:
        errs.append("job not a reducer job")
    steps = spec.get("steps")
    if not isinstance(steps, list):
        errs.append("steps is not a list")
        return errs
    seen: set[str] = set()
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            errs.append(f"step {i} is not an object")
            continue
        op = st.get("op")
        if not isinstance(op, str) or op not in _ALLOWED_PARAMS:
            errs.append(f"step {i}: unknown op")
            continue
        if op in seen:
            errs.append(f"step {i}: repeated op {op}")
        seen.add(op)
        for k in st:
            if k != "op" and k not in _ALLOWED_PARAMS[op]:
                errs.append(f"step {i}: unknown param {k}")
        if op == "cap":
            c = st.get("chars")
            if isinstance(c, bool) or not isinstance(c, int) or c not in policy.REDUCER_CAP_LADDER:
                errs.append(f"step {i}: chars not in ladder")
        elif op in ("shorten_urls", "drop_fields"):
            key = "keep_hosts" if op == "shorten_urls" else "fields"
            v = st.get(key)
            if v is None and op == "shorten_urls":
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                errs.append(f"step {i}: {key} must be a list of strings")
    return errs


def _walk(obj, fn):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    return obj


def _parse(result: str):
    try:
        return True, json.loads(result)
    except ValueError:
        return False, None


def apply_reducer(spec: dict, result: str) -> str:
    """Apply a reducer spec to a tool result. Never raises."""
    try:
        if not isinstance(result, str):
            return result
        steps = spec["steps"]
        ok, obj = _parse(result)
        if ok:
            for st in steps:
                op, params = st["op"], st
                if op == "drop_fields":
                    if isinstance(obj, dict):
                        drop = set(params["fields"])
                        obj = {k: v for k, v in obj.items() if k not in drop}
                else:
                    f = OPS[op]
                    obj = _walk(obj, lambda s, f=f, p=params: f(s, p))
            if isinstance(obj, (dict, list)) and not obj:
                return result
            out = json.dumps(obj, ensure_ascii=False)
            if obj == json.loads(result):
                return result
        else:
            out = result
            for st in steps:
                if st["op"] != "drop_fields":
                    out = OPS[st["op"]](out, st)
        if result and not out:
            return result
        return out
    except Exception:
        return result


def text_view(result: str) -> str:
    """What the model reads: string leaves of a JSON result (keys sorted)."""
    ok, obj = _parse(result)
    if not ok:
        return result
    leaves: list[str] = []

    def walk(o):
        if isinstance(o, str):
            leaves.append(o)
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for k in sorted(o):
                walk(o[k])

    walk(obj)
    return "\n".join(leaves)
