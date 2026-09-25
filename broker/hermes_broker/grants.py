"""Grant and pending-request state for gated broker tools.

One JSON file, owner-only. All times are epoch seconds: the host runs BST,
the agent sandbox runs UTC, and tig-gpu has its own container clock.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

BOXES: tuple[str, ...] = ("tig-gpu", "tig-server")
REQUEST_TTL_SECONDS = 120
MAX_GRANT_MINUTES = 480
# A deploy grant is not a box: it names one hermes-harness commit, is approved
# by the owner typing that commit's prefix, and is consumed by a single deploy.
DEPLOY = "deploy"
DEPLOY_GRANT_MINUTES = 10
MIN_SHA_PREFIX = 7


class GrantStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---- persistence -------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"pending": {}, "grants": {}}
        data.setdefault("pending", {})
        data.setdefault("grants", {})
        return data

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

    # ---- requests ----------------------------------------------------

    def create_request(self, box: str, minutes: int, reason: str, now: float) -> str:
        if box not in BOXES:
            raise ValueError(f"unknown box: {box}")
        if not 1 <= int(minutes) <= MAX_GRANT_MINUTES:
            raise ValueError(f"minutes must be 1..{MAX_GRANT_MINUTES}")

        data = self._read()
        data["pending"] = {
            code: req
            for code, req in data["pending"].items()
            if req["box"] != box and req["expires_at"] > now
        }
        code = f"{secrets.randbelow(10000):04d}"
        data["pending"][code] = {
            "box": box,
            "minutes": int(minutes),
            "reason": reason,
            "expires_at": now + REQUEST_TTL_SECONDS,
        }
        self._write(data)
        return code

    def pending_reason(self, code: str) -> str | None:
        req = self._read()["pending"].get(code)
        return req["reason"] if req else None

    def approve(self, code: str, now: float) -> str | None:
        data = self._read()
        req = data["pending"].pop(code, None)
        if req is None:
            self._write(data)
            return None
        if req["expires_at"] <= now:
            self._write(data)
            return None
        data["grants"][req["box"]] = {"expires_at": now + req["minutes"] * 60}
        self._write(data)
        return req["box"]

    # ---- deploys -----------------------------------------------------

    def create_deploy_request(self, sha: str, now: float) -> None:
        data = self._read()
        data["pending_deploy"] = {"sha": sha, "expires_at": now + REQUEST_TTL_SECONDS}
        self._write(data)

    def approve_deploy(self, prefix: str, now: float) -> str | None:
        """Grant the pending deploy if `prefix` starts its commit. A mismatch keeps it pending."""
        data = self._read()
        req = data.get("pending_deploy")
        if req is None:
            return None
        if req["expires_at"] <= now:
            data.pop("pending_deploy")
            self._write(data)
            return None
        prefix = prefix.lower()
        if len(prefix) < MIN_SHA_PREFIX or not req["sha"].startswith(prefix):
            return None
        data.pop("pending_deploy")
        data["grants"][DEPLOY] = {"sha": req["sha"], "expires_at": now + DEPLOY_GRANT_MINUTES * 60}
        self._write(data)
        return req["sha"]

    def take_deploy_grant(self, now: float) -> str | None:
        """The approved commit, if a deploy grant is live. Consumes the grant either way."""
        data = self._read()
        grant = data["grants"].pop(DEPLOY, None)
        if grant is None:
            return None
        self._write(data)
        return grant["sha"] if grant["expires_at"] > now else None

    # ---- grants ------------------------------------------------------

    def expires_at(self, box: str) -> float | None:
        grant = self._read()["grants"].get(box)
        return grant["expires_at"] if grant else None

    def is_active(self, box: str, now: float) -> bool:
        expiry = self.expires_at(box)
        return expiry is not None and expiry > now

    def revoke(self, box: str | None) -> None:
        data = self._read()
        if box is None:
            data["grants"] = {}
            data["pending"] = {}
            data.pop("pending_deploy", None)
        else:
            data["grants"].pop(box, None)
            data["pending"] = {
                c: r for c, r in data["pending"].items() if r["box"] != box
            }
        self._write(data)
