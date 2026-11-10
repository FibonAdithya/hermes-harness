# Tuner harm checks for config and skill changes — design

Date: 2026-11-10. Addendum to `2026-11-04-tuner-self-repair-design.md` (Loop 1).
Status: approved in chat by the owner on 2026-11-10, pending this write-up.

## Why

Loop 1 (PR #37) runs harm checks on every live run of a **job** change on trial
(`JOB_KINDS`: job_prompt, composed_tool, reducer) and rolls a harmful change back
at the next 03:30 cycle. It excluded config and skill changes (`GLOBAL_KINDS`)
because "their runs include every owner chat and one bad chat would roll back a
global fix" (self-repair spec §2.1). Those changes are judged on cost alone:
after 7 days (`GLOBAL_TRIAL_DAYS`), with a 21-day deadline.

That leaves a gap that is live today. Config change `20261029-d4ec5691`
(`tools.tool_search.listing`) went live on 2026-11-09 03:30. The 2026-11-10 08:00
morning briefing made three malformed `get_gmail_message_content` calls
(`expected_sha256 and page_size require page_offset`), and Hermes then marked the
Google MCP server unreachable. This is the same pattern as the 2026-11-04 incident.
A dry run (`python -m hermes_tuner run --dry-run` on hermes-vm, 2026-11-10) planned
no rollback. Whether the config caused the failure is not established: Hermes's own
self-improvement step also edited the gmail skill at 03:30 on 11-09.

The owner wants this gap closed before the drafting budget is raised, so that new
config and skill drafts have the same undo as job drafts.

## Evidence the exclusion was too cautious

| claim | value | MEASURED / ESTIMATED | command | when |
|---|---|---|---|---|
| global-trial runs in the 28 days to 2026-11-10 | 41 chats, 24 briefing (`3bc4e11d29b1`), 24 calendar (`cc68e24f21cd`) | MEASURED | replay script below, on hermes-vm | 2026-11-10 |
| runs firing `server_tripped` or `self_inflicted_errors` | 2 (briefing 11-04 08:00, briefing 11-10 08:00), 0 chats | MEASURED | same | 2026-11-10 |
| 11-04 hit was tuner-caused | rolled back as `harm: unknown_tool (early)` on 11-07 | MEASURED | `ledger.json`, record `20261103-23272612` | 2026-11-10 |

The replay ran `harm.CHECKS["server_tripped"]` and `["self_inflicted_errors"]` over
each run's `call_results`, with a per-group baseline (chats vs. chats, each job vs.
its own runs) over the preceding 28 days. `unknown_tool` and `run_failed` were not
replayed: the first needs a fix's text, the second the job status at run time.

So in 28 days the signals fired only on runs that were broken, and never on a chat.

## Goals

- A config or skill change on trial is rolled back at the next cycle after **one**
  live run that fires any harm signal. Same rule as job changes (owner, 2026-11-10).
- Same reason format (`harm: <signal> (early)`), same rollback path, same one-line
  outbox notice as Loop 1.

## Non-goals

- Shadow runs for global changes. They have none today, and this adds none.
- Hermes's own self-edits (the self-improvement review patching skills). Not a
  tuner change, so out of scope, as in the Loop 1 non-goals.
- Changing the cost verdict, the 7-day trial or the 21-day deadline.
- Repair requests (Loop 2) for global rollbacks.
- Raising the drafting budget. That comes after this ships, as a separate change.

## Anti-goals

- A chat measured against a job's error rate, or one job against another's. That
  would turn normal chat error rates into rollbacks.
- Any harm check getting weaker for job changes.

## Design (decided)

1. **Where:** `_Cycle.judge_trials` runs `_harm_in_trial` for records whose kind
   is in `GLOBAL_KINDS` as well as `JOB_KINDS`, before the verdict and deadline
   logic. A hit calls `_rollback(rec, f"harm: {sig} (early)", runs)` and skips
   the rest of that record, as for job changes. The early-failure and re-read
   checks stay job-only.
2. **Which runs:** the existing `_trial_runs(rec)` for a global change. That is
   runs started after `live_from`, with at least one tool call, not shadows, for
   which `_global_run` holds (Telegram chats and `policy.EDITABLE_JOBS` runs).
   This includes records already in trial when the change ships.
3. **Baseline per group:** for the `self_inflicted_errors` baseline, a run's
   group is "chat" (`source == "telegram"`) or its `job_id`. The baseline is the
   error rates of runs in the same group, under the same filters as today
   (has calls, not a shadow), that started in the `BASELINE_DAYS` before
   `live_from`. For job changes the group is the target job, which is unchanged
   behaviour.
