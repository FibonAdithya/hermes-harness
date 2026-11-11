# Tuner harm checks for config and skill changes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A config or skill change on trial is rolled back at the next cycle after one live run that fires a harm signal, using the same rule as job changes.

**Architecture:** All changes are in `tuner/hermes_tuner/cycle.py`. `judge_trials` calls `_harm_in_trial` for `GLOBAL_KINDS` too. `_harm_in_trial` drops runs that belong to a live job change, reads job status per run's own job, and picks outage evidence by change kind. `_harm` builds the self-inflicted-error baseline per run group for global changes. `harm.py` does not change.

**Tech Stack:** Python 3, pytest, uv (`tuner/`).

**Spec:** `docs/ai/specs/2026-11-10-tuner-global-harm-design.md`

## Global Constraints

- Work in worktree `/home/fibonadithya/hermes-harness/.worktrees/tuner-global-harm`, branch `docs/tuner-global-harm`. All commands run from `tuner/`.
- First time in the worktree: `uv sync --extra dev`. Otherwise `uv run pytest` fails with "Failed to spawn: pytest".
- Baseline at master `cb8f6de`: `uv run pytest -q` → `1018 passed` (MEASURED 2026-11-10).
- Existing job-change tests pass **unmodified**. The only existing test that changes is `test_config_trial_ignores_harm` (Task 1).
- `policy.HARM_CHECKS` still turns every harm check off.
- Reason strings are exactly `harm: <signal> (early)`, with `<signal>` the first hit in `harm.SIGNALS` order.
- Stage explicit paths only (never `git add -A` / `.` / `commit -a`). Run `git status --short` before each commit. End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No ssh to hermes-vm and no push from implementation tasks. Task 4 is the orchestrator's.

## Review Focus

1. **Shadow harm baseline for job changes must not move.** `evaluate_shadows` calls `_harm(rec, shadow, ...)` with a shadow run whose `job_id` is the shadow job, not the target. A per-group baseline applied to job kinds gives shadows an empty baseline. The existing `test_shadow_with_baseline_level_errors_goes_live` does NOT guard this: it has 2 errors, below `SELF_ERR_MIN = 3`, so it passes whatever the baseline is (audit, 2026-11-10). Task 1 adds `test_shadow_harm_baseline_is_the_target_jobs_runs`, which does.
2. **A rolled-back job change's old runs must not count against the global change on the next cycle.** Pinned by Task 2's `test_job_change_first_when_both_on_trial[no_new_bad_run]`.
3. **Several jobs in one run list.** For a global change, `runs` mixes triage, calendar and chats (History returns them ordered by `started_at`). "Newest run of a job" is the newest run *of that job*, not `runs[-1]`. Pinned by Task 3's test, where the calendar run is older than the triage run.
4. **A `list_jobs` failure during a global trial** still runs the result-based signals and logs `harm status <id>: ...`. Pinned by Task 3's `test_global_trial_harm_survives_job_status_failure`.
5. **A chat run has no `job_id`.** Group key `"chat"` comes from `source == "telegram"`, never from `job_id is None`. Pinned by Task 1's per-group test, where cron runs carry job ids.

---

### Task 1: Global changes get harm checks, with a per-group baseline

**Files:**
- Modify: `tuner/hermes_tuner/cycle.py`: `judge_trials` (~l.617–633), `_harm` (~l.420–459), new module helper next to `_global_run` (~l.64)
- Test: `tuner/tests/test_cycle.py`: replace `test_config_trial_ignores_harm` (l.2340–2350), add one test after it

**Interfaces:**
- Produces: `_run_group(s: Session) -> str`, which returns `"chat"` when `s.source == "telegram"`, else `s.job_id or ""`. Tasks 2 and 3 use it.
- Produces: `DOWN` test constant, `json.dumps({"error": "MCP server 'google' is unreachable after 3 consecutive failures. Auto-retry available in ~58s."})`, defined next to `ARG_ERR` (l.537). Tasks 2 and 3 use it.

- [ ] **Step 1: Write the failing tests** (replace `test_config_trial_ignores_harm`)

