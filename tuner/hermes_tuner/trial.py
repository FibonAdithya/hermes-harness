"""Shadow runs, quality comparison and trial verdicts (spec 4.5).

Before a fix to a mutating job goes live, `schedule_shadow` runs a one-shot
copy of the job with the fix's new prompt shortly before the live run,
delivering locally. The plugin blocks and records its mutations as intents.
The cycle compares the shadow's decisions with the live run's, and after the
fix is live judges the trial on cost.

Nothing here reads the clock: `now` is always a parameter.

`root` holds `shadow_jobs.json`:
    {"<shadow job id>": {"fix_id", "live_job_id", "run_at": <epoch s>,
                         "created_at": <epoch s>, "scheduled_at": <epoch s, = created_at>,
                         "removed_at": <epoch s, once cleaned up>}}
and the plugin writes `shadow/<session id>.jsonl` (one intent per line).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import policy
from .bridge import Bridge, BridgeError
from .ledger import FixRecord, atomic_write, job_after_for
from .measure import JobStats, job_stats
from .model import Fix, Session

SHADOW_FILE = "shadow_jobs.json"
STALE_AFTER_S = 3600
BASELINE_DAYS = 28
READ_TOOL = "mcp__google__get_gmail_message_content"
MODIFY_TOOL = "mcp__google__modify_gmail_message_labels"
EVENT_TOOL = "mcp__google__manage_event"
# The session-id shapes the plugin records under (and Hermes assigns).
_CRON_ID = re.compile(r"cron_[0-9a-f]{12}_\d{8}_\d{6}", re.ASCII)
_CHILD_ID = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}", re.ASCII)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    agreement: float
    flips: list[str]


# --- shadow jobs ---------------------------------------------------------------

def _read_entries(root: Path) -> dict:
    """The ledger entries; {} only when the file does not exist. A corrupt or
    unreadable file raises, so it is never overwritten with an empty one."""
    try:
        text = (Path(root) / SHADOW_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{SHADOW_FILE} is not a JSON object")
    return data


def _write_entries(root: Path, entries: dict) -> None:
    atomic_write(Path(root) / SHADOW_FILE,
                  json.dumps(entries, indent=2, sort_keys=True).encode("utf-8"))


def _epoch(value: object) -> float | None:
    """Epoch seconds of a number or a timezone-aware ISO-8601 string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.timestamp() if dt.tzinfo is not None else None
    return None


def _shadow_prompt(fix: Fix) -> str:
    p = fix.payload if isinstance(fix.payload, dict) else {}
    if fix.kind == "job_prompt":
        prompt = p.get("prompt")
    elif fix.kind == "composed_tool":
        prompt = p.get("job_prompt")
    else:
        raise ValueError(f"no shadow run for {fix.kind!r} fixes")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("fix has no job prompt to shadow")
    return prompt


def schedule_shadow(fix: Fix, job: dict, bridge: Bridge, root: Path, now: float) -> str:
    """Create the one-shot shadow job and record it; returns its id.

    Runs at the live job's `next_run_at` minus SHADOW_LEAD_MIN minutes, with the
    fix's new prompt, delivering locally. Raises ValueError (nothing created) when
    the fix has no prompt, `next_run_at` is missing, unparseable or has no UTC
    offset, or the shadow time is not after `now`.
    """
    prompt = _shadow_prompt(fix)
    live_at = _epoch(job.get("next_run_at"))
    if live_at is None:
        raise ValueError("live job has no usable next_run_at")
    run_at = live_at - policy.SHADOW_LEAD_MIN * 60
    if run_at <= now:
        raise ValueError("shadow time is not in the future")
    after = job_after_for(fix, job)
    if after is None:  # unreachable: _shadow_prompt rejected a missing job_prompt
        raise ValueError("fix does not change the job prompt")
    toolsets = after["enabled_toolsets"]
    schedule = datetime.fromtimestamp(run_at, timezone.utc).isoformat()
    created = bridge.create_job(prompt, schedule, policy.SHADOW_PREFIX + fix.id,
                                "local", toolsets)
    shadow_id = created["id"]
    try:
        entries = _read_entries(root)
        entries[shadow_id] = {"fix_id": fix.id, "live_job_id": job.get("id"),
                              "run_at": run_at, "created_at": now, "scheduled_at": now}
        _write_entries(root, entries)
    except BaseException:
        try:
            bridge.remove_job(shadow_id)
        except BridgeError:
            pass
        raise
    return shadow_id


