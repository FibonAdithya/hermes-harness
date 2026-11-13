"""Deploy-ask: state file, message format, deploy runner and the announcer that asks."""
from __future__ import annotations

import datetime
import json
import os
import logging
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .grants import GrantStore

logger = logging.getLogger(__name__)

Call = Callable[..., dict]

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


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class DeployRunner:
    """Runs an approved auto-asked deploy on a worker thread, one at a time.

    The listener's poll loop must keep answering (a `revoke`, say) while the box
    deploys, so `start` returns at once and the worker sends the result later.
    """

    def __init__(self, call: Call, send: Callable[[str], bool], state: DeployAskState,
                 clock: Callable[[], float] = time.time,
                 spawn: Callable[[Callable[[], None]], None] = _spawn_thread) -> None:
        self._call, self._send, self._state = call, send, state
        self._clock, self._spawn = clock, spawn
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self._lock.locked()

    def start(self, sha: str) -> str:
        if not self._lock.acquire(blocking=False):
            return DEPLOY_BUSY
        try:
            self._state.set_running(sha, self._clock())
            self._spawn(lambda: self._work(sha))
        except BaseException:
            try:
                self._state.clear_running()
            finally:
                self._lock.release()
            raise
        return f"Deploying {sha[:12]} to tig-server. The result follows when it finishes."

    def _work(self, sha: str) -> None:
        try:
            try:
                r = self._call("deploy", {"sha": sha, "target": "harness"}, DEPLOY_TIMEOUT)
                if r.get("deployed") == sha:
                    text = f"tig-server: deployed {sha[:12]}"
                else:
                    text = f"tig-server: deploy of {sha[:12]} failed: {r.get('error', r)}"
            except Exception as exc:
                text = f"tig-server: deploy of {sha[:12]} failed: {exc}"
            try:
                self._send(text)
            except Exception:
                logger.exception("could not send deploy result")
        finally:
            try:
                self._state.clear_running()
            finally:
                self._lock.release()


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


_FULL_SHA = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT = 30
DEPLOYED_TIMEOUT = 60


class _GitFailure(Exception):
    pass


class DeployAnnouncer:
    """Asks the owner, once per master commit, to deploy master to the box when the box
    is behind. Read-only on the droplet checkout: it never fetches (harness-pull does)."""

    def __init__(self, grants: GrantStore, state: DeployAskState, runner: DeployRunner, call: Call,
                 send: Callable[[str], bool], checkout: Path,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self._grants, self._state, self._runner = grants, state, runner
        self._call, self._send, self._checkout, self._run = call, send, Path(checkout), run
        self._last_warn: float | None = None

    def _warn(self, now: float, msg: str) -> None:
        if self._last_warn is None or now - self._last_warn >= WARN_EVERY:
            self._last_warn = now
            logger.warning("deploy ask: %s", msg)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return self._run(["git", "-C", str(self._checkout), *args],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise _GitFailure(f"git {args[0]} failed: {exc}") from exc

    def _git_out(self, *args: str) -> str:
        p = self._git(*args)
        if p.returncode != 0:
            raise _GitFailure(f"git {args[0]} exited {p.returncode}: {(p.stderr or '').strip()}")
        return p.stdout

    def _box_stamp(self) -> tuple[bool, str | None]:
        """(ok, harness stamp). A reply without a valid stamp (an old box, an error) is not ok;
        {"harness": None} is ok with no stamp."""
        r = self._call("deployed", {}, DEPLOYED_TIMEOUT)
        if not isinstance(r, dict) or "error" in r or "harness" not in r:
            return False, None
        stamp = r["harness"]
        if stamp is None or (isinstance(stamp, str) and _FULL_SHA.fullmatch(stamp)):
            return True, stamp
        return False, None

    def tick(self, now: float) -> None:
        try:
            self._tick(now)
        except _GitFailure as exc:
            self._warn(now, str(exc))

    def _tick(self, now: float) -> None:
        running = self._state.deploy_running()
        if running is not None and not self._runner.busy():
            ok, stamp = self._box_stamp()
            if not ok:
                self._warn(now, "cannot read the box's deploy stamp")
                return
            sha = str(running.get("sha", "")) if isinstance(running, dict) else ""
            if stamp == sha:
                self._send(f"tig-server: deployed {sha[:12]}")
            else:
                self._send(f"deploy of {sha[:12]} did not finish; the next ask will offer it again")
                self._state.clear_asked()
            self._state.clear_running()

        master = self._git_out("rev-parse", "origin/master").strip()
        ok, box = self._box_stamp()
        if not ok:
            self._warn(now, "cannot read the box's deploy stamp")
            return
        if box == master:
            return
        if self._grants.deploy_slot_busy(now) or self._state.deploy_running():
            return
        out = self._git_out("log", "-1", "--format=%ct", "origin/master")
        try:
            ct = int(out.strip())
        except ValueError:
            raise _GitFailure(f"git log gave no commit time: {out!r}") from None
        if now - ct < SETTLE_SECONDS:
            return
        commits: list[str] = []
        ancestor = False
        if box is not None:
            # Any nonzero exit is "not an ancestor": 1, or 128 for a commit this checkout lacks.
            ancestor = self._git("merge-base", "--is-ancestor", box, master).returncode == 0
            if ancestor:
                paths = [p for p in self._git_out("diff", "--name-only", f"{box}..{master}").splitlines() if p]
                if all(p.startswith("docs/") for p in paths):
                    return
                commits = [c for c in self._git_out("log", "--format=%h %s", f"{box}..{master}").splitlines()
                           if c]
        if self._state.asked_sha() == master:
            return
        if self._grants.deploy_slot_busy(now):  # request_deploy may have landed during the git calls
            return
        self._grants.create_deploy_request(master, now, ttl_seconds=ASK_TTL_SECONDS, origin="auto")
        if self._send(format_deploy_ask(box, master, ct, commits, ancestor)):
            self._state.record_asked(master)
