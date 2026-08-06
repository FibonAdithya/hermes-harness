"""Thin SSH exec wrapper.

A dead box must be a clean error, never a hung agent turn — tig-gpu is a
rented vast.ai instance whose host and port change when it is recreated.
"""

from __future__ import annotations

import subprocess


class Unreachable(RuntimeError):
    pass


def run_ssh(
    target: str,
    argv: list[str],
    timeout: int = 60,
    stdin_text: str | None = None,
) -> tuple[int, str, str]:
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=accept-new",
        target,
        "--",
        *argv,
    ]
    try:
        proc = subprocess.run(
            command,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unreachable(f"{target}: timed out after {timeout}s") from exc
    except OSError as exc:
        raise Unreachable(f"{target}: {exc}") from exc
    if proc.returncode == 255:
        raise Unreachable(f"{target} unreachable: {proc.stderr.strip()[:400]}")
    return proc.returncode, proc.stdout, proc.stderr
