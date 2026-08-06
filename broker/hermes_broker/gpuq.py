"""gpuq invocations for tig-gpu.

Two constraints are enforced here rather than trusted to the caller: gpuq is
addressed by absolute path (it is not on the PATH for a non-interactive SSH
shell), and a commit must be pinned (the runner checks out that exact tree, so
a reported number traces to a configuration someone can read back).
"""

from __future__ import annotations

GPUQ_BIN = "/venv/main/bin/gpuq"
LANES = ("gpu", "cpu")


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
    return [GPUQ_BIN, "show", job_id]


def build_list_argv() -> list[str]:
    return [GPUQ_BIN, "list"]


def build_cancel_argv(job_id: str) -> list[str]:
    return [GPUQ_BIN, "cancel", job_id]
