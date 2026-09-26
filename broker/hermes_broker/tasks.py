"""Codes for announced fleet tasks.

One code per triage issue, bound to the hash of the title and body the owner was
shown. Codes are separate from grant codes (grants.py) but drawn from the same
four-digit space, so each store avoids the other's live codes.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

CODE_TTL_SECONDS = 7 * 24 * 3600


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"codes": {}}
        data.setdefault("codes", {})
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

    @staticmethod
    def _live(data: dict[str, Any], now: float) -> dict[str, dict]:
        return {c: e for c, e in data["codes"].items() if e["expires_at"] > now}

    def live_codes(self, now: float) -> set[str]:
        return set(self._live(self._read(), now))

    def announce(self, repo: str, slug: str, number: int, body_hash: str, now: float, avoid: set[str]) -> str | None:
        """A new code for this issue, or None if it is already announced with this text."""
        data = self._read()
        codes = self._live(data, now)
        mine = [c for c, e in codes.items() if e["repo"] == repo and e["number"] == int(number)]
        if any(codes[c]["hash"] == body_hash for c in mine):
            return None
        for c in mine:
            codes.pop(c)
        while True:
            code = f"{secrets.randbelow(10000):04d}"
            if code not in codes and code not in avoid:
                break
        codes[code] = {"repo": repo, "slug": slug, "number": int(number), "hash": body_hash,
                       "expires_at": now + CODE_TTL_SECONDS}
        data["codes"] = codes
        self._write(data)
        return code

    def take(self, code: str, now: float) -> dict | None:
        data = self._read()
        entry = data["codes"].pop(code, None)
        self._write(data)
        if entry is None or entry["expires_at"] <= now:
            return None
        return {k: entry[k] for k in ("repo", "slug", "number", "hash")}

    def prune(self, listed_repos: set[str], present: set[tuple[str, int]], now: float) -> None:
        """Forget codes for issues that left triage. Only repos whose listing succeeded are judged."""
        data = self._read()
        data["codes"] = {c: e for c, e in self._live(data, now).items()
                         if e["repo"] not in listed_repos or (e["repo"], e["number"]) in present}
        self._write(data)
