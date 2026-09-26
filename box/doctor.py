"""fleet-doctor: turn a broken fleet night into one triage issue on FibonAdithya/fleet.

Deterministic on purpose: nothing here asks a model whether or what to file, so
log content can shape only the quoted evidence. Faults in fleet, not in the task
it was working on: a hard task reaching NEEDS_HUMAN is fleet working.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import boxlib
import tasklib

MAX_NEW_ISSUES = 3
EVIDENCE_LINES = 20
S4_THRESHOLD = 3
LOG_TAIL_BYTES = 256 * 1024
# 21 evidence lines (20 plus one note) of at most 141 characters, a key of at
# most 201 and the header come to about 3300, under tasklib.MAX_BODY (3500), so
# one oversized log cannot make file_issue refuse the body.
# test_worst_case_body_fits_max_body holds this.
EVIDENCE_LINE_CHARS = 140
KEY_CHARS = 200
DRAIN_NOTE = ("fleet AGENTS.md invariant 5: an unpriced transcript is never fixed by adding a [prices] row. "
              "This finding is for the owner.")
TERMINAL = ("done", "failed", "killed", "skipped", "paused", "stale", "unknown", "unreadable")
# Not `killed`: `fleet run` loops until RuntimeMaxSec's SIGTERM, so that is how
# every fleet night ends. A daemon that crashes exits non-zero first and reads `failed`.
BROKEN = ("failed", "stale", "unknown", "unreadable")

# One per captured file in tests/fixtures/ops/, named by its stem. Write each
# regex from the captured text (plan Task 7 Step 1), never from memory.
OPS_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # claude -p, empty HOME: "Not logged in · Please run /login"
    ("claude-not-logged-in", re.compile(r"Not logged in \S+ Please run /login")),
    # codex exec, empty HOME: "unexpected status 401 Unauthorized: Missing bearer or basic
    # authentication in header, url: https://api.openai.com/v1/responses"
    ("codex-not-logged-in", re.compile(r"401 Unauthorized: Missing bearer or basic authentication in header, "
                                       r"url: \S*api\.openai\.com")),
    # gh api user, empty GH_CONFIG_DIR: "To get started with GitHub CLI, please run:  gh auth login"
    ("gh-not-logged-in", re.compile(r"To get started with GitHub CLI, please run:\s+gh auth login")),
    # dd to /dev/full: "dd: error writing '/dev/full': No space left on device"
    ("disk-full", re.compile(r"No space left on device")),
)
RUN_ID_RE = re.compile(r"fleet run: run_id=(\S+)")
EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Refused|Exit)\w*)\b")
FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')
DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class Finding:
    kind: str
    key: str
    area: str
    suggested: str
    ops: bool = False
    evidence: tuple[str, ...] = field(default=())


def signature(kind: str, key: str, fleet_sha: str) -> str:
    return hashlib.sha256(f"{kind}|{key}|{fleet_sha}".encode()).hexdigest()[:8]


def _clip(line: str, limit: int = EVIDENCE_LINE_CHARS) -> str:
    return line if len(line) <= limit else line[:limit] + "…"


def _intent(e: dict) -> dict:
    """The entry's intent, or {} for a run-log line that is JSON but not an intent record."""
    i = e.get("intent") if isinstance(e, dict) else None
    return i if isinstance(i, dict) else {}


def _tail(path: Path) -> list[str] | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None


def _crash_key(lines: list[str]) -> str:
    exc, frame = "no-exception", "no-frame"
    for i in range(len(lines) - 1, -1, -1):
        m = EXC_RE.match(lines[i])
        if m:
            exc = m.group(1).rsplit(".", 1)[-1]
            for j in range(i - 1, -1, -1):
                f = FRAME_RE.match(lines[j])
                if f:
                    frame = f"{Path(f.group(1)).name}:{f.group(2)}"
                    break
            break
    return f"{exc}|{frame}"


def _runlog(repo_dir: Path, run_id: str | None) -> list[dict]:
    if run_id is None:
        return []
    out = []
    for line in (_tail(Path(repo_dir) / ".fleet" / "run.jsonl") or []):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("run_id") == run_id:
            out.append(e)
    return out


