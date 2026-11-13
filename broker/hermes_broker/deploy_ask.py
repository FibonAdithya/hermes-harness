"""Deploy-ask state file and message format."""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

STATE_FILE = "deploy_ask.json"
SETTLE_SECONDS = 1800
ASK_TTL_SECONDS = 86400
WARN_EVERY = 3600
DEPLOY_TIMEOUT = 900
MAX_COMMIT_LINES = 15
LINE_CHARS = 100
DEPLOY_BUSY = "a deploy is already running"


class DeployAskState:
    """JSON state: {"asked_sha": ..., "deploy_running": {"sha": ..., "started_at": ...}}.

    Written by the listener's main loop and the deploy worker thread, so every
    read-modify-write holds one lock. harness-pull.sh reads deploy_running.started_at.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    def asked_sha(self) -> str | None:
        with self._lock:
            return self._read().get("asked_sha")

    def record_asked(self, sha: str) -> None:
        with self._lock:
            data = self._read()
            data["asked_sha"] = sha
            self._write(data)

    def clear_asked(self) -> None:
        with self._lock:
            data = self._read()
            data.pop("asked_sha", None)
            self._write(data)

    def deploy_running(self) -> dict | None:
        with self._lock:
            return self._read().get("deploy_running")

    def set_running(self, sha: str, started_at: float) -> None:
        with self._lock:
            data = self._read()
            data["deploy_running"] = {"sha": sha, "started_at": started_at}
            self._write(data)

    def clear_running(self) -> None:
        with self._lock:
            data = self._read()
            data.pop("deploy_running", None)
            self._write(data)


def format_deploy_ask(box_sha: str | None, master_sha: str, merged_at: float,
                      commits: list[str], ancestor: bool) -> str:
    when = datetime.datetime.fromtimestamp(merged_at, datetime.timezone.utc).strftime("%H:%M")
    if box_sha is None:
        box_line = "box:    unknown (no deploy stamp)"
    elif not ancestor:
        box_line = f"box:    {box_sha[:12]} (not an ancestor of master)"
    else:
        box_line = f"box:    {box_sha[:12]}"
    lines = ["Box is behind master.", box_line,
             f"master: {master_sha[:12]} (merged {when} UTC)"]
    if box_sha is not None and ancestor:
        n = len(commits)
        lines.append(f"{n} commit:" if n == 1 else f"{n} commits:")
        lines += [f"  {c[:LINE_CHARS]}" for c in commits[:MAX_COMMIT_LINES]]
        if n > MAX_COMMIT_LINES:
            lines.append(f"  and {n - MAX_COMMIT_LINES} more")
    lines.append(f"Read them, then reply: deploy {master_sha[:12]}")
    lines.append("Valid 24 h. Ignore it to skip.")
    return "\n".join(lines)
