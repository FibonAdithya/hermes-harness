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

from . import box, gpuq
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
def run_fleet(repo: str, hours: int = 8) -> str:
    """Start a fleet night on the box against a checked-out repo. Requires a grant.

    The dollar budget comes from that repo's fleet.toml; hours is the hard stop.
    """
    require_grant(_store(), "tig-server", now=time.time())
    r = box.call(_target("tig-server"), "run_fleet", {"repo": repo, "hours": int(hours)})
    return r.get("error") or f"fleet night {r['id']} started on {repo}. Poll night_status()."


@mcp.tool()
def run_talos(challenge: str, direction: str, iterations: int = 30, backend: str = "local") -> str:
    """Start a Talos autoresearch run on the box. Requires a grant. backend is local or modal."""
    require_grant(_store(), "tig-server", now=time.time())
    r = box.call(_target("tig-server"), "run_talos",
                 {"challenge": challenge, "direction": direction, "iterations": int(iterations), "backend": backend})
    return r.get("error") or f"talos night {r['id']} started ({challenge}, {backend}). Poll night_status()."


@mcp.tool()
def run_task(repo: str, prompt: str, minutes: int = 30) -> str:
    """Run a coding task with Claude Code on the box, PR only. Requires a grant."""
    require_grant(_store(), "tig-server", now=time.time())
    _config().check_repo(repo)
    r = box.call(_target("tig-server"), "run_task", {"repo": repo, "prompt": prompt, "minutes": int(minutes)})
    return r.get("error") or f"task {r['id']} started on {repo}. Poll night_status(); read night_log('{r['id']}')."


@mcp.tool()
def add_repo(name: str) -> str:
    """Clone one of the owner's own GitHub repositories onto the box. Requires a grant."""
    require_grant(_store(), "tig-server", now=time.time())
    r = box.call(_target("tig-server"), "add_repo", {"name": name}, timeout=300)
    if "error" in r:
        return r["error"]
    return f"{r['repo']} is at {r['path']}" + (" (already there)" if r.get("already") else "")


@mcp.tool()
def night_status() -> str:
    """Every fleet, talos, and task run on the box: id, status, start time, exit code."""
    r = box.call(_target("tig-server"), "status", {}, timeout=30)
    if "error" in r:
        return r["error"]
    rows = r.get("nights", [])
    if not rows:
        return "no nights recorded"
    return "\n".join(f"{n['id']}  {n['status']:8} started {n['started']}  exit={n['exit_code']}" for n in rows)


@mcp.tool()
def night_log(night_id: str, lines: int = 80) -> str:
    """Tail of one night's log."""
    r = box.call(_target("tig-server"), "log", {"id": night_id, "lines": int(lines)}, timeout=30)
    return r.get("error") or "\n".join(r.get("lines", []))


@mcp.tool()
def list_repos() -> str:
    """Repositories checked out on the box."""
    r = box.call(_target("tig-server"), "list_repos", {}, timeout=30)
    return r.get("error") or ", ".join(r.get("repos", [])) or "none"


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
    except ValueError as exc:
        return str(exc)
    except Unreachable as exc:
        return f"tig-gpu unreachable: {exc}"
    return out or err[:400]


@mcp.tool()
def job_logs(job_id: str, lines: int = 80) -> str:
    """Tail a gpuq job's stdout/stderr via the paths gpuq show reports."""
    try:
        _, out, err = run_ssh(_target("tig-gpu"), gpuq.build_logs_argv(job_id, lines), timeout=30)
    except ValueError as exc:
        return str(exc)
    except Unreachable as exc:
        return f"tig-gpu unreachable: {exc}"
    return "\n".join((out or err[:400]).splitlines()[-int(lines):])


@mcp.tool()
def job_cancel(job_id: str) -> str:
    """Cancel a pending gpuq job. Requires a grant."""
    require_grant(_store(), "tig-gpu", now=time.time())
    try:
        _, out, err = run_ssh(_target("tig-gpu"), gpuq.build_cancel_argv(job_id), timeout=30)
    except ValueError as exc:
        return str(exc)
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