def cleanup_shadows(bridge: Bridge, root: Path, now: float) -> list[str]:
    """Remove shadow jobs whose run time passed over an hour ago.

    Returns the ids removed from Hermes. The ledger entry stays, marked with
    `removed_at`, so shadow cost can still be accounted. A job named like a
    shadow with no ledger entry (or no usable run time) is an orphan and is
    removed too. A failed removal leaves the entry unmarked for the next cycle.
    """
    entries = _read_entries(root)
    jobs = {j.get("id"): j for j in bridge.list_jobs() if isinstance(j, dict)}
    removed: list[str] = []
    changed = False
    for jid, job in jobs.items():
        name = job.get("name")
        if not isinstance(name, str) or not name.startswith(policy.SHADOW_PREFIX):
            continue
        entry = entries.get(jid)
        entry = entry if isinstance(entry, dict) else None
        run_at = _epoch(entry.get("run_at")) if entry else None
        if run_at is None:
            run_at = _epoch(job.get("next_run_at"))
        if run_at is not None and now - run_at <= STALE_AFTER_S:
            continue
        try:
            bridge.remove_job(jid)
        except BridgeError:
            continue
        removed.append(jid)
        if entry is not None and "removed_at" not in entry:
            entry["removed_at"] = now
            changed = True
    for jid, entry in entries.items():
        if jid in jobs or not isinstance(entry, dict) or "removed_at" in entry:
            continue
        run_at = _epoch(entry.get("run_at"))
        if run_at is not None and now - run_at > STALE_AFTER_S:
            entry["removed_at"] = now  # the job is already gone from Hermes
            changed = True
    if changed:
        _write_entries(root, entries)
    return removed