def examine(night: dict, night_dir: Path, repo_dir: Path) -> list[Finding]:
    lines = _tail(Path(night_dir) / "log")
    evidence = tuple(_clip(x) for x in (lines or [])[-EVIDENCE_LINES:])
    text = "\n".join(lines or [])
    for name, pat in OPS_PATTERNS:
        if pat.search(text):
            return [Finding("ops", name, "daemon", "drop", True, evidence)]
    ids = RUN_ID_RE.findall(text)
    entries = _runlog(repo_dir, ids[-1] if ids else None)
    if lines is None:
        return [Finding("S2", "no-log", "daemon", "solo", False, ())]
    if night["status"] in BROKEN:
        # The run log records intents, not ticks, so an empty one on a healthy
        # night only means an idle backlog. It is evidence only once the night broke.
        if not ids:
            return [Finding("S2", f"no-start|{_crash_key(lines)}", "daemon", "solo", False, evidence)]
        if not entries:
            return [Finding("S2", f"no-tick|{_crash_key(lines)}", "daemon", "solo", False, evidence)]
        return [Finding("S1", f"{night['status']}|{_crash_key(lines)}", "daemon", "solo", False, evidence)]
    found = []
    intents = [_intent(e) for e in entries]
    drains = sorted({DIGITS.sub("N", str(i.get("reason", ""))) for i in intents if i.get("kind") == "Drain"})
    for reason in drains:
        found.append(Finding("S3", reason, "cost", "task", False, (DRAIN_NOTE, *evidence)))
    counts = collections.Counter((i["kind"], DIGITS.sub("N", str(i.get("reason", "")))) for i in intents
                                 if i.get("kind") in ("Release", "Escalate"))
    for (kind, reason), n in sorted(counts.items()):
        if n >= S4_THRESHOLD:
            found.append(Finding("S4", f"{kind}:{reason}", "executor", "task", False,
                                 (_clip(f"{n} x {kind}({reason}) in run {ids[-1]}"), *evidence)))
    return found


def _safe_key(key: str) -> str:
    """The key as it may appear inside inline code: no backticks, one line, bounded."""
    return _clip(" ".join(key.replace("`", "'").split()), KEY_CHARS)


def _body(night_id: str, fleet_sha: str, f: Finding) -> str:
    fence = "\n".join(f.evidence).replace("```", "'''")
    return (f"fleet-doctor found `{f.kind}` in night `{night_id}` on fleet `{fleet_sha[:12]}`.\n\n"
            f"Key: `{_safe_key(f.key)}`\nSuggested reply: `{f.suggested}`\n\n```\n{fence}\n```\n")


def run(home: Path, gh: tasklib.Gh) -> dict:
    home = Path(home)
    root = boxlib.nights_root({"HOME": str(home)})
    fleet_dir = home / "TIG" / "fleet"
    fleet_slug = tasklib.slug(fleet_dir)
    stamp = home / ".local" / "share" / "fleet-deployed.sha"
    fleet_sha = stamp.read_text().strip() if stamp.is_file() else "unknown"
    areas = frozenset({"daemon", "cost", "executor"})
    total = {"filed": [], "commented": [], "skipped": 0, "errors": []}
    for night in boxlib.read_status(root):
        d = root / night["id"]
        if not night["id"].startswith("fleet-") or night["status"] not in TERMINAL or (d / "doctored").exists():
            continue
        mine = {"filed": [], "commented": [], "skipped": 0}
        try:
            _doctor_night(gh, night, d, home, fleet_slug, fleet_sha, areas, total, mine)
        except (tasklib.TaskError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            # No `doctored` marker: a transient gh failure is retried next hour,
            # and one poison night does not stop the nights after it.
            mine["error"] = str(exc)[:300]
            total["errors"].append(night["id"])
        else:
            (d / "doctored").write_text("")
        finally:
            (d / "doctor.json").write_text(json.dumps(mine))
            total["commented"] += mine["commented"]
            total["skipped"] += mine["skipped"]
    return total


def _doctor_night(gh: tasklib.Gh, night: dict, d: Path, home: Path, fleet_slug: str, fleet_sha: str,
                  areas: frozenset[str], total: dict, mine: dict) -> None:
    try:
        meta = json.loads(boxlib.read_small(d / "meta.json", 65536) or "{}")
        repo = meta.get("repo", "") if isinstance(meta, dict) else ""
        repo_dir = home / "TIG" / boxlib._name(repo)
    except (ValueError, TypeError, json.JSONDecodeError):
        repo_dir = home / "TIG" / "nonexistent"
    for f in examine(night, d, repo_dir):
        sig = signature(f.kind, f.key, fleet_sha)
        label = f"doctor:{sig}"
        tasklib.ensure_label(gh, fleet_slug, label)
        existing = tasklib.open_issues_with_label(gh, fleet_slug, label)
        if existing:
            tasklib.comment(gh, fleet_slug, existing[0]["number"], f"Seen again in night `{night['id']}`.")
            mine["commented"].append(existing[0]["number"])
            continue
        if len(total["filed"]) >= MAX_NEW_ISSUES:
            mine["skipped"] += 1
            continue
        extra = (label, "ops") if f.ops else (label,)
        out = tasklib.file_issue(gh, fleet_slug, f"[doctor] {f.kind} {_safe_key(f.key)}"[:200],
                                 _body(night["id"], fleet_sha, f), f.area, "investigation", areas,
                                 "source:doctor", extra)
        mine["filed"].append(out["number"])
        total["filed"].append(out["number"])
