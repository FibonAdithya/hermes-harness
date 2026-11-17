# fleet chain route Implementation Plan (2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** fleet routes `build` and `bug` tasks to chain through a `ChainExecutor`, turns chain's outcomes into its own packets, writes a reason on every escalation, lets an owner answer raise a task's caps, and files found bugs straight to the chain route.

**Architecture:** A `CompositeExecutor` sends `StartAgent`/`StopAgent` for roles whose agent kind is `chain` to `ChainExecutor`, which shells out to the chain CLI and reads `chain status --json`. Ledger effects stay in `HerdrExecutor.apply`. When a chain run finishes, `ChainExecutor` writes the same packets a native agent would (`do_handoff` to review plus `do_verdict approved`, or `do_escalate`), so `decide()` moves the task through its existing states and the existing integrator merges.

**Tech Stack:** Python 3.11+ (uv), pytest, ruff, the YAML scenario harness in `src/fleet/simulate.py`.

**Spec:** `hermes-harness` `docs/ai/specs/2026-11-17-fleet-chain-coordination-design.md` (branch `docs/fleet-chain-coordination`, 513f758). Sections 1, 2 and 4 (fleet parts) are this plan. Owner decisions 6, 7 and 8 there approve the reserved changes this plan makes (`CLASSES`, `ROLE_MAY_HAND_TO`, `VERDICT_ROLES`, the handoff schema's role enum, the counter rule).

**Repo:** a fresh clone of public `FibonAdithya/fleet`, branch `feat/chain-route` from `main`. Do not work in `~/TIG/fleet`: its origin is a private mirror (an owner follow-up). MEASURED 2026-11-17: public main 2315427 and private main f03f1e0 have the same tree, 614b936.

**Depends on:** plan 1 (chain) Task 2 for `tests/fixtures/status/v1.json`. Tasks 1-5 here do not.

## Global Constraints

- `make check` (ruff check, ruff format --check, pytest) green after every task.
- Agent kind name: `chain`. Role name: `chain`. New classes: `build`, `bug`.
- Answers file: `<answers_dir>/<owner>__<name>/<number>.json`, a JSON list of `{"text": str, "at": float}`; default `answers_dir` is `~/.local/state/fleet/answers`. The box writes it (plan 3); fleet only reads it.
- Answer label: `fleet:answered-N`, N ≥ 1, the count of answers; written by the box, read here.
- The effective caps for a task are `max_attempts × (1 + answers)`, `max_fix_passes × (1 + answers)`, `max_rebases × (1 + answers)`.
- Escalation comments start with the exact line `<!-- fleet:escalation reason=<reason> -->`.
- The chain status schema id is exactly `chain.status.v1`.
- Nothing in this plan merges to `main` or changes the integrator role prompt.

## Review Focus

1. An answered task's third attempt needs the label `fleet:attempt-3`, which is outside `static_vocabulary`; real gh refuses unknown labels and changes nothing. (Task 2, `test_apply_ensures_runtime_labels` against `FakeGh`, which rejects unensured labels.)
2. `ChainExecutor.agents()` is called every tick; a finished run must produce its packets exactly once, including after a daemon restart. (Task 7, `test_outcome_written_once_across_restart`.)
3. A chain run paused on a usage limit for an hour must not be released as stalled under `stall_minutes = 10`. (Task 6, `test_paused_chain_run_is_not_stalled`.)
4. `chain status --json` failing (binary missing, non-zero exit, bad JSON) must not crash the tick or read as every chain agent vanishing at once. (Task 6, `test_status_failure_keeps_last_known_agents`.)
5. A `bug` route on a target without a `[chain]` default area must leave `agent-reported` issues alone, not adopt them with no area. (Task 9, `test_no_default_area_no_adoption`.)
6. The planner files children with the same `fleet discover` command scouts use for bugs; a planner's `patch` child must stay in triage, not become an unattended chain bug. (Task 9, `test_discover_patch_child_stays_in_triage`.)

---

### Task 1: Vocabulary: classes `build`/`bug`, and the answer count

**Files:**
- Modify: `src/fleet/labels.py:13` (CLASSES), add `answered_label`
- Modify: `src/fleet/model.py:11` (Task: add `answers: int = 0`)
- Modify: `src/fleet/ledger.py:100-102` (parse `fleet:answered-` with `_counter`)
- Test: `tests/test_ledger.py`, `tests/test_model.py`

**Interfaces:**
- Produces: `labels.CLASSES == ("patch", "spec", "investigation", "integration", "build", "bug")`; `labels.answered_label(n: int) -> str` (`"fleet:answered-N"`, `ValueError` for n < 1); `Task.answers: int` (highest N among `fleet:answered-N`, 0 when none).

- [ ] **Step 1: Write the failing tests**: `test_class_build_and_bug_parse` (`class:build` → `klass == "build"`); `test_answered_label_parses_to_count` (labels `fleet:answered-1`, `fleet:answered-2` → `answers == 2`); `test_no_answered_label_is_zero`; `test_answered_label_rejects_zero`.
- [ ] **Step 2: Run** `uv run pytest tests/test_ledger.py -q` — expect FAIL.
- [ ] **Step 3: Implement.** `answered-N` stays out of `static_vocabulary` (it is created at runtime, like `discovered-by:`).
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/labels.py src/fleet/model.py src/fleet/ledger.py tests/ && git commit -m "feat: build and bug classes; fleet:answered-N count"`

### Task 2: Answers raise the caps; counters stay monotonic

**Files:**
- Modify: `src/fleet/decide.py:183-190` (`_fail`), `:377-380` (rebase cap)
- Modify: `src/fleet/commands.py:129` (`_verdict_target`: take `answers`), `do_verdict` caller
- Modify: `src/fleet/expand.py:53` (`_attempt_label`: drop the clamp)
- Modify: `src/fleet/herdr_executor.py:142` (`apply`: ensure labels outside the static vocabulary before adding)
- Create: helper `effective_caps(policy: Policy, task: Task) -> tuple[int, int, int]` in `src/fleet/model.py` (attempts, fix passes, rebases)
- Test: `tests/test_decide_reap.py`, `tests/test_commands.py`, `tests/test_expand.py`, `tests/test_herdr_executor.py`

**Interfaces:**
- Consumes: `Task.answers` (Task 1).
- Produces: `effective_caps`; `_verdict_target(verdict: str, fix_passes: int, max_fix_passes: int, answers: int = 0)`. The attempt label is never clamped; `HerdrExecutor.apply` calls `gh.ensure_label(name)` once per process for any added label not in `static_vocabulary(...)` built from the policy.

- [ ] **Step 1: Write the failing tests**:
  - `test_answered_task_gets_a_third_attempt`: dirty agent, `attempts=2`, `answers=1`, `max_attempts=2` → `Release(task, "dirty_exit", 3)`, no Escalate.
  - `test_answered_task_escalates_after_its_new_allowance`: `attempts=4`, `answers=1` → `Escalate(..., "attempts_exhausted")`.
  - `test_unanswered_cap_unchanged`: the existing `test_a_second_failure_escalates_rather_than_making_a_third_attempt` still passes untouched.
  - `test_answers_raise_rebase_cap` and `test_answers_raise_fix_pass_cap` (`_verdict_target("changes_required", 1, 1, answers=1)` → `("ready", ["fleet:fix-pass-2"])`).
  - `test_release_attempt_label_not_clamped`: `expand(Release(t, "x", 3), policy with max_attempts=2)` contains `fleet:attempt-3`.
  - `test_apply_ensures_runtime_labels`: `FakeGh` (rejects unensured labels) + `HerdrExecutor.apply([SetLabels(t, add=("fleet:attempt-3",))])` succeeds and `fleet:attempt-3` is in `ensured_labels`.
  - `test_old_attempt_packet_does_not_advance_after_answer`: a task with `attempts=3`, `answers=1` whose latest packet has `attempt == 1` → `_advance` returns `[]`. This passes before Task 2 too; it is a regression guard against a later change that compares the packet's attempt to the cap instead of the current attempt.
- [ ] **Step 2: Run** those files — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet tests && git commit -m "feat: an owner answer raises a task's caps; attempt labels stay monotonic"`

### Task 3: Every escalation writes its reason

**Files:**
- Create: `src/fleet/escalation.py`
- Modify: `src/fleet/model.py:306` (Escalate: add `detail: str = ""`), `:386-419` (new effect `Comment(task: int, body: str)`, added to `Effect`)
- Modify: `src/fleet/expand.py:104-109` (Escalate → `StopAgent`, `Assign(None)`, `Comment`, then the state change)
- Modify: `src/fleet/herdr_executor.py:142` (`apply` handles `Comment` with `gh.comment`), `src/fleet/executor.py:99` (FakeExecutor records it)
- Test: `tests/test_escalation.py` (new), `tests/test_expand.py`

**Interfaces:**
- Produces: `escalation.MARKER_PREFIX = "<!-- fleet:escalation reason="`; `escalation.REASON_TEXT: dict[str, str]` with one plain sentence for each reason decide emits (`attempts_exhausted`, `packet_without_target`, `packet_target_unknown`, `rebases_exhausted`, `stage_produced_nothing`, `dirty_exit`, `stalled`, `model_mismatch`, `over_budget`, `unroutable`, `unroutable_stage`); `escalation.render(reason: str, detail: str = "") -> str`; `escalation.latest(comments: list[str]) -> dict | None` returning `{"index": int, "reason": str, "text": str}` for the newest comment that is either a marker comment or a rendered handoff packet with `to == "human"` (text = its `question`, then `considered`), using `packet.parse_latest`'s marker rules. Plan 3 calls `latest` from the box through `uv run --project ~/TIG/fleet`.
- The `Comment` effect is applied before the state label changes, so a reader that sees `fleet:human` also sees the reason.

- [ ] **Step 1: Write the failing tests**:
  - `test_render_starts_with_marker`.
  - `test_every_decide_reason_has_text`: every reason string literal passed to `Escalate(` in `src/fleet/decide.py`, read with `re.findall(r'Escalate\([^)]*?"([a-z_]+)"', text)`, is a key of `REASON_TEXT`, and the list found is non-empty (so a regex that matches nothing fails the test).
  - `test_latest_prefers_newest`: `[render("stalled"), handoff_to_human_packet]` → the packet's question; reversed → `reason == "stalled"`.
  - `test_latest_none_without_escalation`.
  - `test_expand_escalate_writes_comment_before_state`: effect order is `StopAgent, Assign, Comment, SetLabels`.
  - `test_escalate_detail_in_comment`: `Escalate(t, "over_budget", "spent 41.20 of 40.00")` → the comment contains both.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/escalation.py src/fleet/model.py src/fleet/expand.py src/fleet/herdr_executor.py src/fleet/executor.py tests && git commit -m "feat: every escalation writes a reason comment"`

### Task 4: Policy and config for the `chain` agent kind

**Files:**
- Modify: `src/fleet/config.py:104` (`_agents`), `:44` (FleetConfig: `answers_dir: Path`, from `[daemon] answers`, default `~/.local/state/fleet/answers`), new `[chain]` table: `default_area: str | None = None`, `max_age_days: int = 7`
- Modify: `src/fleet/model.py:193` (AgentSpec: `command: str | None = None`, `per_task_usd: float | None = None`), `:208` (Policy: `ceilings: tuple[tuple[str, float], ...] = ()`, method `ceiling_for(role: str | None) -> float`, and `chain_default_area: str | None = None`, `chain_max_age_days: int = 7`, because `decide()` sees only the Policy, not the config), `src/fleet/policy.py:248` (`load` copies them from the config)
- Modify: `src/fleet/policy.py:176-245` (`_validate_agents`)
- Modify: `src/fleet/decide.py:419`, `:472` (use `policy.ceiling_for(role)`)
- Test: `tests/test_config.py`, `tests/test_policy.py`, `tests/test_decide_reap.py`

**Interfaces:**
- Produces: `[agents.chain]` accepts `command` (required for kind `chain`, `~` expanded) and `per_task_usd` (optional, > 0); `model_flag` is required for every other kind as today. `_validate_agents` exempts kind `chain` from the transcript-reader and price checks; the role's Model cell for kind `chain` must be the literal `chain`. `Policy.ceiling_for(role)` returns the role's kind's `per_task_usd` when set, else `policy.per_task_usd`. `cfg.chain_default_area`, `cfg.chain_max_age_days`, `cfg.answers_dir`.

- [ ] **Step 1: Write the failing tests**: `test_chain_agent_needs_command`; `test_chain_agent_per_task_usd_must_be_positive`; `test_chain_kind_needs_no_price_row`; `test_chain_kind_model_cell_must_be_chain`; `test_claude_kind_still_needs_price_row` (unchanged strictness); `test_ceiling_for_chain_role` (40.0) and `test_ceiling_for_other_role_is_global` (2.0); `test_chain_agent_under_own_ceiling_not_over_budget` (cost 30, global 2, chain 40 → no Escalate/Release); `test_chain_agent_over_own_ceiling` (cost 41 → `over_budget`); `test_answers_dir_default`; `test_chain_table_defaults`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet tests && git commit -m "feat: chain agent kind, per-kind task ceiling, [chain] table"`

### Task 5: The chain role: hand-off rules, schema, role file, routes

**Files:**
- Modify: `src/fleet/commands.py:33` (`ROLE_MAY_HAND_TO["chain"] = frozenset({"review"})`), `:46` (`VERDICT_ROLES` adds `"chain"`)
- Modify: `.agents/schemas/handoff.schema.json` and `fixtures/policy/fleet-fixture/.agents/schemas/handoff.schema.json` (`from.role` enum adds `chain`)
- Create: `.agents/roles/chain.md` and the fixture's copy: one paragraph saying this role is run by chain through `ChainExecutor`, so no agent reads this prompt
- Modify: `docs/agent/task-classes.md` and `fixtures/policy/fleet-fixture/docs/agent/task-classes.md`: rows `build | chain → integrator | ...` and `bug | chain → integrator | ...`; Role table row `chain | chain | chain`; the root `fleet.toml` and the fixture's `fleet.toml` both get `[agents.chain] command = "chain"` and `per_task_usd = 40.0` (ESTIMATE (unverified) — the measured chain runs cost $3.31-$18.67); without the root one, `test_self_policy` fails Task 4's "chain needs command" rule
- Test: `tests/test_commands.py`, `tests/test_policy.py`, `tests/test_self_policy.py`, `tests/test_schemas.py`

**Interfaces:**
- Consumes: Task 4's kind rules.
- Produces: the policy both targets load has routes `build: (chain, integrator)` and `bug: (chain, integrator)`.

- [ ] **Step 1: Write the failing tests**: `test_chain_may_hand_to_review_only` (`do_handoff(..., role="chain", to="integrating")` raises; `to="review"` posts); `test_chain_may_write_verdict` (`do_verdict(..., role="chain", verdict="approved")` at state review moves to integrating); `test_schema_accepts_chain_role`; `test_fixture_policy_routes_build_and_bug`; `test_self_policy_loads` (existing) still passes.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/commands.py .agents fixtures/policy docs/agent tests && git commit -m "feat: the chain role: review hand-off, approved verdict, build/bug routes"`

### Task 6: `ChainExecutor`: start, stop, and agents from `chain status --json`

**Files:**
- Create: `src/fleet/chain_executor.py`
- Modify: `src/fleet/model.py:44` (AgentState: add `paused: bool = False`), `src/fleet/decide.py:154` (`_observe`: a paused agent refreshes `since`)
- Create: `tests/fixtures/chain/status-v1.json` (copied verbatim from chain's `tests/fixtures/status/v1.json`, plan 1 Task 2), `tests/test_chain_executor.py`

**Interfaces:**
- Consumes: Task 4 (`AgentSpec.command`), chain's CLI from plan 1: `chain start --fleet SLUG#N --kind {build,bug} --base BRANCH [--answers FILE] REPO [SPEC]` (prints the run id) and `chain abort RUN`.
- Produces: `ChainRun` (frozen dataclass mirroring one status object); `parse_status(text: str) -> list[ChainRun]` (`ValueError` unless `schema == "chain.status.v1"`); `class ChainExecutor` with `__init__(self, cfg: FleetConfig, gh: GhClient, command: str, run_cli: Callable[[list[str]], tuple[int, str, str]], state_path: Path)`, `apply(effects: list[Effect]) -> None` (handles only `StartAgent`/`StopAgent`), `agents() -> list[AgentState]`, `unpriced() -> set[str]` (always empty), `tasks() -> set[int]`.
- `StartAgent(task, role, base)`: read the issue; kind is `build` or `bug` from its class; for `build` the spec is the path on the issue body's first line matching `^Spec: (\S+)$` (none → record a failure in `self.failures` and start nothing; fleet's existing `no_agent` release then spends the attempts and escalates `attempts_exhausted`); pass `--answers` when the answers file exists. Record `task → run id` in `state_path` (JSON) so a restart keeps it. Unlike `HerdrExecutor._start`, do not assign the issue: plan 3 reads an assigned `fleet:human` issue as taken by the owner and stops announcing it.
- `agents()`: run `chain status --json`; keep runs whose `fleet_task.slug == cfg.repo_slug` and that `state_path` maps to a task. `status` queued/running/verifying → `working`, paused → `working` with `paused=True`, `waiting` (a fleet-mode run never asks; seeing it is a chain bug) → `dirty` with a recorded failure; `change_seq = progress`, `transcript_bytes = output_bytes`, `cost_usd = run_spent_usd`, `model = None`. Terminal runs are handled by Task 7. On a failed or unparsable status call, return the last good list and record the failure.

