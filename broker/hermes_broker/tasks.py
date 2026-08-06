"""Launching and inspecting Claude Code tasks on tig-server."""

from __future__ import annotations

import re
import secrets
import time

RUN_SCRIPT = "/opt/hermes-exec/run-task.sh"
TASK_ROOT = "/srv/hermes-tasks"
_TASK_ID = re.compile(r"^[a-z0-9-]{3,40}$")


def new_task_id(now: float | None = None) -> str:
    stamp = time.strftime("%m%d%H%M", time.gmtime(now if now is not None else time.time()))
    return f"t-{stamp}-{secrets.token_hex(3)}"


def _check(task_id: str) -> str:
    if not _TASK_ID.match(task_id):
        raise ValueError(f"invalid task id: {task_id!r}")
    return task_id


def prompt_path(task_id: str) -> str:
    return f"{TASK_ROOT}/{_check(task_id)}/prompt.txt"


def build_write_prompt_argv(task_id: str) -> list[str]:
    """Write the prompt from stdin.

    The prompt is composed by an agent that reads untrusted email, so it never
    goes on a command line where it would land in shell history or `ps`.
    """
    path = prompt_path(task_id)
    return ["sh", "-c", f"mkdir -p {TASK_ROOT}/{task_id} && cat > {path}"]


def build_launch_argv(task_id: str, repo: str, prompt_file: str) -> list[str]:
    return [RUN_SCRIPT, _check(task_id), repo, prompt_file]


def build_status_argv(task_id: str) -> list[str]:
    return ["cat", f"{TASK_ROOT}/{_check(task_id)}/status"]


def build_log_argv(task_id: str, lines: int = 80) -> list[str]:
    return ["tail", "-n", str(int(lines)), f"{TASK_ROOT}/{_check(task_id)}/log"]