```python
def test_config_trial_rolls_back_on_a_harmful_chat(env):
    """One chat with three self-inflicted errors under a config change on trial.
    Catches: harm checks still limited to JOB_KINDS (in judge_trials or _harm)."""
    fix = config_trial(env, applied_days_ago=2)
    bad_chat = {"id": "20261019_010000_ab12cd", "source": "telegram", "started_at": NOW - 2 * 3600,
            "cost": 0.05, "in": 10_000, "out": 10, "calls": [(GET, {}, ARG_ERR)] * 3}
    build(env, [bad_chat])
    run_cycle(env, FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7}), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert (rec.state, rec.reason) == ("rolled_back", "harm: self_inflicted_errors (early)")


def chat(t, calls):
    return {"id": datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d_%H%M%S") + "_c0ffee",
            "source": "telegram", "started_at": t, "cost": 0.0, "in": 10_000, "out": 10,
            "calls": calls}


NOISY = [(GET, {}, ARG_ERR)] * 3 + [(GET, {"message_id": "m"}, "b")]  # error rate 0.75


@pytest.mark.parametrize("trial_run, rolled", [("chat", False), ("triage", True)])
def test_global_harm_baseline_is_per_group(env, trial_run, rolled):
    """Chats before go-live erred at 0.75; triage runs at 0. The same 0.75 run after
    go-live is normal for a chat and harmful for triage.
    Catches: one baseline over all global runs (the chat fires), or a baseline keyed
    on rec.target (empty for a config change, so the chat fires)."""
    fix = config_trial(env, applied_days_ago=0)  # live from NOW
    before = [chat(NOW - d * DAY, NOISY) for d in range(1, 6)] + triage_runs(days=range(1, 13))
    t = NOW + DAY
    after = (chat(t, NOISY) if trial_run == "chat" else
             {"id": sid(TRIAGE, t), "source": "cron", "started_at": t, "cost": 0.0,
              "in": 10_000, "out": 10, "calls": NOISY})
    build(env, before + [after])
    run_cycle(env, FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7}), NOW + 2 * DAY)
    rec = Ledger.load(env.root).get(fix.id)
    assert (rec.state == "rolled_back") is rolled
    if rolled:
        assert rec.reason == "harm: self_inflicted_errors (early)"


def test_shadow_harm_baseline_is_the_target_jobs_runs(env):
    """Review Focus 1. Triage runs err at 0.5; a shadow at 0.6 is ordinary for triage.
    Catches: a per-group baseline applied to job kinds (a shadow's group is the shadow
    job, so its baseline is empty and it fails)."""
    noisy = [r | {"calls": r["calls"] + [(GET, {}, ARG_ERR)] * 3} for r in triage_runs(range(2, 30))]
    fix = shadow_setup(env, old_mod=[(MOD, {"message_id": "m2", "add_label_ids": ["TRASH"]}, "ok")],
                       new_mod=[{"message_id": "m2", "add_label_ids": ["TRASH"]}],
                       base_runs=noisy, extra_calls=[(GET, {}, ARG_ERR)] * 3,
                       prompt="Fetch mail with tuned_x.")
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "trial", rec.reason
```

This guard test passes on master. It failed when the `_harm` baseline filter was mutated to `s.job_id == run.job_id` (MEASURED during the 2026-11-10 audit, using a temporary copy of the test).