- [ ] **Step 1: Write the failing tests** (a fake `run_cli` that returns scripted output, and the copied fixture):
  - `test_parse_status_fixture` (seven runs; `test_parse_status_rejects_other_schema`).
  - `test_start_build_passes_spec_and_base` (argv exactly `[command, "start", "--fleet", "o/r#5", "--kind", "build", "--base", "integration", str(cfg.root), "docs/ai/specs/x.md"]`).
  - `test_start_bug_has_no_spec`; `test_start_passes_answers_when_file_exists`; `test_start_build_without_spec_line_starts_nothing`.
  - `test_stop_aborts_the_run` (argv `[command, "abort", run_id]`).
  - `test_running_run_maps_to_working`; `test_paused_maps_to_paused_working`.
  - `test_task_map_survives_restart` (new executor on the same `state_path` reports the same task).
  - `test_status_failure_keeps_last_known_agents` (exit 1, then bad JSON → same list as the last good call).
  - `test_paused_chain_run_is_not_stalled` in `tests/test_decide_reap.py`: an agent with `paused=True` and an unchanged `change_seq` for 60 minutes, `stall_minutes=10` → no Release.
  - `test_other_repos_runs_ignored`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/chain_executor.py src/fleet/model.py src/fleet/decide.py tests && git commit -m "feat: ChainExecutor starts, stops and reports chain runs"`

### Task 7: Chain outcomes become fleet packets

**Files:**
- Modify: `src/fleet/chain_executor.py`
- Test: `tests/test_chain_executor.py`

**Interfaces:**
- Consumes: `do_handoff`, `do_verdict`, `do_escalate` (`src/fleet/commands.py:84`, `:154`, `:115`), `escalation.render` (Task 3), Task 5's role rules.
- Produces: when `agents()` sees a mapped run in a terminal status for the first time (tracked by run id in `state_path`; each write step, e.g. `handoff` then `verdict`, is recorded as it succeeds, so a retry after a partial failure resumes at the failed step instead of posting a second handoff):
  - `approved`: `do_handoff(cfg, gh, task=t, to="review", role="chain", agent=f"chain-{run_id}", run=<now ISO>, base_sha, head_sha, branch, did=f"chain run {run_id}: plan, audit, implement, review", checks=["test command passed at the reviewed SHA"], commands=[test_command])`, then `do_verdict(cfg, gh, task=t, verdict="approved", head_sha=head_sha, reviewer=f"chain-{run_id}", role="chain", findings=[])`. Then see the reporting rule below.
  - `escalated`: `do_escalate(cfg, gh, task=t, question=escalation["text"], considered=f"chain run {run_id} ended at stage {stage} ({escalation['kind']})", ...)` with the run's branch and SHAs (`base_sha` for the head when `head_sha` is null; `git rev-parse origin/<integration_branch>` in `cfg.root` when both are null, for a run that failed during setup). Then see the reporting rule below.
  - `not_reproduced` / `duplicate`: `gh.comment(t, <verdict text: what was tried, or "Duplicate of #n">)`, move to `fleet:done` with `commands._move_state`, then `gh.close_issue(t)`. Then see the reporting rule below.
  - any other terminal status (aborted, ended with no outcome): report `dirty` (fleet's `_fail` retries within the caps).
  - The ledger the daemon read this tick was fetched before these writes (`Daemon.tick` reads the ledger, then calls `agents()`), so `decide()` still sees the task at `claimed`. A `done` agent there with no packet falls through to the stall check (`decide.py:436-450`), and a missing agent is released as `no_agent` (`:434`); either would relabel the task `ready` over the `integrating` or `human` label just written. So after writing an outcome, report the agent as `working` with `change_seq = progress + 1` (no stall, no release) on every call until `gh.get_issue(t)` shows the issue no longer carries `fleet:claimed`; only then drop it and forget the mapping. A dirty outcome writes nothing and is reported `dirty` at once.
  - For `escalated`, also `gh.assign(t, None)` (as `expand`'s Escalate does), so plan 3 does not read the issue as taken.

- [ ] **Step 1: Write the failing tests** (FakeGh):
  - `test_approved_run_hands_to_review_then_approves`: issue ends at `fleet:integrating` with one handoff and one verdict comment, both validating against the schemas.
  - `test_escalated_run_moves_to_human_with_question`: `fleet:human`, and `escalation.latest(comments)["text"]` starts with chain's question.
  - `test_not_reproduced_closes_with_comment` and `test_duplicate_closes_naming_the_duplicate`.
  - `test_aborted_run_reports_dirty`.
  - `test_escalation_before_setup_uses_integration_head` (both SHAs null → the packet's `base_sha` and `head_sha` are the stubbed `origin/integration` commit, and it validates).
  - `test_outcome_written_once_across_restart`: two `agents()` calls, and a new executor on the same `state_path`, → exactly one handoff comment.
  - `test_failed_write_is_retried_next_tick`: the verdict write raises once → next tick writes the verdict and the issue has exactly one handoff comment.
  - `test_no_release_race_after_outcome`: feed `decide()` the stale ledger (task at `claimed`, observation older than `stall_minutes`) together with the executor's agents right after it wrote an approved outcome → no `Release` and no `Escalate` for the task; on the next tick (ledger at `integrating`) the agent is gone and `decide()` emits `Continue(t, "integrator", ...)`. Mutation caught: reporting `done` or dropping the agent on the write tick.
  - `test_escalated_outcome_unassigns`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/chain_executor.py tests && git commit -m "feat: chain outcomes become handoff, verdict and escalation packets"`

