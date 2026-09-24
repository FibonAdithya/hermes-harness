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


# --- gated verbs: what each one launches -------------------------------------

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# The challenges the pinned Talos knows. Extend by PR when Talos does.
TALOS_CHALLENGES = ("knapsack", "vehicle_routing", "satisfiability", "vector_search", "hypergraph", "neuralnet_optimizer")
TALOS_BACKENDS = ("local", "modal")
MAX_FLEET_HOURS = 24
MAX_TALOS_ITER = 500
MAX_TASK_MINUTES = 240


def _name(s: str) -> str:
    if not isinstance(s, str) or not NAME_RE.fullmatch(s) or s in (".", ".."):
        raise ValueError(f"invalid name: {s!r}")
    return s


def fleet_command(home: Path, repo: str, hours: int) -> tuple[Path, list[str], int]:
    repo = _name(repo)
    if not 1 <= int(hours) <= MAX_FLEET_HOURS:
        raise ValueError(f"hours must be 1..{MAX_FLEET_HOURS}")
    home = Path(home)
    workdir = home / "TIG" / repo
    argv = ["uv", "run", "--project", str(home / "TIG" / "fleet"), "fleet", "run", "--new-run"]
    return workdir, argv, int(hours) * 3600


def talos_command(home: Path, challenge: str, direction: str, iterations: int, backend: str) -> tuple[Path, list[str], int]:
    if challenge not in TALOS_CHALLENGES:
        raise ValueError(f"unknown challenge: {challenge!r}")
    if backend not in TALOS_BACKENDS:
        raise ValueError(f"unknown backend: {backend!r}")
    if not isinstance(direction, str) or not direction.strip():
        raise ValueError("direction is required")
    if not 1 <= int(iterations) <= MAX_TALOS_ITER:
        raise ValueError(f"iterations must be 1..{MAX_TALOS_ITER}")
    workdir = Path(home) / f"talos-{backend}"
    argv = [str(workdir / ".venv" / "bin" / "talos"), "run", "--challenge", challenge,
            "--direction", direction, "--budget-iterations", str(int(iterations)), "--yes"]
    return workdir, argv, 12 * 3600


def add_repo_check(owner_login: str, repo_json: dict, name: str) -> str | None:
    try:
        _name(name)
    except ValueError as exc:
        return str(exc)
    if repo_json.get("fork"):
        return f"{name} is a fork; only repositories you own outright are cloned"
    if (repo_json.get("owner") or {}).get("login") != owner_login:
        return f"{name} is not owned by {owner_login}"
    return None


def task_command(home: Path, night_dir_: Path, repo: str, minutes: int) -> tuple[Path, list[str], int]:
    if not isinstance(repo, str) or not SLUG_RE.fullmatch(repo):
        raise ValueError(f"repo must be owner/name: {repo!r}")
    if not 1 <= int(minutes) <= MAX_TASK_MINUTES:
        raise ValueError(f"minutes must be 1..{MAX_TASK_MINUTES}")
    argv = [str(Path(home) / ".local" / "bin" / "run-task.sh"), str(night_dir_), repo, str(int(minutes))]
    return Path(night_dir_), argv, int(minutes) * 60 + 300


def start_night(kind: str, workdir: Path, argv: list[str], runtime_sec: int, env: dict,
                meta: dict | None = None, night_id: str | None = None) -> str:
    """Mint an id (unless given), create the directory, hand the command to systemd.

    Returns the id. On a systemd-run failure the night is recorded as failed with
    the reason in its log, and RuntimeError is raised for the caller to report.
    """
    root = nights_root(env)
    night_id = night_id or new_night_id(kind)
    d = night_dir(root, night_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"kind": kind, "argv": argv, "workdir": str(workdir), **(meta or {})}))
    write_status(d, "running")
    home = env.get("HOME", "/home/adi")
    wrapper = str(Path(home) / ".local" / "bin" / "run-night.sh")
    full = systemd_run_argv(f"night-{night_id}", runtime_sec, workdir, env, [wrapper, str(d), *argv])
    proc = subprocess.run(full, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        write_status(d, "failed", exit_code=proc.returncode)
        with (d / "log").open("a") as fh:
            fh.write(f"systemd-run failed: {proc.stderr}\n")
        raise RuntimeError(f"systemd-run failed: {proc.stderr.strip()[:300]}")
    return night_id