Before relying on the parametrized test, check that `triage_runs`' calls contain no `tool_error` results, so their baseline rate is 0. If they do, build the triage baseline from `later_run([])`-style runs before `NOW` instead.

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_cycle.py -k "harmful_chat or per_group or target_jobs_runs" -q`
Expected: `test_config_trial_rolls_back_on_a_harmful_chat` and `[triage-True]` FAIL (state stays `trial`). `[chat-False]` and `test_shadow_harm_baseline_is_the_target_jobs_runs` pass (both are guards). Mutation checks in Step 5 cover them.

- [ ] **Step 3: Implement**

- `_run_group(s: Session) -> str` as in Interfaces.
- `judge_trials`: run `_harm_in_trial(rec, runs)` and its early rollback when `rec.live_from is not None and rec.kind in JOB_KINDS | GLOBAL_KINDS`. Keep `_early_failures` and the re-read check under `rec.kind in JOB_KINDS` only.
- `_harm`: the guard becomes `rec.kind not in JOB_KINDS | GLOBAL_KINDS`. For `JOB_KINDS` the baseline filter is unchanged (`s.job_id == rec.target`); see Review Focus 1. For `GLOBAL_KINDS`, the baseline runs are `_global_run(s) and _run_group(s) == _run_group(run)`, with the other filters unchanged (has calls, not a shadow, `low <= s.started_at < before`).

- [ ] **Step 4: Run the new tests and the full suite**

Run: `uv run pytest tests/test_cycle.py -k "harmful_chat or per_group" -q`, then `uv run pytest -q`
Expected: new tests PASS. Full suite: `1021 passed` (1018 − 1 replaced + 1 + 2 parametrized cases + 1 guard).

- [ ] **Step 5: Mutation check**

1. Change the global baseline filter to `_global_run(s)` only, run the per-group test, and confirm `[chat-False]` FAILS. Restore.
2. Revert the `judge_trials` condition to `JOB_KINDS`, and confirm both new tests' rollback cases FAIL. Restore.
3. Apply the per-group filter to job kinds too, and confirm `test_shadow_harm_baseline_is_the_target_jobs_runs` FAILS. Restore.
Record each result in the commit message body as one line.

- [ ] **Step 6: Commit**

```bash
git status --short
git add tuner/hermes_tuner/cycle.py tuner/tests/test_cycle.py
git commit -m "feat(tuner): harm checks for config and skill changes on trial"
```

### Task 2: A run that belongs to a live job change is not counted against the global change

**Files:**
- Modify: `tuner/hermes_tuner/cycle.py`: `_harm_in_trial` (~l.674–706), new method on `_Cycle`
- Test: `tuner/tests/test_cycle.py`, after Task 1's tests

**Interfaces:**
- Consumes: `_run_group`, `DOWN` (Task 1); `job_prompt_trial(env)`, `later_run(calls, day)`, `config_trial(env, applied_days_ago)`, `SETUP_SESSIONS` (existing).
- Produces: `_Cycle._owned_by_job_change(self, run: Session) -> bool`. It returns True when some ledger record `j` has `j.kind in JOB_KINDS`, `j.target == run.job_id`, `j.live_from is not None and j.live_from < run.started_at`, and `(j.decided_at is None or j.decided_at > run.started_at)`. This is judged on the run's time, never on `j.state`.

- [ ] **Step 1: Write the failing tests**

```python
TRIPPED = [(GET, {}, ARG_ERR), (GET, {}, DOWN)]  # server_tripped, no baseline needed


def both_on_trial(env):
    fix, bridge = job_prompt_trial(env)       # job change on triage, live from NOW
    cfg_fix = config_trial(env, applied_days_ago=0)   # config change, live from NOW
    bridge.config[CFG_KEY] = 0.7  # keep plugins.enabled, which the FakeBridge wrapper sets
    return fix, cfg_fix, bridge


@pytest.mark.parametrize("new_bad_run", [False, True], ids=["no_new_bad_run", "new_bad_run"])
def test_job_change_first_when_both_on_trial(env, new_bad_run):
    """Day 1: a tripped triage run, both changes live. Only the job change goes.
    Day 3, after it went: the config goes only if triage is still bad.
    Catches: no skip (config goes on day 1); a skip keyed on the job change's state at
    cycle time (config goes on day 3 for the day-1 run); skipping triage forever."""
    fix, cfg_fix, bridge = both_on_trial(env)
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + [later_run(TRIPPED, day=1)])
    run_cycle(env, bridge, NOW + 2 * DAY)
    led = Ledger.load(env.root)
    assert (led.get(fix.id).state, led.get(fix.id).reason) == (
        "rolled_back", "harm: server_tripped (early)")
    assert led.get(cfg_fix.id).state == "trial"
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + [later_run(TRIPPED, day=1)]
          + ([later_run(TRIPPED, day=3)] if new_bad_run else []))
    run_cycle(env, bridge, NOW + 4 * DAY)
    rec = Ledger.load(env.root).get(cfg_fix.id)
    if new_bad_run:
        assert (rec.state, rec.reason) == ("rolled_back", "harm: server_tripped (early)")
    else:
        assert rec.state == "trial"


def test_chat_counts_against_global_while_a_job_change_is_on_trial(env):
    """Catches: skipping every run while any job change is live."""
    fix, cfg_fix, bridge = both_on_trial(env)
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + [chat(NOW + DAY, TRIPPED)])
    run_cycle(env, bridge, NOW + 2 * DAY)
    led = Ledger.load(env.root)
    assert led.get(cfg_fix.id).state == "rolled_back"
    assert led.get(fix.id).state == "trial"