### Task 8: `CompositeExecutor` and daemon wiring; a scenario end to end

**Files:**
- Create: `src/fleet/composite_executor.py`
- Modify: `src/fleet/cli.py:383-396` (build `ChainExecutor` when any routed role's kind is `chain`; wrap both in `CompositeExecutor`)
- Create: `fixtures/scenarios/chain-build-integrates.yaml`; modify `tests/test_scenarios.py:25` (the pinned file set) (no simulator change: the scenario uses the existing `relabel` tick key, `src/fleet/simulate.py:51`, to stand in for `ChainExecutor`'s writes)
- Test: `tests/test_composite_executor.py` (new), `tests/test_scenarios.py`

**Interfaces:**
- Produces: `CompositeExecutor(herdr: HerdrExecutor, chain: ChainExecutor | None, policy: Policy)`; `apply` sends `StartAgent` by `policy.agent_for(role)` kind, `StopAgent` to `chain` when `task in chain.tasks()` else to `herdr`, every other effect to `herdr.apply`; `agents()` and `unpriced()` are the unions.

- [ ] **Step 1: Write the failing tests**: `test_start_routes_by_kind`; `test_stop_routes_by_owner`; `test_ledger_effects_applied_once`; `test_no_chain_roles_no_chain_executor` (cli wiring with a tmp policy whose routes use only claude roles → `chain is None`). Scenario `chain-build-integrates` (policy: the fleet-fixture policy after Task 5): tick 1 expects `Dispatch(5, chain, 1)`; tick 2 `relabel: {5: integrating}` with the agent `healthy` expects no Release; tick 3 the agent `clean_exit` expects `Continue(5, integrator, ...)` (exact string as the harness prints it).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/composite_executor.py src/fleet/cli.py src/fleet/simulate.py fixtures/scenarios tests && git commit -m "feat: CompositeExecutor routes chain roles to ChainExecutor"`

### Task 9: Found bugs and agent-reported issues go straight to the chain route

`fleet discover` is used both by agents reporting a bug they found and by the planner filing an epic's children (`.agents/roles/planner.md` step 5), so the class has to be explicit.

**Files:**
- Modify: `src/fleet/commands.py:233-281` (`do_discover`: new `klass: str = "bug"`, `spec: str | None = None`), `src/fleet/cli.py:239` (`discover` gains `--class {patch,build,bug}` default `bug`, and `--spec PATH`)
- Modify: `.agents/roles/planner.md` and the fixture copy (step 5: pass `--class` per child; a `build` child passes `--spec` naming a spec committed on the integration branch)
- Modify: `src/fleet/model.py:11` (Task: `agent_reported: bool = False`, `by_owner: bool = False`, `created_at: datetime | None = None`), `src/fleet/ledger.py:80` and `src/fleet/reader.py:37` (`read_ledger(gh, areas, login: str | None = None)` sets `by_owner` when the issue's author is `login`; the daemon passes `HerdrExecutor.owner`'s login), `src/fleet/gh.py:118` (`list_issues` `--json` adds `author,createdAt`; FakeGh too)
- Modify: `src/fleet/decide.py` (new phase `_adopt` before `_unblock`; new Intent `Adopt(task: int)` in model.py; expand → `SetLabels(add=(ready, class:bug, auto-ok, area:<default>))`)
- Test: `tests/test_discover.py`, `tests/test_decide_adopt.py` (new), `tests/test_expand.py`

**Interfaces:**
- Consumes: `cfg.chain_default_area`, `cfg.chain_max_age_days` (Task 4).
- Produces: chain route enabled for a class ⇔ `policy.route_for(klass)` is non-empty and its first role's kind is `chain`. `do_discover` with `klass` `bug` or `build` on an enabled class files `[fleet:ready, class:<klass>, fleet:auto-ok, area:X, discovered-by:N]`; every other case (`patch`, or the class not routed to chain) files today's `[fleet:triage, area:X, discovered-by:N]` plus `class:<klass>`. `build` requires `spec`, which must exist on `origin/<integration_branch>` (`git cat-file -e`), and the body's first line is `Spec: <path>` (the line `ChainExecutor` reads, Task 6); `spec` is refused for other classes. `_adopt` emits `Adopt(t)` for an open task with `agent_reported`, no state label (reads as triage), `by_owner`, `created_at` within `policy.chain_max_age_days` of `now`, when the chain route is enabled and `policy.chain_default_area` is set.

- [ ] **Step 1: Write the failing tests**: `test_discover_with_chain_route_is_ready_bug_auto_ok`; `test_discover_without_chain_route_is_triage` (today's labels plus `class:bug`); `test_discover_build_child_has_spec_line_and_auto_ok`; `test_discover_build_needs_spec_on_integration` (missing file → refused, nothing filed); `test_discover_patch_child_stays_in_triage` (a planner's patch child is not auto-ok); `test_discover_spec_refused_for_bug`; `test_adopt_agent_reported`; `test_no_adopt_other_author`; `test_no_adopt_older_than_max_age`; `test_no_default_area_no_adoption`; `test_no_adopt_with_state_label`; `test_adopt_expands_to_ready_bug_labels`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet tests && git commit -m "feat: found bugs and agent-reported issues go to the chain route unattended"`

### Task 10: Native roles see the owner's answers

**Files:**
- Modify: `src/fleet/herdr_executor.py:63` (`build_prompt`)
- Test: `tests/test_herdr_executor.py`

**Interfaces:**
- Consumes: `cfg.answers_dir` (Task 4).
- Produces: when `<answers_dir>/<owner>__<name>/<task>.json` exists, `build_prompt` adds a part `# Owner answers (from Telegram)` listing each `text` in order, after the issue part. Malformed file → the part says the answers file could not be read (the prompt still builds).

- [ ] **Step 1: Write the failing tests**: `test_prompt_includes_answers`; `test_prompt_without_answers_unchanged` (byte-identical to today's output for the same inputs); `test_prompt_with_bad_answers_file_still_builds`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check` — expect PASS.
- [ ] **Step 5: Commit** `git add src/fleet/herdr_executor.py tests && git commit -m "feat: native roles see the owner's Telegram answers"`

### Task 11: Docs

**Files:** `README.md` ("fleet and chain" section), `docs/runbook.md` (a section "The chain route": fleet.toml `[agents.chain]` and `[chain]`, the routes, where answers live, how outcomes map), `AGENTS.md` (record that the owner approved the chain role in `ROLE_MAY_HAND_TO`/`VERDICT_ROLES` and the new classes on 2026-11-17, with the spec path).

- [ ] **Step 1: Write** the sections.
- [ ] **Step 2: Run** `make check` (`test_docs_references`, `test_no_home_paths`) — expect PASS.
- [ ] **Step 3: Commit** `git add README.md docs/runbook.md AGENTS.md && git commit -m "docs: the chain route"`

### Task 12: PR, review, merge (owner-attended)

- [ ] **Step 1:** Push `feat/chain-route` to `FibonAdithya/fleet`; open a PR with the claim table (test counts MEASURED before and after).
- [ ] **Step 2:** `review-pr` skill; `gh pr checks <n> --watch`, output pasted.
- [ ] **Step 3:** The owner merges. Deploy to the box is plan 3 Task 6.
