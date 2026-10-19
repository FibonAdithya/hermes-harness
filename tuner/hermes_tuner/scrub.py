"""Scrubbed session fixtures: structure and sizes only, safe to commit to a public repo."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from .model import Session, ToolCall

VERSION = 2  # 2: token breakdown (input, cache read, cache write)


def _hash_ref(salt: bytes, ref: str) -> str:
    return hashlib.sha256(salt + ref.encode()).hexdigest()[:16]


def dump(sessions: list[Session], out: Path, salt: bytes) -> None:
    doc = {"version": VERSION, "sessions": [{
        "id": s.id, "source": s.source, "job_id": s.job_id, "started_at": s.started_at,
        "est_cost": s.est_cost, "prompt_tokens": s.prompt_tokens, "output_tokens": s.output_tokens,
        "input_tokens": s.input_tokens, "cache_read_tokens": s.cache_read_tokens,
        "cache_write_tokens": s.cache_write_tokens,
        "calls": [{"name": c.name, "arg_keys": list(c.arg_keys),
                   "ref": None if c.ref is None else _hash_ref(salt, c.ref),
                   "result_chars": c.result_chars} for c in s.calls],
    } for s in sessions]}
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    with open(out, "wb") as f:
        # mtime=0 keeps the output byte-identical across runs
        f.write(gzip.compress(raw, mtime=0))


def _session(s: dict) -> Session:
    try:  # required: zeros would price every run at its output tokens only
        breakdown = {k: s[k] for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")}
    except KeyError as exc:
        raise ValueError(f"fixture predates token breakdown (no {exc.args[0]})") from None
    return Session(
        id=s["id"], source=s["source"], job_id=s["job_id"], started_at=s["started_at"],
        est_cost=s["est_cost"], prompt_tokens=s["prompt_tokens"], output_tokens=s["output_tokens"],
        calls=tuple(ToolCall(call_id=f"{s['id']}-c{i}", name=c["name"], arg_keys=tuple(c["arg_keys"]),
                             ref=c["ref"], result_chars=c["result_chars"])
                    for i, c in enumerate(s["calls"])),
        **breakdown)


def load(path: Path) -> list[Session]:
    doc = json.loads(gzip.decompress(Path(path).read_bytes()))
    return [_session(s) for s in doc["sessions"]]
