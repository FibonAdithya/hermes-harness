"""The forced-command dispatcher and the helpers every verb shares.

The broker's SSH key can run exactly one program: `dispatch`. What it asked
for arrives in SSH_ORIGINAL_COMMAND and must be a bare verb name; the verb's
arguments are one JSON object on stdin. A verb is an executable file in the
verbs directory. Anything else is refused before any lookup happens.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_STDIN = 1 << 20  # 1 MiB: a prompt, never a file upload
NIGHT_ID_RE = re.compile(r"^[a-z]{1,8}-[0-9]{8}-[0-9]{4}-[a-f0-9]{4}$")


def refuse(msg: str) -> int:
    print(json.dumps({"error": msg}))
    sys.stdout.flush()
    return 2


def read_json_stdin(stdin: BinaryIO, limit: int = MAX_STDIN) -> dict | None:
    """One JSON object, or None if the body is oversized or not an object."""
    data = stdin.read(limit + 1)
    if len(data) > limit:
        return None
    try:
        obj = json.loads(data.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def dispatch(command: str, stdin: BinaryIO, verbs_dir: Path, env: dict) -> int:
    verb = command.strip()
    if not VERB_RE.match(verb):
        return refuse("invalid verb: a bare lowercase name is required")
    path = Path(verbs_dir) / verb
    if not path.is_file() or not os.access(path, os.X_OK):
        return refuse(f"unknown verb: {verb}")
    body = stdin.read(MAX_STDIN + 1)
    if len(body) > MAX_STDIN:
        return refuse("stdin too large")
    try:
        obj = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return refuse("stdin is not valid json")
    if not isinstance(obj, dict):
        return refuse("stdin json must be an object")
    # capture_output rather than inheriting stdout: pytest's captured stdout has
    # no real file descriptor, and production and tests must share one path.
    proc = subprocess.run(
        [str(path)],
        input=json.dumps(obj).encode("utf-8"),
        env={**env, "PATH": env.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        check=False,
    )
    sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    sys.stdout.flush()
    return proc.returncode
