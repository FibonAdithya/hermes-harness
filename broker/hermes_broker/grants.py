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
DEPLOY_TARGETS = ("harness", "fleet")
# "agent": requested through the broker, approved into a grant Hermes consumes.
# "auto": asked by the approvals listener itself, approved and deployed by it.
DEPLOY_ORIGINS = ("agent", "auto")


def _created_at(req: dict[str, Any]) -> float:
    """When the request was made; requests written before created_at was stored
    fall back to expires_at - REQUEST_TTL_SECONDS."""
    created = req.get("created_at")
    return float(created) if isinstance(created, (int, float)) else req["expires_at"] - REQUEST_TTL_SECONDS


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

    def create_request(self, box: str, minutes: int, reason: str, now: float,
                        avoid: set[str] | frozenset[str] = frozenset()) -> str:
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
        while True:
            code = f"{secrets.randbelow(10000):04d}"
            if code not in data["pending"] and code not in avoid:
                break
        data["pending"][code] = {
            "box": box,
            "minutes": int(minutes),
            "reason": reason,
            "created_at": now,
            "expires_at": now + REQUEST_TTL_SECONDS,
        }
        self._write(data)
        return code

    def pending_reason(self, code: str) -> str | None:
        req = self._read()["pending"].get(code)
        return req["reason"] if req else None

    def pending_codes(self, now: float) -> set[str]:
        return {c for c, r in self._read()["pending"].items() if r["expires_at"] > now}

    def approve(self, code: str, now: float) -> str | None:
        """`now` is when the owner sent the reply (the message date). A reply sent
        before the request existed cannot be approving it, whatever its digits:
        refuse it and leave the request pending for the real reply."""
        data = self._read()
        req = data["pending"].get(code)
        if req is not None and now < _created_at(req):
            return None
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

    def create_deploy_request(self, sha: str, now: float, target: str = "harness", *,
                              ttl_seconds: float = REQUEST_TTL_SECONDS,
                              origin: str = "agent") -> None:
        if target not in DEPLOY_TARGETS:
            raise ValueError(f"unknown deploy target: {target}")
        if origin not in DEPLOY_ORIGINS:
            raise ValueError(f"unknown deploy origin: {origin}")
        data = self._read()
        data["pending_deploy"] = {"sha": sha, "target": target, "origin": origin,
                                  "created_at": now, "expires_at": now + ttl_seconds}
        self._write(data)

    def pending_deploy_origin(self, now: float) -> str | None:
        req = self._read().get("pending_deploy")
        if req is None or req["expires_at"] <= now:
            return None
        return req.get("origin", "agent")

    def deploy_slot_busy(self, now: float) -> bool:
        """True if an unexpired non-auto pending deploy or a live deploy grant (either
        target) exists; an auto-ask must not displace either."""
        data = self._read()
        req = data.get("pending_deploy")
        if req is not None and req["expires_at"] > now and req.get("origin", "agent") != "auto":
            return True
        grant = data["grants"].get(DEPLOY)
        return grant is not None and grant["expires_at"] > now

    def _take_pending_deploy(self, data: dict[str, Any], prefix: str, now: float,
                             target: str, origin: str) -> dict[str, Any] | None:
        """The pending deploy request, popped from `data` (the caller writes), if it is
        live, of this origin, for this target, and `prefix` starts its commit. Expiry pops
        it and writes; any other mismatch -- reply sent before the request existed, wrong
        target, wrong prefix -- leaves it pending."""
        req = data.get("pending_deploy")
        if req is None:
            return None
        if req["expires_at"] <= now:
            data.pop("pending_deploy")
            self._write(data)
            return None
        if now < _created_at(req):  # sent before the request existed (see approve)
            return None
        if req.get("origin", "agent") != origin:
            return None
        if req.get("target", "harness") != target:
            return None
        prefix = prefix.lower()
        if len(prefix) < MIN_SHA_PREFIX or not req["sha"].startswith(prefix):
            return None
        return data.pop("pending_deploy")

    def approve_deploy(self, prefix: str, now: float, target: str = "harness") -> str | None:
        """Grant the pending agent deploy if `prefix` starts its commit and `target` matches the
        request's own target. A mismatch on either -- wrong prefix, or an owner naming the
        wrong target -- keeps the request pending rather than granting it."""
        data = self._read()
        req = self._take_pending_deploy(data, prefix, now, target, "agent")
        if req is None:
            return None
        data["grants"][DEPLOY] = {"sha": req["sha"], "target": req.get("target", "harness"),
                                   "expires_at": now + DEPLOY_GRANT_MINUTES * 60}
        self._write(data)
        return req["sha"]

    def approve_auto_deploy(self, prefix: str, now: float, target: str = "harness") -> str | None:
        """Approve the pending auto-ask like approve_deploy, but write no grant: the
        caller deploys the returned commit itself."""
        data = self._read()
        req = self._take_pending_deploy(data, prefix, now, target, "auto")
        if req is None:
            return None
        self._write(data)
        return req["sha"]

    def deploy_target(self) -> str | None:
        grant = self._read()["grants"].get(DEPLOY)
        return grant.get("target", "harness") if grant else None

    def take_deploy_grant(self, now: float, target: str = "harness") -> str | None:
        """The approved commit, if a deploy grant for this target is live. Consumes it only on a match."""
        data = self._read()
        grant = data["grants"].get(DEPLOY)
        if grant is None or grant.get("target", "harness") != target:
            return None
        data["grants"].pop(DEPLOY)
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
