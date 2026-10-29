"""Cycle steps 5-7: budget check, detection, and drafting each selected candidate
into a rejected, shadow-pending, trial or proposed record (spec 4.2-4.4, 6).

A mixin for `cycle._Cycle`: it uses the cycle's ledger, sessions, history, report
and its `_close`, `_went_live`, `_shadow_tool` and `_remove_shadow_tool` helpers.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from . import policy, trial
from .backoff import BACKOFF_SKIP_AFTER, backoff, draft_history, input_hash, reason_class
from .bridge import BridgeError
from .budget import DAY, JOB_KINDS, iso_week_start, job_runs, savings, tuner_spend
from .detect import SCHEMA_TOOL, SKILL_TOOL, Candidate, detect
from .draft import draft, turn_layout
from .fingerprint import CopyIndex
from .history import HistoryUnavailable
from .ledger import (ACTIVE_STATES, APPLIED_STATES, BASE_KEY, FixRecord, _Failed, apply,
                     needs_restart, write_shadow_reducer, write_shadow_tool)
from .limits import LimitContext, changeable_settings, check_fix
from .measure import priced_cost
from .model import Fix, Session
from .proposals import approved_callables, write_proposal
from .replay import composed_gate, composed_monthly, replay_composed, search_reducer

COPY_INDEX_DAYS = 14
REPLAY_WINDOW_DAYS = policy.REPLAY_WINDOW_DAYS
RELAY_PATH = Path(__file__).resolve().parents[1] / "plugin" / "hermes-tuner" / "relay.py"
PLUGIN_NOT_LOADED = "plugin not loaded; shadow not scheduled"
GLOBAL_KINDS = frozenset({"config", "skill"})
GLOBAL_CANDIDATES = ("schema_lookup", "skill_load")
JOB_CANDIDATES = ("sequence", "reread", "bytes")
# A reducer blocks a second search for its (job, tool) from shadow until rolled back:
# once one is live, history holds reduced text and a new search would stack on it.
REDUCER_BLOCKING = ACTIVE_STATES | APPLIED_STATES


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


@functools.cache
def load_relay():
    """The plugin's relay module, loaded from its file (it is not a package module)."""
    spec = importlib.util.spec_from_file_location("hermes_tuner_replay_relay", RELAY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DraftSteps:
    # Composed-tool replay: a callable with relay.run_program's signature, and the
    # runner argv for a container name. The cycle's constructor sets both; None means
    # the plugin's relay in Docker (relay.production_argv).
    run_program: Callable[..., str] | None = None
    replay_argv: Callable[[str], list[str]] | None = None
    # Seconds of composed replay this cycle has used (self.clock), of REPLAY_CYCLE_BUDGET_S.
    replay_spent_s: float = 0.0
    # Live jobs read this drafting step (job id -> job, or None when it could not be
    # read), so _select, _draft_one and _reduce_one read each job once.
    _job_cache: dict[str, dict | None] | None = None
    # backoff.input_hash of the candidate being drafted; every record it writes carries it.
    _cur_input_hash: str | None = None

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
        cands = detect(window, now, pricing, self.skill_names, aliases=self._aliases())
        self._job_cache = {}
        for cand in self._select(cands):
            job_cost = self._job_cost(cand.job_id) if cand.job_id else 0.0
            reduce = self._reducer_route(cand)  # no model call: only the shadow run costs
            projected = (0.0 if reduce else policy.DRAFT_EST_USD) + job_cost
            if week + projected > policy.WEEKLY_CAP_USD:
                self.report.budget = (f"cap: week spend ${week:.2f} + projected "
                                      f"${projected:.2f} > ${policy.WEEKLY_CAP_USD:.2f} "
                                      "(ESTIMATE)")
                continue
            if self.dry:
                self.plan(f"draft {cand.key}" + (f" for job {cand.job_id}" if cand.job_id else ""))
                continue
            if reduce:
                spend, shadowed = self._reduce_one(cand)
            else:
                spend, shadowed = self._draft_one(cand)
            week += spend + (job_cost if shadowed else 0.0)

    def _aliases(self) -> dict[str, str]:
        """Composed tool name -> the one tool it calls, from tools/*/tool.json whose
        `calls` has length 1. Unreadable or malformed files are skipped."""
        out: dict[str, str] = {}
        try:
            paths = sorted((self.root / "tools").glob("*/tool.json"))
        except OSError:
            return out
        for path in paths:
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = spec.get("name") if isinstance(spec, dict) else None
            calls = spec.get("calls") if isinstance(spec, dict) else None
            if (isinstance(name, str) and isinstance(calls, list) and len(calls) == 1
                    and isinstance(calls[0], str)):
                out[name] = calls[0]
        return out

    @staticmethod
    def _reducer_route(cand: Candidate) -> bool:
        """A bytes candidate of a reducer job for an allowlisted tool goes to the
        deterministic replay search instead of a model draft."""
        return (cand.kind == "bytes" and cand.job_id in policy.REDUCER_JOBS
                and cand.key.split(":", 1)[1] in policy.REDUCER_TOOLS)

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
        """The first candidate per editable job, and one global one, among those worth
        at least MIN_DRAFT_MONTHLY_USD a month and not backed off. Backed-off
        candidates are dropped before the pick, so a job's next candidate is tried."""
        cands = [c for c in cands if c.est_monthly_usd >= policy.MIN_DRAFT_MONTHLY_USD]
        picked = []
        for job in sorted(policy.EDITABLE_JOBS):
            if job in policy.NEVER_TOUCH_JOBS or self.ledger.active_for(job) is not None:
                continue
            ok = self._not_backed_off(
                [c for c in cands if c.job_id == job and c.kind in JOB_CANDIDATES])
            if ok:
                picked.append(ok[0])
        if not self._global_active():
            ok = self._not_backed_off([c for c in cands if c.kind in GLOBAL_CANDIDATES])
            if ok:
                picked.append(ok[0])
        return picked

    def _cand_records(self, cand: Candidate) -> list[FixRecord]:
        """The candidate's records. Job candidates' keys ("seq:...", "bytes:...") can
        recur in another job, so records that target another job are left out."""
        return [r for r in self.ledger.records if r.candidate_key == cand.key
                and not (cand.job_id is not None and r.target != cand.job_id
                         and r.target in policy.EDITABLE_JOBS)]

    def _cand_input_hash(self, cand: Candidate) -> str | None:
        """What a draft of `cand` would be made from now, or None when its job cannot
        be read (reported by _get_job)."""
        job = None
        if cand.job_id is not None:
            job = self._get_job(cand.job_id)
            if job is None:
                return None
        return input_hash(job, self._skill_text(cand))

    def _skill_text(self, cand: Candidate) -> str | None:
        """A skill_load candidate's SKILL.md text; None for other kinds or when unread."""
        if cand.kind != "skill_load":
            return None
        path = _skill_file(self.cfg.skills_root, cand.key.split(":", 1)[1])
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _not_backed_off(self, cands: list[Candidate]) -> list[Candidate]:
        """`cands` without those backoff() skips or parks; parked ones are counted in
        the report. A job is read only when a candidate has enough failed outcomes to
        be skipped or parked. When its job cannot be read, no candidate of it is
        drafted this cycle (_get_job reported the error once)."""
        out = []
        for c in cands:
            recs = self._cand_records(c)
            if sum(1 for r in recs if reason_class(r)) < BACKOFF_SKIP_AFTER:
                out.append(c)  # too few failed outcomes to skip or park: no job read
                continue
            h = self._cand_input_hash(c)
            if h is None:
                return []
            verdict = backoff(recs, c.key, h, self.now)
            if verdict == "parked":
                self.report.parked += 1
            elif verdict == "ok":
                out.append(c)
        return out

    def _call_shape(self, cand: Candidate) -> list[tuple[str, tuple[str, ...]]]:
        """Tool names and argument KEYS of the candidate's most recent session."""
        recent = self._recent(cand)
        return [] if recent is None else [(c.name, c.arg_keys) for c in recent.calls]

    def _turn_layout(self, cand: Candidate) -> list[int]:
        """Calls per turn of the session _call_shape describes."""
        recent = self._recent(cand)
        return [] if recent is None else turn_layout(recent.calls)

    def _recent(self, cand: Candidate) -> Session | None:
        def matches(s: Session) -> bool:
            if cand.job_id is not None:
                return s.job_id == cand.job_id and bool(s.calls)
            if cand.kind == "schema_lookup":
                return sum(c.name == SCHEMA_TOOL for c in s.calls) >= 2
            name = cand.key.split(":", 1)[1]
            return name in self.skill_names.get(s.id, ())
        return max((s for s in self.window if matches(s)),
                   key=lambda s: (s.started_at, s.id), default=None)

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
        self._cur_input_hash = None
        job = None
        if cand.job_id is not None:
            job = self._get_job(cand.job_id)
            if job is None:
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
        self._cur_input_hash = input_hash(job, skill_text)
        try:
            fix, spend = draft(cand, job, skill_text, {}, self._call_shape(cand),
                               self.bridge, self.now, self.pricing, settings=self._settings(),
                               turn_layout=self._turn_layout(cand),
                               history=draft_history(self._cand_records(cand), cand.key))
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

    def _get_job(self, job_id: str) -> dict | None:
        """The live job, or None after reporting why (plugin not loaded, bridge
        failure, missing job). A job fix needs a guarded shadow run (F4). Read once
        per drafting step: a second call returns the first answer, unreported."""
        if self._job_cache is None:
            self._job_cache = {}
        if job_id in self._job_cache:
            return self._job_cache[job_id]
        self._job_cache[job_id] = job = self._read_job(job_id)
        return job

    def _read_job(self, job_id: str) -> dict | None:
        if not self.plugin_loaded():
            self.err(PLUGIN_NOT_LOADED)
            return None
        try:
            job = self.bridge.get_job(job_id)
        except BridgeError as exc:
            self.err(f"get_job {job_id}: {exc}")
            return None
        if job is None:
            self.err(f"job {job_id} not found")
        return job

    def _reducer_taken(self, job_id: str, tool: str) -> bool:
        """True when a reducer for (job, tool) is shadow-pending, applied, on trial or
        kept. The tool comes from the stored fix; an unreadable one counts as taken."""
        for r in self.ledger.records:
            if r.kind != "reducer" or r.target != job_id or r.state not in REDUCER_BLOCKING:
                continue
            try:
                payload = self.ledger.fix(r.id).payload
            except (OSError, ValueError, TypeError):
                return True
            if not isinstance(payload, dict) or payload.get("tool") == tool:
                return True
        return False

    def _reduce_one(self, cand: Candidate) -> tuple[float, bool]:
        """Search replayed runs for a reducer of the candidate's tool; shadow what it
        finds. No model call: spend is 0. Returns (spend, shadow scheduled)."""
        self._cur_input_hash = None
        job_id, tool = cand.job_id, cand.key.split(":", 1)[1]
        if self._reducer_taken(job_id, tool):  # local check first: no bridge call
            return 0.0, False
        job = self._get_job(job_id)
        if job is None:
            return 0.0, False
        self._cur_input_hash = input_hash(job, None)
        prompt = job.get("prompt")
        exclude = [prompt] if isinstance(prompt, str) else []
        try:
            runs = self.history.replay_runs(job_id, self.now - REPLAY_WINDOW_DAYS * DAY)
            result = search_reducer(runs, tool, job_id, exclude, self.pricing)
        except HistoryUnavailable as exc:
            self.err(f"replay {cand.key}: {exc}")
            return 0.0, False
        except Exception as exc:  # noqa: BLE001 - the type only: a message could hold result text
            self.err(f"reducer search {cand.key}: unexpected {type(exc).__name__}")
            return 0.0, False
        if result.spec is None:
            self._spend_record(cand, 0.0, "reducer", f"reducer search: {result.reason}")
            return 0.0, False
        # The candidate cleared the floor on all of the tool's bytes, aliased calls
        # included; the reducer cuts only direct calls, so its own saving must too.
        # est_saving_per_run is averaged over the runs that call the tool: times their
        # count it is the replayed window's total saving, scaled here to 30 days.
        monthly = result.est_saving_per_run * result.sessions * 30 / REPLAY_WINDOW_DAYS
        if monthly < policy.MIN_DRAFT_MONTHLY_USD:
            self._spend_record(cand, 0.0, "reducer", "reducer search: saving_below_floor")
            return 0.0, False
        day = datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y%m%d")
        digest = hashlib.sha256(
            (cand.key + json.dumps(result.spec, sort_keys=True)).encode()).hexdigest()[:8]
        fix = Fix(f"{day}-r{digest}", "reducer", job_id, result.spec, cand.key)
        fresh = self.ledger.get(fix.id) is None
        try:
            out = self._act(cand, job, fix, 0.0, None)
        except Exception as exc:  # noqa: BLE001 - report, go on
            self.err(f"reduce {cand.key}: unexpected {type(exc).__name__}")
            rec = self.ledger.get(fix.id)
            if rec is None:
                self._spend_record(cand, 0.0, "reducer", f"internal error: {type(exc).__name__}")
            elif rec.state == "shadow_pending" and rec.applied_at is None:
                self._remove_shadow_tool(rec)
                self._close(rec, "rejected", f"internal error: {type(exc).__name__}")
            return 0.0, False
        rec = self.ledger.get(fix.id)
        if fresh and rec is not None:
            rec.est_saving_per_run = result.est_saving_per_run
            self.ledger.save()
        return out

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
        per_run = None
        if fix.kind == "composed_tool":
            reason, per_run = self._replay_gate(cand, fix)
            if reason is not None:  # no shadow: the drafting call was paid
                self._record(fix, "rejected", spend, reason)
                return spend, False
        if fix.kind in JOB_KINDS:
            shadowed = self._shadow(fix, job, spend)
            rec = self.ledger.get(fix.id)
            if shadowed and per_run is not None and rec is not None:
                rec.est_saving_per_run = per_run
                self.ledger.save()
            return spend, shadowed
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

    @staticmethod
    def _turns_usd(cand: Candidate) -> float:
        """The turns-removed part of a composed tool's monthly estimate: a sequence
        candidate's own estimate. Any other kind's estimate is carried bytes, which the
        replay prices itself."""
        return cand.est_monthly_usd if cand.kind == "sequence" else 0.0

    def _replay_gate(self, cand: Candidate, fix: Fix) -> tuple[str | None, float | None]:
        """Replay a composed tool on recorded runs of its job. Returns (rejection reason
        or None, est_saving_per_run or None). Reasons and errors carry counts and
        exception types only: a message could hold result text."""
        run_program = self.run_program
        argv_for = self.replay_argv
        try:
            if run_program is None or argv_for is None:
                relay = load_relay()
                run_program = run_program or relay.run_program
                argv_for = argv_for or relay.production_argv
            runs = self.history.replay_runs(fix.target, self.now - REPLAY_WINDOW_DAYS * DAY)
            left = policy.REPLAY_CYCLE_BUDGET_S - self.replay_spent_s
            rep = replay_composed(fix.payload, runs, run_program, pricing=self.pricing,
                                  argv_for=argv_for, clock=self.clock, budget_s=left)
            self.replay_spent_s += rep.elapsed_s
        except HistoryUnavailable:
            self.err(f"replay {fix.id}: history unavailable")
            return "replay: history unavailable", None
        except Exception as exc:  # noqa: BLE001 - the type only
            self.err(f"replay {fix.id}: unexpected {type(exc).__name__}")
            return f"replay: internal error {type(exc).__name__}", None
        monthly = composed_monthly(rep, self._turns_usd(cand))
        reason = composed_gate(rep, monthly)
        if reason is not None or not rep.eligible:
            return reason, None
        return None, monthly / (rep.eligible * 30 / REPLAY_WINDOW_DAYS)

    def _record(self, fix: Fix, state: str, spend: float, reason: str) -> FixRecord:
        rec = FixRecord(fix.id, fix.kind, fix.target, fix.candidate_key, state,  # type: ignore[arg-type]
                        self.now, reason=reason, spend_usd=spend,
                        needs_restart=needs_restart(fix), input_hash=self._cur_input_hash)
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
                write_shadow_tool(self.root / "tools", fix.payload, fix.id)
            if fix.kind == "reducer":  # applies only in this fix's shadow until go-live
                write_shadow_reducer(self.root, fix.payload, fix.id)
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
