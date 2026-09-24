"""gpuq invocations for tig-gpu.

Two constraints are enforced here rather than trusted to the caller: gpuq is
addressed by absolute path (it is not on the PATH for a non-interactive SSH
shell), and a commit must be pinned (the runner checks out that exact tree, so
a reported number traces to a configuration someone can read back).
"""

from __future__ import annotations

import re

GPUQ_BIN = "/venv/main/bin/gpuq"
LANES = ("gpu", "cpu")
# tig-gpu has no forced command: whatever reaches ssh is run by the remote login
# shell, so a job id is validated here, the same rule fetch_artifacts applies.
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return job_id


def build_submit_argv(
    project: str,
    commit: str,
    branch: str,
    lane: str,
    artifacts: list[str],
    command: list[str],
) -> list[str]:
    if not project:
        raise ValueError("project is required")
    if not commit:
        raise ValueError("commit is required — gpuq pins the tree it runs")
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}")
    if not command:
        raise ValueError("command is required")

    argv = [GPUQ_BIN, "submit", "--project", project, "--commit", commit]
    if branch:
        argv += ["--branch", branch]
    argv += ["--lane", lane]
    for artifact in artifacts:
        argv += ["--artifact", artifact]
    argv.append("--")
    argv.extend(command)
    return argv


def build_show_argv(job_id: str) -> list[str]:
    return [GPUQ_BIN, "show", check_job_id(job_id)]


def build_logs_argv(job_id: str, lines: int) -> list[str]:
    """`gpuq show`; the tail is taken broker-side, so no pipe and no shell run remotely."""
    int(lines)
    return [GPUQ_BIN, "show", check_job_id(job_id)]


def build_list_argv() -> list[str]:
    return [GPUQ_BIN, "list"]


def build_cancel_argv(job_id: str) -> list[str]:
    return [GPUQ_BIN, "cancel", check_job_id(job_id)]
