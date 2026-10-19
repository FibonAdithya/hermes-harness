"""One daily tuner run (spec 4, 6), held under a lock on `root/.lock`.

Order: clean up shadows -> evaluate shadows -> judge trials -> apply approved
proposals -> budget check -> detect -> draft / validate / shadow / apply / propose
-> restart if allowed -> weekly line.

`now` is a parameter; nothing here reads the clock. A BridgeError in any step is
recorded in the report and the cycle moves on; the ledger guarantees nothing is
left half-applied. A dry run makes no mutating bridge call (no drafting either)
and writes nothing under `root`; it lists what it would do in `planned`.

Besides the ledger, `root/cycle_state.json` holds {"pending_restart": bool,
"restart_times": [epoch, ... last 28 days], "weekly_written": "YYYY-Www",
"weekly_written_at": epoch,
"errors": [{"at": epoch, "error": str}]} (errors from the last 28 days; each failure
is logged once and the weekly line counts the log).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from . import policy, trial
from .bridge import Bridge, BridgeError
from .budget import DAY, JOB_KINDS, weekly_line
from .cycle_draft import BASE_KEY, GLOBAL_KINDS, DraftSteps
from .fingerprint import CopyIndex
from .history import History, HistoryUnavailable
from .ledger import (APPLIED_STATES, FixRecord, Ledger, _tool_files, apply, atomic_write,
                     is_shadow_tool, rollback)
from .measure import priced_cost
from .model import Fix, Session
from .proposals import apply_approved, approved, dropped
from .restart import restart_allowed

__all__ = ["CycleConfig", "CycleReport", "run_cycle", "weekly_line"]

HISTORY_DAYS = 35          # detection window (28) plus a week of margin
SHADOW_GRACE_S = 2 * DAY   # a shadow that has not run 2 days after run_at failed
NO_LIVE_RUN_S = 3 * DAY    # a shadow with no live run to compare 3 days after run_at failed
ERROR_KEEP_S = 28 * DAY
RESTART_KEEP_S = 28 * DAY
STATE_FILE = "cycle_state.json"
HEARTBEAT_FILE = "plugin_heartbeat.json"   # written by the plugin at register()
PLUGIN_NAME = "hermes-tuner"


@dataclass(frozen=True)
class CycleConfig:
    root: Path
    db: Path
    broker_state: Path
    skills_root: Path
    dry_run: bool = False


@dataclass
class CycleReport:
    applied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    spend_usd: float = 0.0
    shadowed: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)   # dry run: what would happen
    budget: str = ""
    weekly: str = ""
    restarted: bool = False
    skipped: str = ""
    dry_run: bool = False


def run_cycle(cfg: CycleConfig, bridge: Bridge, now: float) -> CycleReport:
    report = CycleReport(dry_run=cfg.dry_run)
    if cfg.dry_run:
        _Cycle(cfg, bridge, now, report).run()
        return report
    root = Path(cfg.root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report.skipped = "already running"
            return report
        _Cycle(cfg, bridge, now, report).run()
    finally:
        os.close(fd)  # releases the lock
    return report


class _Cycle(DraftSteps):
    def __init__(self, cfg: CycleConfig, bridge: Bridge, now: float, report: CycleReport):
        self.cfg, self.bridge, self.now, self.report = cfg, bridge, now, report
        self.root = Path(cfg.root)
        self.dry = cfg.dry_run
        self.restart_wanted = False
        self.history: History | None = None
        self.sessions: list[Session] = []
        self._copy_index: CopyIndex | None = None
        self._plugin_ok: bool | None = None

    # --- plumbing -----------------------------------------------------------------

    def err(self, msg: str) -> None:
        self.report.errors.append(msg)

    def plan(self, msg: str) -> None:
        self.report.planned.append(msg)

    def _load_state(self) -> dict:
        try:
            data = json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.err(f"{STATE_FILE} unreadable: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        if self.dry:
            return
        atomic_write(self.root / STATE_FILE,
                     json.dumps(self.state, indent=2, sort_keys=True).encode("utf-8"))

    def _shadow_entries(self) -> dict:
        try:
            return trial._read_entries(self.root)
        except (OSError, ValueError) as exc:
            self.err(f"{trial.SHADOW_FILE} unreadable: {exc}")
            return {}

    def _close(self, rec: FixRecord, state: str, reason: str) -> None:
        rec.state, rec.reason = state, reason  # type: ignore[assignment]
        self.ledger.save()

    def plugin_loaded(self) -> bool:
        """True when Hermes's config enables the tuner plugin AND the plugin's
        heartbeat is at or after the last recorded gateway restart (if any), i.e.
        the running gateway loaded it. Checked once per cycle, before the restart
        step; shadows and composed tools are unsafe without the guard (F4)."""
        if self._plugin_ok is None:
            self._plugin_ok = self._check_plugin()
        return self._plugin_ok

    def _check_plugin(self) -> bool:
        try:
            enabled = self.bridge.config_get("plugins.enabled")
        except BridgeError:
            return False
        if not isinstance(enabled, list) or PLUGIN_NAME not in enabled:
            return False
        try:
            beat = json.loads((self.root / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        t = beat.get("registered_at") if isinstance(beat, dict) else None
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            return False
        restarts = self._restart_times()
        return not restarts or t >= max(restarts)

    def _global_active(self) -> bool:
        return any(r.kind in GLOBAL_KINDS and r.state in ("shadow_pending", "trial", "applied")
                   for r in self.ledger.records)

    # --- the run ---------------------------------------------------------------------

    def run(self) -> None:
        try:
            self.ledger = Ledger.load(self.root)
        except (OSError, ValueError, TypeError) as exc:
            self.err(f"ledger unreadable, nothing done: {exc}")
            return
        self.state = self._load_state()
        self._step("cleanup", self.cleanup)
        self.entries = self._shadow_entries()
        self.shadow_ids = set(self.entries)
        try:
            self.history = History.open(self.cfg.db)
            self.sessions = self.history.sessions(self._history_since())
        except HistoryUnavailable as exc:
            self.err(f"state.db unavailable, run skipped: {exc}")
            self._finish_errors()
            self._save_state()
            return
        for name, step in (("evaluate_shadows", self.evaluate_shadows),
                           ("judge_trials", self.judge_trials),
                           ("approved_proposals", self.approved_proposals),
                           ("draft_fixes", self.draft_fixes),
                           ("restart", self.restart)):
            self._step(name, step)
        self._finish_errors()
        self._step("weekly", self.weekly)
        self._save_state()

    def _step(self, name: str, fn) -> None:
        """Run one step; an unexpected exception is logged (report and cycle_state
        errors) and the cycle goes on to the next step and the weekly line. Every
        state change a step makes is saved as it is made, so nothing is half-done."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self.err(f"{name}: unexpected {type(exc).__name__}: {exc}")

    def _history_since(self) -> float:
        """The detection window, widened only for records still being judged: a
        trial needs the 28 days before its applied_at (job prompt-token fallback,
        global baseline), a shadow_pending fix its shadow and live runs. Kept and
        applied records carry their priced baseline (baseline_cost), so they never
        widen it; a trial ends within TRIAL_DEADLINE_DAYS, so the window is bounded."""
        since = self.now - HISTORY_DAYS * DAY
        for r in self.ledger.records:
            if r.state == "trial" and r.applied_at is not None:
                since = min(since, r.applied_at - 28 * DAY)
            elif r.state == "shadow_pending":
                since = min(since, r.created_at - DAY)
        return since

    def _finish_errors(self) -> None:
        if self.dry:
            return
        log = [e for e in self.state.get("errors", []) if isinstance(e, dict)
               and isinstance(e.get("at"), (int, float)) and self.now - e["at"] < ERROR_KEEP_S]
        log += [{"at": self.now, "error": m[:500]} for m in self.report.errors]
        self.state["errors"] = log

    # 1. cleanup ------------------------------------------------------------------------

    def cleanup(self) -> None:
        if self.dry:
            return
        try:
            trial.cleanup_shadows(self.bridge, self.root, self.now)
        except (BridgeError, OSError, ValueError) as exc:
            self.err(f"cleanup_shadows: {type(exc).__name__}: {exc}")

    # 2. shadows ------------------------------------------------------------------------

    def evaluate_shadows(self) -> None:
        by_start = sorted(self.sessions, key=lambda s: (s.started_at, s.id))
        for rec in [r for r in self.ledger.records if r.state == "shadow_pending"]:
            if rec.kind not in JOB_KINDS:
                continue
            mine = [(sid, e) for sid, e in self.entries.items()
                    if isinstance(e, dict) and e.get("fix_id") == rec.id]
            if not mine:
                if self.now - rec.created_at > SHADOW_GRACE_S:
                    self._shadow_failed(rec, "shadow did not run")
                continue
            shadow_id, entry = max(mine, key=lambda x: x[1].get("created_at") or 0)
            live = entry.get("live_job_id")
            shadow = next((s for s in by_start if s.job_id == shadow_id), None)
            if shadow is None:
                run_at = trial._epoch(entry.get("run_at"))
                if run_at is None or self.now - run_at > SHADOW_GRACE_S:
                    self._shadow_failed(rec, "shadow did not run")
                continue
            if rec.needs_restart and not self._restarted_between(
                    trial._epoch(entry.get("scheduled_at")), shadow.started_at):
                self._shadow_failed(rec, "shadow ran before restart")
                continue
            if not shadow.calls:
                self._shadow_failed(rec, "shadow run made no tool calls")
                continue
            old = next((s for s in by_start if s.job_id == live and s.calls
                        and s.started_at > shadow.started_at), None)
            if old is None:
                run_at = trial._epoch(entry.get("run_at"))
                if run_at is not None and self.now - run_at > NO_LIVE_RUN_S:
                    self._shadow_failed(rec, "no live run to compare")
                continue  # wait for the live run to compare with
            verdict = self._compare(live, shadow, old)
            if verdict is None:
                self._shadow_failed(rec, f"no quality check for job {live}")
            elif not verdict.ok:
                self._shadow_failed(rec, verdict.reason or (
                    f"shadow disagreed: agreement {verdict.agreement:.2f}, "
                    f"{len(verdict.flips)} flips"))
            else:
                self._go_live(rec)

    def _restart_times(self) -> list[float]:
        times = self.state.get("restart_times")
        return [t for t in times if isinstance(t, (int, float)) and not isinstance(t, bool)] \
            if isinstance(times, list) else []

    def _restarted_between(self, scheduled_at: float | None, shadow_start: float) -> bool:
        """True when a gateway restart r satisfies scheduled_at <= r <= shadow_start. The
        tool dir is written at scheduling (step 7) and the restart step (8) runs later in
        the same cycle with the same `now`, so r == scheduled_at means the restart loaded
        the tool; the lock keeps two cycles from sharing a `now`. An unknown scheduling
        time fails closed."""
        if scheduled_at is None:
            return False
        return any(scheduled_at <= r <= shadow_start for r in self._restart_times())

    def _compare(self, live: object, shadow: Session, old: Session):
        rule = policy.EDITABLE_JOBS.get(live) if isinstance(live, str) else None
        if rule is None:
            return None
        h = self.history
        intents = trial.read_intents(self.root, h.chain_ids(shadow.id))
        old_calls, new_calls = h.calls_with_args(old.id), h.calls_with_args(shadow.id)
        if rule == "triage":
            return trial.compare_triage(trial.triage_decisions(old_calls, []),
                                        trial.triage_decisions(new_calls, intents))
        if rule == "calendar":
            return trial.compare_calendar(trial.calendar_events(old_calls, []),
                                          trial.calendar_events(new_calls, intents))
        return None

    def _shadow_failed(self, rec: FixRecord, reason: str) -> None:
        if self.dry:
            self.plan(f"shadow_failed {rec.id}: {reason}")
            return
        self._remove_shadow_tool(rec)
        self._close(rec, "shadow_failed", reason)
        self.report.rejected.append(rec.id)

    def _shadow_tool(self, fix: Fix) -> tuple[dict, str] | None:
        if fix.kind != "composed_tool":
            return None
        try:
            return _tool_files(fix.payload)
        except ValueError:
            return None

    def _remove_shadow_tool(self, rec: FixRecord) -> None:
        """Remove the shadow-only tool dir written for a composed_tool shadow, if it
        is still exactly ours."""
        try:
            fix = self.ledger.fix(rec.id)
        except (OSError, ValueError, TypeError):
            return
        files = self._shadow_tool(fix)
        if files is None:
            return
        tools = self.root / "tools"
        if is_shadow_tool(tools, fix.payload):
            shutil.rmtree(tools / files[0]["name"], ignore_errors=True)

    def _owner_changed_job(self, fix: Fix) -> bool | None:
        """True when the live job's prompt or toolsets differ from the `_base` recorded
        at scheduling (the owner edited it since); None when it cannot be read."""
        base = fix.payload.get(BASE_KEY) if isinstance(fix.payload, dict) else None
        if not isinstance(base, dict):
            return False  # recorded before _base existed
        try:
            live = self.bridge.get_job(fix.target)
        except BridgeError as exc:
            self.err(f"get_job {fix.target}: {exc}")
            return None
        if live is None:
            return False  # apply reports the missing job
        return any(live.get(k) != base.get(k) for k in ("prompt", "enabled_toolsets"))

    def _go_live(self, rec: FixRecord) -> None:
        if self.dry:
            self.plan(f"apply {rec.id}")
            return
        fix = self.ledger.fix(rec.id)
        if fix.kind == "composed_tool" and not self.plugin_loaded():
            self.err(f"plugin not loaded; {rec.id} not applied")
            return  # stays shadow_pending; retried next cycle
        changed = self._owner_changed_job(fix)
        if changed is None:
            return
        if changed:
            self._remove_shadow_tool(rec)
            self._close(rec, "owner_changed",
                        "owner changed the job after drafting; not applied")
            return
        try:  # apply rewrites a shadow-only tool dir without the flag
            out = apply(fix, self.ledger, self.bridge, self.root, self.now,
                        skills_root=self.cfg.skills_root)
        except ValueError as exc:
            self._remove_shadow_tool(rec)
            self._close(rec, "rejected", f"apply refused: {exc}")
            self.report.rejected.append(rec.id)
            return
        self._went_live(out)

    def _went_live(self, rec: FixRecord) -> None:
        if rec.state != "trial":
            self.err(f"apply {rec.id}: {rec.reason}")
            return
        if rec.kind in JOB_KINDS:
            base = trial.baseline_stats(self.sessions, rec.target, rec.applied_at)
            rec.baseline_cost = base.median_priced_cost if base is not None else None
            self.ledger.save()
        self.report.applied.append(rec.id)
        self.restart_wanted |= rec.needs_restart

    # 3. trials -------------------------------------------------------------------------

    def judge_trials(self) -> None:
        for rec in [r for r in self.ledger.records if r.state == "trial"]:
            if rec.applied_at is None:
                continue
            if rec.live_from is None:
                continue  # waiting for the gateway restart that makes it live (spec 4.7)
            if rec.kind in JOB_KINDS:
                # baseline: before applied_at; trial runs: after the fix went live
                base = trial.baseline_stats(self.sessions, rec.target, rec.applied_at)
                verdict = trial.trial_verdict(
                    rec, trial.trial_stats(self.sessions, rec.target, rec.live_from),
                    base.median_prompt_tokens if base is not None else 0.0)
            elif rec.kind in GLOBAL_KINDS:
                if self.now - rec.live_from < policy.GLOBAL_TRIAL_DAYS * DAY:
                    continue
                verdict = self._global_verdict(rec.applied_at, rec.live_from)
            else:
                continue
            if verdict == "keep":
                self._keep(rec)
            elif verdict == "rollback":
                self._rollback(rec)

    def _global_verdict(self, applied_at: float, live_from: float) -> str:
        """keep only if the median priced_cost over chat sessions and editable-job runs
        since live_from is strictly below the 28 days before applied_at (prompt
        tokens when both medians are 0)."""
        def runs(lo: float, hi: float) -> list[Session]:
            return [s for s in self.sessions if lo <= s.started_at < hi and s.calls
                    and (s.source == "telegram" or s.job_id in policy.EDITABLE_JOBS)]
        after = [s for s in runs(live_from, float("inf")) if s.started_at > live_from]
        before = runs(applied_at - 28 * DAY, applied_at)
        if not after or not before:
            return "rollback"
        ca, cb = median(map(priced_cost, after)), median(map(priced_cost, before))
        if ca == 0 and cb == 0:
            better = (median(s.prompt_tokens for s in after)
                      < median(s.prompt_tokens for s in before))
        else:
            better = ca < cb
        return "keep" if better else "rollback"

    def _keep(self, rec: FixRecord) -> None:
        if self.dry:
            self.plan(f"keep {rec.id}")
            return
        rec.decided_at = self.now
        self._close(rec, "kept", "trial: cheaper than baseline")
        self.report.kept.append(rec.id)

    def _rollback(self, rec: FixRecord) -> None:
        if self.dry:
            self.plan(f"rollback {rec.id}")
            return
        try:
            out = rollback(rec, self.ledger, self.bridge, self.root, self.now,
                           skills_root=self.cfg.skills_root)
        except (ValueError, OSError, KeyError) as exc:
            self.err(f"rollback {rec.id}: {type(exc).__name__}: {exc}")
            return
        if out.state == "trial":
            self.err(f"rollback {rec.id}: {out.reason}")
            return
        out.decided_at = self.now
        if out.state == "rolled_back":
            out.reason = "trial: not cheaper than baseline"
            self.restart_wanted |= out.needs_restart
        self.ledger.save()
        self.report.rolled_back.append(rec.id)

    # 4. proposals ----------------------------------------------------------------------

    def approved_proposals(self) -> None:
        cfg = self.cfg
        for doc in approved(self.root, cfg.broker_state):
            pid = doc.get("id")
            rec = self.ledger.get(pid)
            if rec is not None and rec.state not in ("proposed", "approved", "bridge_error"):
                continue
            if self.dry:
                self.plan(f"apply proposal {pid}")
                continue
            try:
                out = apply_approved(pid, self.bridge, self.ledger, self.root,
                                     cfg.broker_state, self.now)
            except (ValueError, OSError) as exc:
                self.err(f"proposal {pid}: {exc}")
                continue
            if out is None:
                continue
            if out.state == "applied" and out.applied_at == self.now:
                self.report.applied.append(pid)
                self.restart_wanted |= out.needs_restart
            elif out.state == "bridge_error":
                self.err(f"proposal {pid}: {out.reason}")
        for doc in dropped(self.root, cfg.broker_state):
            rec = self.ledger.get(doc.get("id"))
            if rec is not None and rec.state == "proposed":
                if self.dry:
                    self.plan(f"drop proposal {rec.id}")
                else:
                    self._close(rec, "dropped", "owner dropped the proposal")

    # 8. restart --------------------------------------------------------------------------

    def restart(self) -> None:
        if not (self.restart_wanted or self.state.get("pending_restart")):
            return
        if self.dry:
            self.plan("restart gateway when the window allows")
            return
        self.state["pending_restart"] = True
        try:
            next_runs = []
            for j in self.bridge.list_jobs():
                if isinstance(j, dict) and j.get("enabled", True):
                    t = trial._epoch(j.get("next_run_at"))
                    if t is not None:
                        next_runs.append(t)
            last = self.history.last_activity()
            if restart_allowed(datetime.fromtimestamp(self.now, timezone.utc), last, next_runs):
                self.bridge.restart_gateway()
                self.state["pending_restart"] = False
                self.state["restart_times"] = [
                    t for t in self._restart_times() if self.now - t < RESTART_KEEP_S] + [self.now]
                self.report.restarted = True
                for r in self.ledger.records:
                    if r.state in APPLIED_STATES and r.needs_restart and r.live_from is None:
                        r.live_from = self.now
                self.ledger.save()
        except (BridgeError, HistoryUnavailable) as exc:
            self.err(f"restart: {type(exc).__name__}: {exc}")

    # 9. weekly line ----------------------------------------------------------------------

    def weekly(self) -> None:
        iso = datetime.fromtimestamp(self.now, timezone.utc).isocalendar()
        tag = f"{iso.year}-W{iso.week:02d}"
        path = self.root / "outbox" / f"weekly-{tag}.txt"
        if (self.state.get("weekly_written") == tag or path.exists()
                or path.with_suffix(".sent").exists()):
            return
        since = self.state.get("weekly_written_at")
        if not isinstance(since, (int, float)) or since >= self.now:
            since = self.now - 7 * DAY
        if self.dry:  # _finish_errors does not log in a dry run
            recent = len(self.report.errors)
        else:
            recent = sum(1 for e in self.state.get("errors", []) if isinstance(e, dict)
                         and isinstance(e.get("at"), (int, float)) and since < e["at"] <= self.now)
        line = weekly_line(self.ledger, self.sessions, self.now, self.shadow_ids, errors=recent,
                           since=since)
        self.report.weekly = line
        if self.dry:
            self.plan(f"weekly line {tag}")
            return
        atomic_write(path, (line + "\n").encode("utf-8"))
        self.state["weekly_written"] = tag
        self.state["weekly_written_at"] = self.now
