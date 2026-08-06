"""MCP stdio server: the agent's only path to compute.

The agent calls these tools; the SSH keys stay in this process. Gated tools
refuse to act without a live grant issued out-of-band by the owner.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

# mcp 2.x renamed FastMCP to MCPServer; the decorator and run() API are the same.
from mcp.server import MCPServer

from . import gpuq, tasks
from .config import BrokerConfig, load_config
from .grants import GrantStore
from .ssh import Unreachable, run_ssh

CONFIG_PATH = Path(
    os.environ.get("HERMES_BROKER_CONFIG", "/home/hermes/.hermes/broker/broker.json")
)
# The sandbox's /root is a bind mount of this directory (spec §7.3 of the
# assistant design); writing here is how the agent receives files.
SANDBOX_HOME = Path("/home/hermes/.hermes/sandboxes/docker/default/home")

mcp = MCPServer("compute")

_cfg: BrokerConfig | None = None
_store_cache: GrantStore | None = None


# Config is loaded on first tool call, not at import. Importing this module
# must not require the droplet's filesystem — the gate is unit-tested on a
# laptop that has no broker.json.
def _config() -> BrokerConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config(CONFIG_PATH)
    return _cfg


def _store() -> GrantStore:
    global _store_cache
    if _store_cache is None:
        _store_cache = GrantStore(_config().state_dir / "grants.json")
    return _store_cache


class Locked(RuntimeError):
    pass


def require_grant(store: GrantStore, box: str, now: float) -> None:
    if not store.is_active(box, now):
        raise Locked(f"LOCKED: no active grant for {box}. Call request_access first.")


def _target(box: str) -> str:
    return _config().ssh_targets[box]


@mcp.tool()
def request_access(box: str, minutes: int, reason: str) -> str:
    """Ask the owner for time-boxed access to a compute box.

    Returns a 4-digit code. Tell the owner the box, the minutes, the reason, and
    the code. They approve in the approvals chat. This grants nothing by itself.
    """
    code = _store().create_request(box, minutes, reason, now=time.time())
    return (
        f"Requested {box} for {minutes} minutes.\n"
        f"Ask the owner to reply `approve {code}` in the approvals chat "
        f"within 2 minutes. Reason shown to them: {reason}"
    )


@mcp.tool()
def run_task(repo: str, prompt: str, minutes: int = 30) -> str:
    """Run a coding task with Claude Code on tig-server. Requires a grant."""
    require_grant(_store(), "tig-server", now=time.time())
    _config().check_repo(repo)
    task_id = tasks.new_task_id()
    target = _target("tig-server")
    try:
        code, _, err = run_ssh(
            target, tasks.build_write_prompt_argv(task_id), timeout=30, stdin_text=prompt
        )
        if code != 0:
            return f"failed to stage prompt: {err.strip()[:400]}"
        # 60s is correct and deliberate: run-task.sh backgrounds the container
        # and returns immediately. If launches start timing out, the bug is in
        # the script's backgrounding, not here.
        code, out, err = run_ssh(
            target,
            tasks.build_launch_argv(task_id, repo, tasks.prompt_path(task_id)),
            timeout=60,
        )
    except Unreachable as exc:
        return f"tig-server unreachable: {exc}"
    if code != 0:
        return f"launch failed: {err.strip()[:400]}"
    return f"task {task_id} started on {repo}. Poll task_status('{task_id}')."


@mcp.tool()
def task_status(task_id: str) -> str:
    """Status of a task this broker started: running, done, or failed."""
    try:
        _, out, err = run_ssh(_target("tig-server"), tasks.build_status_argv(task_id), timeout=30)
    except Unreachable as exc:
        return f"tig-server unreachable: {exc}"
    return out.strip() or err.strip()[:400] or "unknown"


@mcp.tool()
def task_log(task_id: str, lines: int = 80) -> str:
    """Tail of a task's log."""
    try:
        _, out, err = run_ssh(
            _target("tig-server"), tasks.build_log_argv(task_id, lines), timeout=30
        )
    except Unreachable as exc:
        return f"tig-server unreachable: {exc}"
    return out or err[:400]


@mcp.tool()
def submit_job(
    project: str,
    commit: str,
    command: list[str],
    lane: str = "gpu",
    branch: str = "",
    artifacts: list[str] | None = None,
) -> str:
    """Submit a job to the gpuq queue on tig-gpu. Requires a grant.

    The commit must already be pushed — gpuq checks out that exact tree.
    """
    require_grant(_store(), "tig-gpu", now=time.time())
    argv = gpuq.build_submit_argv(project, commit, branch, lane, artifacts or [], command)
    try:
        code, out, err = run_ssh(_target("tig-gpu"), argv, timeout=60)
    except Unreachable as exc:
        return f"tig-gpu unreachable: {exc}"
    return out.strip() if code == 0 else f"submit failed: {err.strip()[:400]}"


@mcp.tool()
def job_status(job_id: str) -> str:
    """Show one gpuq job."""
    try:
        _, out, err = run_ssh(_target("tig-gpu"), gpuq.build_show_argv(job_id), timeout=30)
    except Unreachable as exc:
        return f"tig-gpu unreachable: {exc}"
    return out or err[:400]


@mcp.tool()
def job_logs(job_id: str, lines: int = 80) -> str:
    """Tail a gpuq job's stdout/stderr via the paths gpuq show reports."""
    try:
        _, out, err = run_ssh(
            _target("tig-gpu"),
            ["sh", "-c", f"{gpuq.GPUQ_BIN} show {job_id} | tail -n {int(lines)}"],
            timeout=30,
        )
    except Unreachable as exc:
        return f"tig-gpu unreachable: {exc}"
    return out or err[:400]


@mcp.tool()
def job_cancel(job_id: str) -> str:
    """Cancel a pending gpuq job. Requires a grant."""
    require_grant(_store(), "tig-gpu", now=time.time())
    try:
        _, out, err = run_ssh(_target("tig-gpu"), gpuq.build_cancel_argv(job_id), timeout=30)
    except Unreachable as exc:
        return f"tig-gpu unreachable: {exc}"
    return out or err[:400]


@mcp.tool()
def fetch_artifacts(job_id: str) -> str:
    """Copy a finished job's artifacts into the agent's sandbox workspace.

    The agent has no network, so the broker does the copy. Files land in the
    sandbox bind mount, and ownership is fixed by a throwaway root container —
    the agent runs as container-root, so host-user-owned files are unwritable
    to it (the failure recorded in spec §7.5).
    """
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", job_id):
        return f"invalid job id: {job_id!r}"

    dest = SANDBOX_HOME / "artifacts" / job_id
    dest.mkdir(parents=True, exist_ok=True)
    target = _target("tig-gpu")
    remote = f"/workspace/queue/artifacts/{job_id}/"
    try:
        proc = subprocess.run(
            ["scp", "-r", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             f"{target}:{remote}", str(dest)],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"artifact copy failed: {exc}"
    if proc.returncode != 0:
        return f"artifact copy failed: {proc.stderr.strip()[:400]}"

    subprocess.run(
        ["docker", "run", "--rm", "--network=none",
         "-v", f"{SANDBOX_HOME}:/root", "hermes-sandbox:latest",
         "chown", "-R", "0:0", f"/root/artifacts/{job_id}"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    return f"artifacts for {job_id} copied to /root/artifacts/{job_id} in your workspace"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
