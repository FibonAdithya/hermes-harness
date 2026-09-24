"""The verb protocol to the TIG box.

The broker's key on the box is bound to a forced command, so the only thing
that can be sent is a bare verb name; everything else travels as one JSON
object on stdin. Arguments composed by an agent that reads untrusted email
therefore never touch a command line or a shell.
"""

from __future__ import annotations

import json
import re

from .ssh import Unreachable, run_ssh

VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def call(target: str, verb: str, args: dict, timeout: int = 60) -> dict:
    if not VERB_RE.fullmatch(verb):
        raise ValueError(f"invalid verb: {verb!r}")
    try:
        code, out, err = run_ssh(target, [verb], timeout=timeout, stdin_text=json.dumps(args))
    except Unreachable as exc:
        return {"error": f"{target} unreachable: {exc}"}
    try:
        reply = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        return {"error": f"{verb} returned no json (exit {code}): {(err or out).strip()[:300]}"}
    if not isinstance(reply, dict):
        return {"error": f"{verb} returned {type(reply).__name__}, not an object"}
    if code != 0 and "error" not in reply:
        reply["error"] = f"{verb} exited {code}: {err.strip()[:300]}"
    return reply
