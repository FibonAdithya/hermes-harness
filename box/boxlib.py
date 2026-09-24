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
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO

VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_STDIN = 1 << 20  # 1 MiB: a prompt, never a file upload
NIGHT_ID_RE = re.compile(r"^[a-z]{1,8}-[0-9]{8}-[0-9]{4}-[a-f0-9]{4}$")
STATUSES = ("running", "done", "failed", "killed", "skipped")


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
    if not VERB_RE.fullmatch(verb):
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


# --- nights -------------------------------------------------------------------
# A night is one detached run (fleet, talos, task) under ~/nights/<id>/ with a
# status file as its record. The transient systemd unit that runs it is
# garbage-collected on exit; the directory is what outlives it.


def nights_root(env: dict | None = None) -> Path:
    home = (env or os.environ).get("HOME", "/home/adi")
    return Path(home) / "nights"


def new_night_id(kind: str, now: float | None = None) -> str:
    if not re.match(r"^[a-z]{1,8}$", kind):
        raise ValueError(f"bad kind: {kind!r}")
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(now if now is not None else time.time()))
    return f"{kind}-{stamp}-{secrets.token_hex(2)}"


def check_night_id(s: str) -> str:
    if not isinstance(s, str) or not NIGHT_ID_RE.fullmatch(s):
        raise ValueError(f"invalid night id: {s!r}")
    return s


def night_dir(root: Path, night_id: str) -> Path:
    return Path(root) / check_night_id(night_id)


def write_status(d: Path, status: str, exit_code: int | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    (Path(d) / "status").write_text(status + "\n")
    if exit_code is not None:
        (Path(d) / "exit_code").write_text(f"{int(exit_code)}\n")


def read_status(root: Path) -> list[dict]:
    rows: list[dict] = []
    root = Path(root)
    if not root.is_dir():
        return rows
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not NIGHT_ID_RE.fullmatch(d.name):
            continue
        status_file = d / "status"
        exit_file = d / "exit_code"
        rows.append({
            "id": d.name,
            "status": status_file.read_text().strip() if status_file.is_file() else "unknown",
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d.stat().st_mtime)),
            "exit_code": int(exit_file.read_text()) if exit_file.is_file() else None,
        })
    return rows


def systemd_run_argv(unit: str, runtime_sec: int, workdir: Path, env: dict, argv: list[str]) -> list[str]:
    out = [
        "systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
        f"--property=RuntimeMaxSec={int(runtime_sec)}",
        f"--property=WorkingDirectory={workdir}",
    ]
    for k, v in env.items():
        out.append(f"--setenv={k}={v}")
    out.extend(argv)
    return out