```

If `job_prompt_trial` plus a hand-added config record trips a precondition (for example, the first `run_cycle` inside `job_prompt_trial` drafting a global candidate), add the config record after `job_prompt_trial` returns, as written here, and say so in the commit body. Do not loosen any assertion.

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_cycle.py -k "job_change_first or chat_counts_against" -q`
Expected: both `test_job_change_first...` cases FAIL at the day-1 assertion (config rolled back). The chat test may pass already. Mutation 3 in Step 5 covers it.

- [ ] **Step 3: Implement**

Add `_owned_by_job_change` as in Interfaces. In `_harm_in_trial`, for `rec.kind in GLOBAL_KINDS`, drop runs where `_owned_by_job_change(run)` before evaluating any signal. Job kinds are unchanged.

- [ ] **Step 4: Run the new tests and the full suite**

Run: `uv run pytest tests/test_cycle.py -k "job_change_first or chat_counts_against" -q`, then `uv run pytest -q`
Expected: all PASS. Full suite `1024 passed` (+2 parametrized cases, +1).

- [ ] **Step 5: Mutation check**

1. Make `_owned_by_job_change` test `j.state in ACTIVE_STATES` instead of `decided_at`, and confirm `[no_new_bad_run]` FAILS. Restore.
2. Make it return True for any triage run, and confirm `[new_bad_run]` FAILS. Restore.
3. Drop all runs for global kinds whenever any job change has `live_from` set, and confirm the chat test FAILS. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add tuner/hermes_tuner/cycle.py tuner/tests/test_cycle.py
git commit -m "feat(tuner): blame a live job change before a global one"
```

### Task 3: `run_failed` for global changes: each job's own status, outage only from jobs outside the trial set

**Files:**
- Modify: `tuner/hermes_tuner/cycle.py`: `_harm_in_trial` (status and outage block)
- Test: `tuner/tests/test_cycle.py`, after Task 2's tests

**Interfaces:**
- Consumes: `_run_group`, `chat` (Task 1); `TRIPPED` (Task 2); `iso`, `later_run`, `sid` (existing).
- Produces: nothing new for later tasks.

- [ ] **Step 1: Write the failing tests**

```python
OUTSIDE_JOB = "aaaabbbbcccc"  # a cron job outside policy.EDITABLE_JOBS (OTHER is taken, l.1076)


def global_trial_with_runs(env):
    cfg_fix = config_trial(env, applied_days_ago=0)
    t_cal = NOW - 3 * 3600 + 6 * 3600 + DAY     # day-1 calendar run, 06:00
    cal = {"id": sid(CAL, t_cal), "source": "cron", "started_at": t_cal, "cost": 0.0,
           "in": 10_000, "out": 10,
           "calls": [("mcp__google__get_events", {"calendar_id": "primary"}, "events")]}
    build(env, [cal, later_run([], day=1)])      # calendar (06:00) is never the last run
    return cfg_fix, FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7}), t_cal


def test_global_trial_run_failed_reads_each_jobs_own_status(env):
    """Calendar's run failed; triage's is fine.
    Catches: status read only for rec.target (a config key, so never), or only for
    the last run in list order."""
    cfg_fix, bridge, t_cal = global_trial_with_runs(env)
    bridge.jobs[CAL] |= {"last_status": "error", "last_run_at": iso(t_cal + 60)}
    run_cycle(env, bridge, NOW + 2 * DAY)
    rec = Ledger.load(env.root).get(cfg_fix.id)
    assert (rec.state, rec.reason) == ("rolled_back", "harm: run_failed (early)")


@pytest.mark.parametrize("other, rolled", [(CAL, True), (OUTSIDE_JOB, False)])
def test_global_outage_only_from_jobs_outside_the_trial_set(env, other, rolled):
    """Triage failed, and another job failed within the outage window.
    Catches: calendar excusing triage under a global change, or no outage test at all."""
    cfg_fix, bridge, t_cal = global_trial_with_runs(env)
    t = NOW - 3 * 3600 + 7 * 3600 + DAY
    bridge.jobs[TRIAGE] |= {"last_status": "error", "last_run_at": iso(t + 60)}
    bridge.jobs.setdefault(other, jobs()[CAL] | {"id": other, "name": "other"})  # full job shape
    bridge.jobs[other] |= {"last_status": "error", "last_run_at": iso(t + 120)}
    run_cycle(env, bridge, NOW + 2 * DAY)
    rec = Ledger.load(env.root).get(cfg_fix.id)
    assert (rec.state == "rolled_back") is rolled


