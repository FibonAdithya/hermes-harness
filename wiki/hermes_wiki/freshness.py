"""Throttled `git pull` so a local wiki copy does not go stale."""
import os
import subprocess
from pathlib import Path


def maybe_pull(root: Path, stamp: Path, now: float, interval: int = 300, run=None) -> str | None:
    """Pull when the stamp is missing or older than `interval` seconds.

    Returns None on success or skip, else a warning string.
    """
    run = run or subprocess.run
    stamp = Path(stamp)
    try:
        if now - stamp.stat().st_mtime < interval:
            return None
    except OSError:
        pass
    try:
        proc = run(
            ["git", "-C", str(root), "pull", "--ff-only", "-q"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "wiki copy may be stale: pull timed out"
    except OSError as e:
        return f"wiki copy may be stale: {e}"
    if proc.returncode != 0:
        reason = (proc.stderr or "").strip() or f"git exited {proc.returncode}"
        return f"wiki copy may be stale: {reason}"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        pass
    return None