4. **Signals:** all four from `harm.SIGNALS`.
   - `unknown_tool`: unchanged. A name counts only if it is not registered and the
     change's own payload text names it (config key/value, or skill `text`).
   - `server_tripped`, `self_inflicted_errors`: unchanged functions.
   - `run_failed`: for a global change, job status comes from each editable job's
     own `list_jobs` entry. It applies to that job's newest run in `runs`, and only
     if the job has not run again since (the existing `_ran_again` rule, keyed on
     the run's `job_id` instead of `rec.target`). Chats have no status.
     *Outage test (builder-check call, 2026-11-10):* for a global change, only jobs
     outside `policy.EDITABLE_JOBS` count as evidence of an outage. The two
     editable jobs both run under the change, so they can fail together because of
     it. If one could excuse the other, `run_failed` could never catch a change that
     breaks both. For job changes the test is unchanged (any job other than
     `rec.target`).
5. **`_harm` guard:** the `rec.kind not in JOB_KINDS` early return in `_harm`
   becomes `not in JOB_KINDS | GLOBAL_KINDS`. `policy.HARM_CHECKS` still turns
   all of it off.
6. **Attribution: job change first** (owner, 2026-11-10). `_global_active`
   allows only one global change at a time, but `cycle_draft._select` picks job
   candidates per editable job independently of it, so a job change and a global
   change can be on trial together. In the global check, a run of job J is skipped
   when a job change targeting J was live when the run started: its `live_from`
   is before the run's start, and it had not been decided (`decided_at` unset or
   after the run's start). The test is on the run's time, not on the job change's
   state at cycle time. Otherwise, the cycle after a job change is rolled back
   would count its bad runs against the global change. That run is then judged
   only against the job change, by the existing job-change check. Runs of J that
   start after the job change is decided count against the global change again. A
   guilty global change is caught one run later, and an innocent one keeps its
   trial. Chats and runs of other jobs still count against the global change.
7. **Rollback mechanics:** unchanged. A config rollback with `needs_restart` sets
   `restart_wanted`, and the existing quiet-window restart applies it. A skill whose
   file changed since apply ends `owner_changed`, as today.

## Expected effect on deploy

Merging to master deploys to hermes-vm within ~15 minutes (`harness-pull.timer`).
At the next 03:30 cycle the tuner should roll back `20261029-d4ec5691`, because the
2026-11-10 08:00 briefing run fires `server_tripped`. This happens whether or not
the config caused that failure. Rolling back an innocent change costs one trial,
which is the stricter direction the owner accepted.

## Acceptance

- Unit tests for each decided point, including: a chat run is judged against chat
  baselines only; `run_failed` reads the run's own job status; a job change's
  behaviour is unchanged (existing job-change tests pass unmodified).
- With a job change on trial for triage and a config change on trial, a harmful
  triage run rolls back only the job change. A harmful triage run after the job
  change has rolled back rolls back the config change. A harmful chat in either
  period rolls back the config change.
- `tests/test_cycle.py::test_config_trial_ignores_harm` asserts the old exclusion
  (a config change survives a chat with 3 argument errors). It is inverted into a
  test of the new behaviour: that chat now rolls the config back. This is the only
  existing test expected to change (builder-check call, 2026-11-10).
- Before merge: `python -m hermes_tuner run --dry-run` on hermes-vm, using a scratch
  copy of the branch (`git archive | ssh tar`, no install), plans
  `rollback 20261029-d4ec5691`, and the deployed master's dry run does not.
- After the first live cycle: the ledger shows `20261029-d4ec5691` as
  `rolled_back` with reason `harm: server_tripped (early)` (or the first signal in
  `SIGNALS` order that fires), and an outbox notice exists.

## Left to the builder

- How the per-group baseline is passed into `_harm` (a helper for the group key,
  or a precomputed map), provided job changes still compute the same baseline.
- Test fixture shape, reusing Loop 1's fixtures where possible.
- Whether `_harm_in_trial`'s job-status lookup is restructured or extended.

## Replay script

Read-only. Run on hermes-vm with
`ssh hermes-vm 'cd ~/hermes-harness/tuner && .venv/bin/python -' < harm_noise.py`.

```python
# Read-only: how often would each harm signal fire on global-trial runs (chats + editable jobs)?
import time, datetime, collections
from pathlib import Path
from hermes_tuner import harm, policy
from hermes_tuner.history import History
from hermes_tuner.cycle import _global_run
policy.HARM_CHECKS = True
db = Path.home()/".hermes"/"state.db"
now = time.time(); D = 86400
h = History.open(db)
ss = [s for s in h.sessions(now - 56*D) if s.calls]
def group(s): return "chat" if s.source == "telegram" else policy.EDITABLE_JOBS.get(s.job_id, "other")
rates = {s.id: harm.error_rate(h.call_results(s.id)) for s in ss if _global_run(s)}
tot = collections.Counter(); hit = collections.Counter(); rows = []
for s in ss:
    if not _global_run(s) or s.started_at < now - 28*D: continue
    g = group(s)
    base = tuple(rates[x.id] for x in ss if _global_run(x) and group(x) == g
                 and s.started_at - 28*D <= x.started_at < s.started_at)
    ev = harm.RunEvidence(results=tuple(h.call_results(s.id)), fix_kind="config")
    b = harm.Baseline(error_rates=base)
    sig = [n for n in ("server_tripped", "self_inflicted_errors") if harm.CHECKS[n](ev, b)]
    tot[g] += 1
    for n in sig: hit[(g, n)] += 1
    if sig: rows.append((datetime.datetime.fromtimestamp(s.started_at).strftime("%m-%d %H:%M"), g, sig, len(ev.results)))
print("runs per group (28d):", dict(tot))
print("signal hits:", dict(hit))
for r in rows: print(*r)
```