def test_global_trial_harm_survives_job_status_failure(env):
    """Review Focus 4. Catches: a list_jobs failure aborting the global check."""
    cfg_fix = config_trial(env, applied_days_ago=0)
    build(env, [chat(NOW + DAY, TRIPPED)])
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7})
    bridge.fail = {"list_jobs"}
    report = run_cycle(env, bridge, NOW + 2 * DAY)
    assert Ledger.load(env.root).get(cfg_fix.id).state == "rolled_back"
    assert any(e.startswith(f"harm status {cfg_fix.id}:") for e in report.errors)
```

In the `CAL` case of the outage test, CAL's error is within 6 h of the triage run but is not CAL's own run status (no CAL run after `t_cal` is in the db, and `last_run_at` is after `t_cal`). So CAL's own `run_failed` may also fire. Either way the expected state is `rolled_back`, and the assertion does not depend on which run fired.

- [ ] **Step 2: Run and confirm they fail**

Run: `uv run pytest tests/test_cycle.py -k "each_jobs_own_status or outside_the_trial_set or global_trial_harm_survives" -q`
Expected: `each_jobs_own_status` and `[cc68e24f21cd-True]` FAIL (state `trial`). `[aaaabbbbcccc-False]` passes. The `list_jobs` test may pass already.

- [ ] **Step 3: Implement**

In `_harm_in_trial`:
- **Status:** for each run whose `job_id` is a key of `policy.EDITABLE_JOBS` (job kinds: `rec.target`), take that job's `list_jobs` entry. Apply `last_status` / `last_delivery_error` only when the run is that job's newest run in `runs` by `started_at`, `last_run_at >= run.started_at`, and `not self._ran_again(run.job_id, run.started_at, ran)`. For job kinds this is the same result as today.
- **Outage:** excluded ids are `{rec.target}` for `JOB_KINDS` and `set(policy.EDITABLE_JOBS)` for `GLOBAL_KINDS`. Otherwise the window test is unchanged.

- [ ] **Step 4: Run the new tests and the full suite**

Run: `uv run pytest -q`
Expected: `1028 passed` (+1, +2 parametrized cases, +1). Existing `test_trial_run_failed_unless_another_job_failed_too` and `test_trial_job_status_older_than_the_run_is_ignored` pass unmodified.

- [ ] **Step 5: Mutation check**

1. Pick the newest run by list position (`runs[-1]`), and confirm `each_jobs_own_status` FAILS. Restore.
2. Exclude only the run's own job for global kinds, and confirm `[cc68e24f21cd-True]` FAILS. Restore.
3. Drop the outage test for global kinds, and confirm `[aaaabbbbcccc-False]` FAILS. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add tuner/hermes_tuner/cycle.py tuner/tests/test_cycle.py
git commit -m "feat(tuner): global trials read each job's own status and outage"
```

### Task 4: Live-data acceptance and PR (orchestrator only)

- [ ] **Step 1:** Branch dry run on hermes-vm from a scratch copy, with no install:
  `git archive HEAD tuner | ssh hermes-vm 'rm -rf ~/tuner-scratch-gh && mkdir ~/tuner-scratch-gh && tar x -C ~/tuner-scratch-gh'`, then
  `ssh hermes-vm 'cd ~/tuner-scratch-gh/tuner && ~/hermes-harness/tuner/.venv/bin/python -c "import hermes_tuner; print(hermes_tuner.__file__)" && ~/hermes-harness/tuner/.venv/bin/python -m hermes_tuner run --dry-run'`.
  Expected: the path printed is under `~/tuner-scratch-gh`, and `planned` contains `rollback 20261029-d4ec5691`. If `20261029-d4ec5691` is no longer in `trial` when this runs, say so and skip this check.
- [ ] **Step 2:** The deployed master's dry run (`cd ~/hermes-harness/tuner && .venv/bin/python -m hermes_tuner run --dry-run`) does not plan that rollback.
- [ ] **Step 3:** Push (probe ssh first, HTTPS fallback), then open the PR with the spec, plan, test delta (1018 → measured count) and the dry-run outputs. Poll `gh pr checks <n> --watch`. Merging is the owner's call, because merging deploys.
