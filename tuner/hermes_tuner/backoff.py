"""Back-off from repeated identical outcomes, and the drafting history.

A candidate whose last two drafts ended the same way (same kind, same reason class)
is skipped for BACKOFF_SKIP_DAYS after the newer one. After three, it is parked
until its inputs (the job prompt, the skill text) change. The drafting model is shown
the classes of its last few outcomes, never the ledger's reason strings.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from . import policy
from .budget import DAY
from .draft import HISTORY_LEN, top_fields
from .ledger import FixRecord

BACKOFF_SKIP_DAYS = 7
BACKOFF_SKIP_AFTER = 2
BACKOFF_PARK_AFTER = 3

COPY_TEXT = "contains text copied from untrusted tool results"
_COPY_ITEM = re.compile(r"payload\.(.+?): " + re.escape(COPY_TEXT))
# Replay reasons the drafted program did not cause (cycle_draft._replay_gate,
# replay.composed_gate): class "timeout", which skips but never parks.
REPLAY_OUTAGES = ("replay: history unavailable", "replay: no time left",
                  "replay: internal error")


def reason_class(rec: FixRecord) -> str:
    """The outcome class of a record; "" for records that are not failed drafts."""
    reason = rec.reason or ""
    if rec.state == "rejected":
        if COPY_TEXT in reason:
            return "copy"
        if reason.startswith("replay: limit hit"):
            return "replay_limit"
        if reason.startswith(REPLAY_OUTAGES):  # outages and budget, not the draft
            return "timeout"
        if reason.startswith("replay:"):
            return "replay"
        if rec.kind == "bridge_error" or reason.startswith("drafting call failed"):
            return "timeout"
        if rec.kind == "unparseable" or reason == "unparseable draft":
            return "unparseable"
        return "limits"
    if rec.state == "shadow_failed":
        return "shadow"
    if rec.state == "rolled_back":
        return "trial"
    if rec.state == "bridge_error":  # a scan or scheduling call failed after drafting
        return "timeout"
    return ""


def copy_fields(rec: FixRecord) -> list[str]:
    """The top-level payload fields a copy rejection names ("payload.steps[0].x" ->
    "steps"), in order, deduplicated; "?" for one that is not identifier-shaped.
    Deeper segments are keys the model wrote, which may be the copied text."""
    if rec.state != "rejected":
        return []
    return top_fields(m.group(1) for item in (rec.reason or "").split("; ")
                      if (m := _COPY_ITEM.fullmatch(item)) is not None)


def _parkable(rec: FixRecord, cls: str) -> bool:
    """Only outcomes the hashed inputs can cause park. A failed bridge call (credits,
    timeouts) and a reducer search's rejected placeholder (its input is the replay
    window, not the prompt) would otherwise stay parked for good; they only skip. A
    reducer that failed its shadow or trial parks: the same search finds it again."""
    return cls != "timeout" and not (rec.kind == "reducer" and rec.state == "rejected")


def input_hash(job: dict | None, skill_text: str | None) -> str:
    """What a draft was made from: the job prompt and the skill text."""
    prompt = job.get("prompt") if job else None
    return hashlib.sha256(json.dumps([prompt, skill_text]).encode()).hexdigest()


def _newest_first(records: list[FixRecord], candidate_key: str) -> list[FixRecord]:
    mine = [(r.created_at, i, r) for i, r in enumerate(records)
            if r.candidate_key == candidate_key]
    return [r for _t, _i, r in sorted(mine, key=lambda x: (x[0], x[1]), reverse=True)]


def backoff(records: list[FixRecord], candidate_key: str, current_hash: str,
            now: float) -> Literal["ok", "skip", "parked"]:
    """"parked" when the 3 newest failed outcomes share (kind, class), the outcome is
    parkable (_parkable), the newest was drafted on the current inputs and is under
    PARK_MAX_DAYS old; "skip" when the 2 newest share (kind, class) and the newest is
    under 7 days old; otherwise "ok". Records with class "" are ignored."""
    failed = [(r, c) for r in _newest_first(records, candidate_key)
              if (c := reason_class(r))]
    outcomes = [(r.kind, c) for r, c in failed]
    if (len(failed) >= BACKOFF_PARK_AFTER
            and len(set(outcomes[:BACKOFF_PARK_AFTER])) == 1
            and _parkable(*failed[0])
            and failed[0][0].input_hash == current_hash
            and now - failed[0][0].created_at < policy.PARK_MAX_DAYS * DAY):
        return "parked"
    if (len(failed) >= BACKOFF_SKIP_AFTER
            and len(set(outcomes[:BACKOFF_SKIP_AFTER])) == 1
            and now - failed[0][0].created_at < BACKOFF_SKIP_DAYS * DAY):
        return "skip"
    return "ok"


def draft_history(records: list[FixRecord], candidate_key: str) -> list[dict]:
    """The candidate's last HISTORY_LEN outcomes, newest first, as class and field
    names only: no ledger reason text."""
    return [{"kind": r.kind, "state": r.state, "reason_class": reason_class(r),
             "fields": copy_fields(r)}
            for r in _newest_first(records, candidate_key)[:HISTORY_LEN]]
