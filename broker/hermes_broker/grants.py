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
        else:
            data["grants"].pop(box, None)
            data["pending"] = {
                c: r for c, r in data["pending"].items() if r["box"] != box
            }
        self._write(data)
