"""Throttled `git pull` so a local wiki copy does not go stale."""
import os
import subprocess
from pathlib import Path


def _stamp(stamp: Path, now: float, warning: str) -> None:
    """Record this attempt (success or failure) so the next pull waits a full interval."""
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(warning, encoding="utf-8")
        os.utime(stamp, (now, now))
    except OSError:
        pass


def maybe_pull(root: Path, stamp: Path, now: float, interval: int = 300, run=None) -> str | None:
    """Pull when the stamp is missing or older than `interval` seconds.

    Every attempt touches the stamp, so an offline laptop retries at most once per interval.
    The stamp holds the last failure's warning, which is returned again until a pull succeeds.
    Returns None on success or skip after a success, else a warning string.
    """
    run = run or subprocess.run
    stamp = Path(stamp)
    try:
        if now - stamp.stat().st_mtime < interval:
            return stamp.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        pass
    try:
        proc = run(
            ["git", "-C", str(root), "pull", "--ff-only", "-q"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        warning = "wiki copy may be stale: pull timed out"
    except OSError as e:
        warning = f"wiki copy may be stale: {e}"
    else:
        if proc.returncode == 0:
            warning = ""
        else:
            reason = (proc.stderr or "").strip()[:300] or f"git exited {proc.returncode}"
            warning = f"wiki copy may be stale: {reason}"
    _stamp(stamp, now, warning)
    return warning or None
