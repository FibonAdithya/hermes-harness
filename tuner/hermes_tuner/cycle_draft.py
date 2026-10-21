"""Cycle steps 5-7: budget check, detection, and drafting each selected candidate
into a rejected, shadow-pending, trial or proposed record (spec 4.2-4.4, 6).

A mixin for `cycle._Cycle`: it uses the cycle's ledger, sessions, history, report
and its `_close`, `_went_live`, `_shadow_tool` and `_remove_shadow_tool` helpers.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from . import policy, trial
from .bridge import BridgeError
from .budget import DAY, JOB_KINDS, iso_week_start, job_runs, savings, tuner_spend
from .detect import SCHEMA_TOOL, SKILL_TOOL, Candidate, detect
from .draft import draft
from .fingerprint import CopyIndex
from .ledger import FixRecord, _Failed, apply, needs_restart, write_shadow_tool
from .limits import LimitContext, changeable_settings, check_fix
from .measure import priced_cost
from .model import Fix, Session
from .proposals import approved_callables, write_proposal

COPY_INDEX_DAYS = 14
BASE_KEY = "_base"  # Fix payload key: the job as drafted against; never sent to Hermes
PLUGIN_NOT_LOADED = "plugin not loaded; shadow not scheduled"
GLOBAL_KINDS = frozenset({"config", "skill"})
GLOBAL_CANDIDATES = ("schema_lookup", "skill_load")
JOB_CANDIDATES = ("sequence", "reread")


def _skill_file(skills_root: Path, name: str) -> Path | None:
    """The SKILL.md of the skill directory called `name`, first in sorted order."""
    try:
        hits = sorted(p for p in Path(skills_root).rglob("SKILL.md") if p.parent.name == name)
    except OSError:
        return None
    return hits[0] if hits else None


def bundled_skills(skills_root: Path) -> frozenset[str] | None:
    """Skill names the tuner must never edit: those in `.bundled_manifest` (shipped
    with Hermes) plus those installed from the Skills Hub, read from `.hub/lock.json`
    as Hermes reads it (tools/skill_usage.py): the keys of "installed" and the last
    part of each entry's install_path. None when the manifest is missing, or when a
    lock file exists but cannot be read (fail closed: no skill fixes)."""
    root = Path(skills_root)
    try:
        text = (root / ".bundled_manifest").read_text(encoding="utf-8")
    except OSError:
        return None
    names = {line.split(":", 1)[0].strip() for line in text.splitlines()}
    lock = root / ".hub" / "lock.json"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        installed = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(installed, dict):
            return None
        for key, entry in installed.items():
            names.add(str(key))
            path = entry.get("install_path") if isinstance(entry, dict) else None
            if isinstance(path, str) and path.strip():
                names.add(Path(path.strip()).name)
    return frozenset(n for n in names if n)


class DraftSteps:
    # 5-7. budget, detect, draft ----------------------------------------------------------

    def _job_cost(self, job_id: str) -> float:
        costs = [priced_cost(s, self.pricing) for s in job_runs(self.sessions, job_id)
                 if s.started_at >= self.now - 28 * DAY]
        return median(costs) if costs else 0.0

    def draft_fixes(self) -> None:
        """Skipped without a price list: the budget, the detector's ranking and the
        trial baseline all need one (the cycle reported "no model pricing")."""
        if self.pricing is None:
            return
        now, pricing = self.now, self.pricing
        week = tuner_spend(self.ledger, self.sessions, self.shadow_ids, iso_week_start(now),
                           pricing)
        spend28 = tuner_spend(self.ledger, self.sessions, self.shadow_ids, now - 28 * DAY,
                              pricing)
        saved28 = savings(self.ledger, self.sessions, now - 28 * DAY, now, pricing)
        allowance = policy.PAUSE_ALLOWANCE_USD
        if spend28 > saved28 and spend28 > allowance:
            self.report.budget = (f"paused: 28-day spend ${spend28:.4f} > 28-day savings "
                                  f"${saved28:.4f} and > ${allowance:.2f} starting allowance "
                                  "(ESTIMATE)")
            return
        window = [s for s in self.sessions if s.started_at >= now - 28 * DAY]
        self.skill_names = self._skill_names(window)
        self.window = window
        cands = detect(window, now, pricing, self.skill_names)
        for cand in self._select(cands):
            job_cost = self._job_cost(cand.job_id) if cand.job_id else 0.0
            projected = policy.DRAFT_EST_USD + job_cost
            if week + projected > policy.WEEKLY_CAP_USD:
                self.report.budget = (f"cap: week spend ${week:.2f} + projected "
                                      f"${projected:.2f} > ${policy.WEEKLY_CAP_USD:.2f} "
                                      "(ESTIMATE)")
                continue
            if self.dry:
                self.plan(f"draft {cand.key}" + (f" for job {cand.job_id}" if cand.job_id else ""))
                continue
            spend, shadowed = self._draft_one(cand)
            week += spend + (job_cost if shadowed else 0.0)

    def _skill_names(self, window: list[Session]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for s in window:
            if any(c.name == SKILL_TOOL for c in s.calls):
                names = []
                for name, args in self.history.calls_with_args(s.id):
                    if name == SKILL_TOOL:
                        n = args.get("name")
                        names.append(n if isinstance(n, str) else "?")
                out[s.id] = names
        return out

    def _select(self, cands: list[Candidate]) -> list[Candidate]:
        picked = []
        for job in sorted(policy.EDITABLE_JOBS):
            if job in policy.NEVER_TOUCH_JOBS or self.ledger.active_for(job) is not None:
                continue
            c = next((c for c in cands if c.job_id == job and c.kind in JOB_CANDIDATES), None)
            if c is not None:
                picked.append(c)
        if not self._global_active():
            c = next((c for c in cands if c.kind in GLOBAL_CANDIDATES), None)
            if c is not None:
                picked.append(c)
        return picked

    def _call_shape(self, cand: Candidate) -> list[tuple[str, tuple[str, ...]]]:
        """Tool names and argument KEYS of the candidate's most recent session."""
        def matches(s: Session) -> bool:
            if cand.job_id is not None:
                return s.job_id == cand.job_id and bool(s.calls)
            if cand.kind == "schema_lookup":
                return sum(c.name == SCHEMA_TOOL for c in s.calls) >= 2
            name = cand.key.split(":", 1)[1]
            return name in self.skill_names.get(s.id, ())
        recent = max((s for s in self.window if matches(s)),
                     key=lambda s: (s.started_at, s.id), default=None)
        return [] if recent is None else [(c.name, c.arg_keys) for c in recent.calls]

    def _settings(self) -> dict:
        """The settings a config fix may change, read once per cycle. On a bridge
        failure: {} (no config fix passes) and one error."""
        if self._config_settings is None:
            try:
                raw = self.bridge.config_settings(list(policy.CONFIG_ALLOW_PREFIXES))
            except BridgeError as exc:
                self.err(f"config_settings: {exc}")
                raw = {}
            self._config_settings = changeable_settings(raw)
        return self._config_settings

    def _bundled(self) -> frozenset[str] | None:
        return bundled_skills(self.cfg.skills_root)

    def copy_index(self) -> CopyIndex:
        if self._copy_index is None:
            self._copy_index = CopyIndex.build(
                self.history.untrusted_texts(self.now - COPY_INDEX_DAYS * DAY))
        return self._copy_index

    def _draft_one(self, cand: Candidate) -> tuple[float, bool]:
        """Draft, check and act on one candidate. Returns (spend, shadow scheduled)."""
        job = None
        if cand.job_id is not None:
            if not self.plugin_loaded():  # a job fix needs a guarded shadow run (F4)
                self.err(PLUGIN_NOT_LOADED)
                return 0.0, False
            try:
                job = self.bridge.get_job(cand.job_id)
            except BridgeError as exc:
                self.err(f"get_job {cand.job_id}: {exc}")
                return 0.0, False
            if job is None:
                self.err(f"job {cand.job_id} not found")
                return 0.0, False
        bundled = self._bundled()
        skill_text = None
        if cand.kind == "skill_load":
            name = cand.key.split(":", 1)[1]
            path = _skill_file(self.cfg.skills_root, name)
            if bundled is None or name in bundled or path is None:
                return 0.0, False  # bundled, unknown or not found: nothing to draft
            rel = path.relative_to(self.cfg.skills_root).parts
            if any(p in bundled for p in rel[:-1]):
                return 0.0, False
            skill_text = path.read_text(encoding="utf-8", errors="replace")
        try:
            fix, spend = draft(cand, job, skill_text, {}, self._call_shape(cand),
                               self.bridge, self.now, self.pricing, settings=self._settings())
        except BridgeError as exc:
            # The call may have been billed before it failed (e.g. a timeout), and
            # all spend counts (spec 6): charge the drafting estimate.
            self.err(f"draft {cand.key}: {exc}")
            spend = policy.DRAFT_EST_USD
            self.report.spend_usd += spend
            self._spend_record(cand, spend, "bridge_error", f"drafting call failed: {exc}"[:1000])
            return spend, False
        self.report.spend_usd += spend
        if fix is None:
            self._spend_record(cand, spend, "unparseable", "unparseable draft")
            return spend, False
        try:
            return self._act(cand, job, fix, spend, bundled)
        except Exception as exc:  # noqa: BLE001 - keep the spend, report, go on
            self.err(f"draft {cand.key}: unexpected {type(exc).__name__}: {exc}")
            rec = self.ledger.get(fix.id)
            if rec is None:
                self._spend_record(cand, spend, "error", f"internal error: {type(exc).__name__}")
            elif rec.state == "shadow_pending" and rec.applied_at is None:
                self._close(rec, "rejected", f"internal error: {type(exc).__name__}")
            return spend, False

    def _spend_record(self, cand: Candidate, spend: float, tag: str, reason: str) -> None:
        """A rejected placeholder record that carries drafting spend with no usable fix."""
        digest = hashlib.sha256(f"{cand.key}|{tag}|{self.now!r}".encode()).hexdigest()[:8]
        day = datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y%m%d")
        placeholder = Fix(f"{day}-u{digest}", tag, cand.job_id or "", {}, cand.key)  # type: ignore[arg-type]
        self._record(placeholder, "rejected", spend, reason)

    def _act(self, cand: Candidate, job: dict | None, fix: Fix, spend: float,
             bundled: frozenset[str] | None) -> tuple[float, bool]:
        if self.ledger.get(fix.id) is not None:
            self.err(f"draft {fix.id} is already in the ledger")
            return spend, False
        try:
            reasons = self._limits(fix, cand, bundled)
        except BridgeError as exc:
            self._record(fix, "bridge_error", spend, f"scan: {exc}")
            self.err(f"check {fix.id}: {exc}")
            return spend, False
        if reasons:
            self._record(fix, "rejected", spend, "; ".join(reasons)[:1000])
            return spend, False
        if fix.kind in JOB_KINDS:
            return spend, self._shadow(fix, job, spend)
        if fix.kind in GLOBAL_KINDS:
            rec = self._record(fix, "shadow_pending", spend, "")
            try:
                out = apply(fix, self.ledger, self.bridge, self.root, self.now,
                            skills_root=self.cfg.skills_root)
            except ValueError as exc:
                self._close(rec, "rejected", f"apply refused: {exc}")
                self.report.rejected.append(rec.id)
                return spend, False
            self._went_live(out)
            return spend, False
        try:  # new_reach
            write_proposal(fix, self.root)
        except ValueError as exc:
            self._record(fix, "rejected", spend, f"proposal refused: {exc}")
            return spend, False
        self._record(fix, "proposed", spend, "")
        self.report.proposed.append(fix.id)
        return spend, False

    def _record(self, fix: Fix, state: str, spend: float, reason: str) -> FixRecord:
        rec = FixRecord(fix.id, fix.kind, fix.target, fix.candidate_key, state,  # type: ignore[arg-type]
                        self.now, reason=reason, spend_usd=spend,
                        needs_restart=needs_restart(fix))
        self.ledger.add(rec, fix)
        if state == "rejected":
            self.report.rejected.append(fix.id)
        return rec

    def _limits(self, fix: Fix, cand: Candidate, bundled: frozenset[str] | None) -> list[str]:
        reasons = []
        if fix.kind in JOB_KINDS and fix.target != cand.job_id:
            reasons.append("fix targets a job other than the candidate's")
        if fix.kind in GLOBAL_KINDS and self._global_active():
            reasons.append("a config or skill fix is already active")
        if fix.kind == "skill" and bundled is None:
            reasons.append("bundled skill manifest is missing")
        tools_dir = self.root / "tools"
        existing = frozenset(p.name for p in tools_dir.iterdir() if p.is_dir()) \
            if tools_dir.is_dir() else frozenset()
        ctx = LimitContext(
            copy_index=self.copy_index(), scan=self.bridge.scan,
            bundled_skills=bundled or frozenset(), skills_root=Path(self.cfg.skills_root),
            approved_callables=approved_callables(self.root, self.cfg.broker_state),
            existing_tools=existing, config_settings=self._settings())
        return reasons + check_fix(fix, ctx)

    def _shadow(self, fix: Fix, job: dict | None, spend: float) -> bool:
        if job is not None:  # F8: remember what the fix was drafted against
            fix = Fix(fix.id, fix.kind, fix.target, dict(fix.payload) | {BASE_KEY: {
                "prompt": job.get("prompt"), "enabled_toolsets": job.get("enabled_toolsets")}},
                fix.candidate_key)
        rec = self._record(fix, "shadow_pending", spend, "")
        if job is None:  # unreachable: job fixes come only from job candidates
            self._close(rec, "rejected", "no job to shadow")
            self.report.rejected.append(rec.id)
            return False
        if not self.plugin_loaded():  # unreachable: _draft_one checked it
            self._close(rec, "rejected", PLUGIN_NOT_LOADED)
            self.err(PLUGIN_NOT_LOADED)
            return False
        files = self._shadow_tool(fix)
        try:
            if files is not None:  # the shadow run needs the tool; shadow-only until go-live
                write_shadow_tool(self.root / "tools", fix.payload)
            trial.schedule_shadow(fix, job, self.bridge, self.root, self.now)
        except (ValueError, OSError) as exc:
            self._remove_shadow_tool(rec)
            self._close(rec, "rejected", f"shadow not scheduled: {exc}")
            self.report.rejected.append(rec.id)
            return False
        except BridgeError as exc:
            self._remove_shadow_tool(rec)
            self._close(rec, "bridge_error", f"shadow not scheduled: {exc}")
            self.err(f"schedule_shadow {fix.id}: {exc}")
            return False
        except _Failed as exc:  # _write_tool_dir: the tool already exists
            self._close(rec, "rejected", f"shadow not scheduled: {exc}")
            self.report.rejected.append(rec.id)
            return False
        self.report.shadowed.append(rec.id)
        # A composed tool is visible to the shadow only after a gateway restart.
        self.restart_wanted |= rec.needs_restart
        return True
