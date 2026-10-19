"""Codes for new-reach proposals the tuner wants the owner to approve.

One code per (proposal id, hash of the exact change). The listener only records
the owner's decision in `approved_proposals.json` / `dropped_proposals.json`; the
tuner applies exactly the hashed change. The hash is computed here from the
proposal's `change`, never read from the proposal file, and must stay identical
to hermes_tuner.proposals.change_hash (the broker is a separate deployable, so
the function is duplicated; both test suites assert one shared vector).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tasks import CODE_TTL_SECONDS

SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")


def change_hash(change: dict) -> str:
    blob = json.dumps(change, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def safe_id(pid: object) -> bool:
    return isinstance(pid, str) and bool(SAFE_ID.fullmatch(pid)) and ".." not in pid


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


class ProposalStore:
    """{"codes": {code: {proposal_id, hash, expires_at}}, "settled": [{proposal_id, hash}]}"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        # Valid JSON of the wrong shape reads like a corrupt file: as empty. Raising
        # here would escape the listener's dispatch and crash-loop it.
        if not isinstance(data, dict):
            data = {}
        codes = data.get("codes")
        data["codes"] = {c: e for c, e in codes.items()
                         if isinstance(e, dict) and isinstance(e.get("expires_at"), (int, float))
                         } if isinstance(codes, dict) else {}
        if not isinstance(data.get("settled"), list):
            data["settled"] = []
        return data

    @staticmethod
    def _live(data: dict[str, Any], now: float) -> dict[str, dict]:
        return {c: e for c, e in data["codes"].items() if e["expires_at"] > now}

    def live_codes(self, now: float) -> set[str]:
        return set(self._live(self._read(), now))

    def announce(self, proposal_id: str, change_hash: str, now: float, avoid: set[str]) -> str | None:
        """A new code for this exact change, or None if it is already announced
        (live) or was already approved or dropped. An edited change (new hash)
        replaces the old code; an expired code is announced afresh."""
        data = self._read()
        if {"proposal_id": proposal_id, "hash": change_hash} in data["settled"]:
            return None
        codes = self._live(data, now)
        mine = [c for c, e in codes.items() if e["proposal_id"] == proposal_id]
        if any(codes[c]["hash"] == change_hash for c in mine):
            return None
        for c in mine:
            codes.pop(c)
        while True:
            code = f"{secrets.randbelow(10000):04d}"
            if code not in codes and code not in avoid:
                break
        codes[code] = {"proposal_id": proposal_id, "hash": change_hash,
                       "expires_at": now + CODE_TTL_SECONDS}
        data["codes"] = codes
        _atomic_write_json(self.path, data)
        return code

    def peek(self, code: str, now: float) -> dict | None:
        entry = self._live(self._read(), now).get(code)
        return None if entry is None else {"proposal_id": entry["proposal_id"], "hash": entry["hash"]}

    def take(self, code: str, now: float) -> dict | None:
        """Spend the code. The proposal may be announced again (used to give the
        code back when the announcement could not be sent)."""
        return self._pop(code, now, settle=False)

    def settle(self, code: str, now: float) -> dict | None:
        """Spend the code because the owner decided: this change is never announced again."""
        return self._pop(code, now, settle=True)

    def _pop(self, code: str, now: float, settle: bool) -> dict | None:
        data = self._read()
        entry = data["codes"].pop(code, None)
        out = None
        if entry is not None and entry["expires_at"] > now:
            out = {"proposal_id": entry["proposal_id"], "hash": entry["hash"]}
            if settle and out not in data["settled"]:
                data["settled"].append(out)
        _atomic_write_json(self.path, data)
        return out


def _record(path: Path, field: str, proposal_id: str, change_hash: str, now: float) -> None:
    path = Path(path)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        rows = []
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON; not overwriting it") from exc
    if not isinstance(rows, list):
        raise ValueError(f"{path} is not a JSON list; not overwriting it")
    if any(isinstance(r, dict) and r.get("proposal_id") == proposal_id and r.get("hash") == change_hash
           for r in rows):
        return
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows.append({"proposal_id": proposal_id, "hash": change_hash, field: stamp})
    _atomic_write_json(path, rows)


def record_approval(path: Path, proposal_id: str, change_hash: str, now: float) -> None:
    _record(path, "approved_at", proposal_id, change_hash, now)


def record_drop(path: Path, proposal_id: str, change_hash: str, now: float) -> None:
    _record(path, "dropped_at", proposal_id, change_hash, now)