def read_intents(root: Path, session_ids: Iterable[str]) -> list[dict]:
    """Intents the plugin recorded under each id, in the order given.

    Pass the shadow run's root id and its compression-chain member ids: the
    plugin writes under a child id when it failed closed. Ids not shaped like
    cron or compression ids are skipped (they become file names). Missing files
    and malformed lines are skipped.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for sid in session_ids:
        if (not isinstance(sid, str) or sid in seen
                or not (_CRON_ID.fullmatch(sid) or _CHILD_ID.fullmatch(sid))):
            continue
        seen.add(sid)
        try:
            text = (Path(root) / "shadow" / f"{sid}.jsonl").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
    return out


# --- decisions ----------------------------------------------------------------------

def _tool_calls(calls: list[tuple[str, dict]], intents: list[dict]):
    for name, args in calls:
        if isinstance(args, dict):
            yield name, args
    for intent in intents:
        if isinstance(intent, dict) and isinstance(intent.get("args"), dict):
            yield intent.get("tool"), intent["args"]


def _labels(args: dict, key: str) -> set:
    v = args.get(key)
    return set(v) if isinstance(v, (list, tuple)) else set()


def triage_decisions(calls: list[tuple[str, dict]], intents: list[dict]) -> dict[str, str]:
    """Message id -> KEEP | TRASH | ARCHIVE from real calls plus recorded intents.

    TRASH if any call adds the TRASH label (wins over ARCHIVE); ARCHIVE if any
    removes INBOX; any other message read or modified is KEEP.
    """
    trash: set[str] = set()
    archive: set[str] = set()
    seen: set[str] = set()
    for name, args in _tool_calls(calls, intents):
        mid = args.get("message_id")
        if not isinstance(mid, str):
            continue
        if name == READ_TOOL:
            seen.add(mid)
        elif name == MODIFY_TOOL:
            seen.add(mid)
            if "TRASH" in _labels(args, "add_label_ids"):
                trash.add(mid)
            if "INBOX" in _labels(args, "remove_label_ids"):
                archive.add(mid)
    return {m: "TRASH" if m in trash else "ARCHIVE" if m in archive else "KEEP"
            for m in seen}


def compare_triage(old: dict, new: dict) -> Verdict:
    """Compare decisions over the message ids both versions saw.

    A flip is an id KEEP in `old` and TRASH/ARCHIVE in `new`. ok = agreement >=
    TRIAGE_AGREEMENT_MIN and no flips. With no ids in common agreement is 1.0
    and ok is True (there are no flips either): nothing disagrees.
    """
    common = sorted(old.keys() & new.keys())
    if not common:
        return Verdict(True, 1.0, [])
    agreement = sum(old[m] == new[m] for m in common) / len(common)
    flips = [m for m in common if old[m] == "KEEP" and new[m] in ("TRASH", "ARCHIVE")]
    return Verdict(agreement >= policy.TRIAGE_AGREEMENT_MIN and not flips, agreement, flips)


def calendar_events(calls: list[tuple[str, dict]], intents: list[dict]) -> set[tuple[str, str]]:
    """(summary, date) of every event created, case- and space-insensitive."""
    out: set[tuple[str, str]] = set()
    for name, args in _tool_calls(calls, intents):
        if name != EVENT_TOOL or args.get("action") != "create":
            continue
        summary, start = args.get("summary"), args.get("start_time")
        if isinstance(summary, str) and isinstance(start, str):
            out.add((summary.casefold().strip(), start[:10]))
    return out


def compare_calendar(old: set, new: set) -> Verdict:
    """ok when every event `old` creates is also created by `new`.

    agreement = |old & new| / |old| (1.0 when old is empty); flips are the
    missing events as "summary|date", sorted.
    """
    missing = old - new
    agreement = 1.0 if not old else len(old & new) / len(old)
    return Verdict(not missing, agreement, sorted(f"{s}|{d}" for s, d in missing))


# --- trial ---------------------------------------------------------------------------

def trial_stats(sessions: list[Session], job_id: str, applied_at: float) -> JobStats | None:
    """job_stats over the runs that started after `applied_at` (the new version's)."""
    return job_stats([s for s in sessions if s.started_at > applied_at], job_id)


def baseline_stats(sessions: list[Session], job_id: str, applied_at: float) -> JobStats | None:
    """job_stats over runs in [applied_at - 28 days, applied_at): the old version's."""
    start = applied_at - BASELINE_DAYS * 86400
    return job_stats([s for s in sessions if start <= s.started_at < applied_at], job_id)


def trial_verdict(record: FixRecord, stats_now: JobStats | None,
                  prompt_baseline: float) -> Literal["continue", "keep", "rollback"]:
    """`stats_now` covers only runs after `record.live_from` (when the fix took
    effect: applied_at, or the gateway restart for a fix that needs one).

    continue until JOB_TRIAL_RUNS runs; then keep only if the median priced cost is
    strictly below `record.baseline_cost` (the priced median stored at go-live; a
    missing one counts as 0). Only when both priced medians are 0 does it fall back
    to median prompt tokens strictly below `prompt_baseline`.
    """
    if stats_now is None or stats_now.runs < policy.JOB_TRIAL_RUNS:
        return "continue"
    base = record.baseline_cost or 0.0
    if base == 0 and stats_now.median_priced_cost == 0:
        better = stats_now.median_prompt_tokens < prompt_baseline
    else:
        better = stats_now.median_priced_cost < base
    return "keep" if better else "rollback"
