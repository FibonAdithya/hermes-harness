# fleet and chain as one system — Design

Date: 2026-11-17

Status: implemented and merged 2026-11-17 (chain PR #13, fleet PR #1,
hermes-harness PR #58). The rollout (plan 3, Task 6 Steps 3-8) has not
started yet.

Repos touched: `fleet`, `chain`, `hermes-harness` (box verbs, approvals
listener, box install). This document lives in hermes-harness because the
cross-repo design for sharing the workflow (`docs/ai/specs/2026-11-12-shared-workflow-design.md`
on branch `docs/shared-workflow`) set that precedent. Each repo's
implementation may get its own short spec that points here.

## Intent

fleet and chain both turn GitHub issues into merged changes, and today they
barely know about each other. The owner wants them to form one system: fleet
handles broad work (many fixes and builds, in parallel, overnight on the box),
and chain is a tool fleet uses for one narrowly scoped task at a time.

Escalations that need the owner must still reach him on Telegram, and he must
be able to answer from Telegram and have the work continue.

## Owner decisions (2026-11-17)

1. **chain is a fleet tool.** fleet orchestrates: backlog, slots, areas,
   planner, scouting, integration, `fleet:human`. chain runs one task end to
   end when fleet sends it one.
2. **Merging.** Work fleet sends to chain lands in the target's
   `integration` branch without asking the owner. The owner's yes moves to the
   promotion of `integration` to `main`, which fleet already leaves to a human
   (`.agents/roles/integrator.md`: "You never merge to `main`. Promotion is a
   human decision."). Refined after the builder check: chain does not merge.
   It stops at a clean review, and fleet's integrator merges, keeping the
   integration lock, the guard checks, `make check` and the revert.
3. **Routing by class.** Spec-sized work goes to chain. Small `patch` tasks
   stay on fleet's native implementer → reviewer → integrator route.
4. **The laptop chain stays, for now.** Runs the owner starts by hand with
   `chain start` keep the Telegram merge question.
5. **Approach 1** (below): a chain executor in fleet, backed by a chaind on
   the box.
6. **`fleet:human` issues ping the owner, and he answers in Telegram.** His
   reply is recorded on the issue, the issue goes back to `fleet:ready`, and
   fleet's next dispatch carries the answer. `drop` and `take` replies exist
   too. The answer resets the issue's attempt counters. That is a change to
   fleet's state machine, which fleet's `AGENTS.md` reserves for a human; the
   owner made it here.
7. **Reserved fleet invariants widened.** The chain role may hand to the
   integrator (`ROLE_MAY_HAND_TO`) and may issue the `approved` verdict
   (`VERDICT_ROLES`). New classes `build` and `bug` join `CLASSES`.
8. **Unattended dispatch.** Found bugs (`discovered-by:`), `agent-reported`
   tool bugs on fleet targets, and `build` issues fleet's planner files all
   carry `fleet:auto-ok` and run without an owner reply.

## What exists today (measured 2026-11-17)

| fact | how it was measured |
|---|---|
| fleet's source never mentions chain | `grep -rn chain ~/TIG/fleet/src` (excluding toolchain/itertools) prints nothing |
| chain intake skips issues with fleet state labels and stops a run if fleet labels its issue later; fleet does not skip `chain:taken` | chain README "Self-healing intake"; the grep above |
| live chain intake covers only `FibonAdithya/chain` | `~/.config/chain/config.toml` |
| chain reads `integration_branch` from the top level of `fleet.toml`; every fleet.toml puts it under `[repo]`, so chain gets `None` and targets the default branch | `src/chain/intake.py:475-488`; `tomllib` on `~/fairvalue`, `~/TIG/fleet`, `~/TIG/fleet-fixture` fleet.toml: top-level `None`, `[repo]` `integration` |
| fleet sends no messages; `fleet:human` is only visible through `night_status` or GitHub | `grep -rn -i "telegram\|notify" ~/TIG/fleet/src` prints nothing |
| fleet has an executor seam: `Executor.apply(effects)` and `Executor.agents()`; effects are `StartAgent(task, role, base)`, `StopAgent(task)`, `SetLabels`, `Assign`, `CloseIssue`; the only real executor is `HerdrExecutor` | `src/fleet/executor.py`, `src/fleet/model.py:404-420` |
| fleet counts attempts, fix passes and rebases with labels `fleet:attempt-N`, `fleet:fix-pass-N`, `fleet:rebase-N` | `src/fleet/labels.py` |
| discovered bugs are filed as `fleet:triage` + `area:<a>` + `discovered-by:<n>`, with no class label | `src/fleet/commands.py:274-281` |
| chain run spend, from every `~/.local/state/chain/runs/*/run.json`: toy acceptance tasks $3.31-$4.96, real tasks $5.52-$18.67 (computed by `claude -p`; on the Max plan this is usage, not money) | `run_spent_usd` read from each `run.json` |
| fleet's budgets: fleet `per_task_usd = 2.0`, `fleet_usd = 10.0`; fairvalue 25 / 100; fleet-fixture 2 / 25 | the three `fleet.toml` files |
| the box's `gh` is logged in as the owner's account, so every comment a box process writes is authored by the owner | `ssh tig-server 'gh api user --jq .login'` printed `FibonAdithya` |
| the approvals listener already announces `fleet:triage` issues with a 4-digit code and handles `task|solo|drop <code>` replies; PR #57 added an announcer that polls a read-only box verb | `broker/hermes_broker/approvals.py` (`TaskAnnouncer`, `handle_task`), PR #57 |
| two laptop chain runs wait on merge questions for PRs already merged another way | owner follow-ups |
| strict branch protection refuses a chain merge when another PR lands first; chain then asks a failure question instead of updating the branch | wiki `chain.md`, "Merge-refusal gotcha", hermes-harness PR #57 |
| the integrator is the only writer of `integration`, one at a time, enforced as a global lock; it rejects diffs touching `Makefile`, `pytest.ini`, `.github/workflows/`, `gates/`, `docs/agent/`, `.agents/` or with net test deletions, and reverts if `make check` fails after merge | `.agents/roles/integrator.md:3-16`, `src/fleet/decide.py:118-126` |
| a handoff packet advances a task only if its `attempt` equals the task's current attempt | `src/fleet/decide.py:200-212` |
| dispatch requires `fleet:auto-ok`; discovered bugs are filed without it | `src/fleet/decide.py:525-531`, `src/fleet/commands.py:280` |
| the allocator's own `Escalate` moves the issue to `fleet:human` and writes no comment | `src/fleet/expand.py:104-109` |
| `per_task_usd` is one value for every class | `src/fleet/model.py:223` |
| PRs into `integration` get no CI: fleet-fixture's `integration` has no `.github/workflows` (404; `main` has `ci.yml`); public `FibonAdithya/fleet` has no `integration` branch | `gh api 'repos/FibonAdithya/fleet-fixture/contents/.github/workflows?ref=integration'`, same with `ref=main`, same on `FibonAdithya/fleet` ("No commit found for the ref integration") |
| branch protection cannot be enabled on private fleet-fixture; hermes-harness master is strict | `gh api repos/FibonAdithya/fleet-fixture/branches/main/protection` → 403 "Upgrade to GitHub Pro"; hermes-harness `required_status_checks.strict` → `true` |

## Goals

1. fleet can route a task to chain and get back a reviewed PR (which fleet's
   integrator merges into `integration`), a failure, or a question, through
   its existing executor seam.
2. No issue is worked by fleet and chain at once. One system owns each repo's
   issue claims.
3. Every `fleet:human` issue, from chain routes and native routes alike,
   reaches the owner on Telegram once, and his answer from Telegram puts the
   task back in the queue with the answer attached.
4. Laptop chain keeps working as it does for hand-started runs, minus the two
   known collisions (stale merge questions, refused merges on strict
   protection).
5. Owner touchpoints per merged fleet-routed change: zero, plus one per
   promotion and one per escalation.

## Non-goals

- Moving laptop runs under fleet. That may come later ("for now").
- Making `FibonAdithya/chain` a fleet target.
- Replacing fleet's native `patch` route.
- Merging the Telegram bots. chain's bot stays on the laptop; box escalations
  go through the approvals bot.
- Rebuilding chain's stages as fleet roles (approach 3).

## Anti-goals

- **A box chain run merges anything.** Fleet-mode runs never merge; the
  integrator is still the only writer of `integration`, and nothing on the box
  merges to `main`.
- **A Telegram token on the box.** The security split keeps Telegram secrets
  on the Hermes VM. The droplet polls the box; the box never sends.
- **An agent can forge the owner's answer.** On the box every comment is
  authored by the owner's account (measured above), so "comments by the gh
  login" does not identify the owner there. The answer must reach chain
  through a path only the owner's Telegram reply writes.
- **Two claimants for one issue.** No path where fleet and laptop chain can
  both take the same issue.
- **Breaking the running setup.** Laptop chaind, the box's fleet nights, the
  approvals listener and the deploy gate keep working through the rollout.
- **More owner messages per merged change than today.**

## Approaches

1. **Chain executor in fleet, backed by a box chaind (chosen).** fleet gets a
   `ChainExecutor` behind its `Executor` seam. A chaind on the box runs
   fleet's chain tasks, with no Telegram and no intake. chain keeps its driver
   checks (reproduction exit codes, test-command allow-list, review-round
   counter, per-stage budgets). Cost: a second daemon on the box, and a status
   contract fleet and chain must both keep.
2. **chain run once inside a herdr pane.** `HerdrExecutor` starts
   `chain run-once <issue>` in a pane instead of `claude`; no chaind. It reuses
   fleet's pane, worktree and stall handling, but fleet's stall signal watches
   herdr agent state, and a driver process spawning `claude -p` children may
   look stalled or finished when it is neither. Not chosen: it would need that
   measured first, and it couples chain to herdr.
3. **chain's stages as fleet roles.** One codebase, but chain's driver logic
   lives in code, not prompts. Rewriting it loses what made the self-heal loop
   trustworthy. Not chosen.

## Design

### 1. Routing in fleet

Two new task classes. Each routes to a role whose agent kind is `chain`, then
to the existing `integrator`:

| class | what the issue holds | route | chain path |
|---|---|---|---|
| `build` | a line `Spec: <path>` naming a spec committed on the integration branch | chain → integrator | ordinary run: plan → audit → implement → pr → review/fix, stopping at a clean review |
| `bug` | a bug report (a discovered bug, or an `agent-reported` tool bug) | chain → integrator | issue run: confirm (reproduce, write spec) → plan → … → review/fix, stopping at a clean review |

The chain role may hand to `integrator` and may issue an `approved` verdict
(owner decision 7). Nothing else about the integrator changes: it holds the
global lock, runs the guard checks against the PR's diff, merges, runs
`make check` and reverts on failure.

`patch`, `spec`, `investigation` and `integration` keep their routes. fleet's
planner (`spec` class) may file its child issues as `build` or `patch`.

Adding classes changes the label vocabulary in `src/fleet/labels.py`
(`CLASSES`), which fleet's `AGENTS.md` reserves for a human. This design is
that decision.

`task-classes.md`'s `Role | Agent | Model` table gains a role (name is the
builder's; `chain` is the obvious one) whose Agent is `chain`. policy.py's
checks that every routed role has a prompt file, an `[agents.*]` table and a
price row need a rule for the `chain` agent kind, which has no transcript and
no model price of its own. Builder's call how; the checks must stay strict for
every other kind.

**Found bugs and planner builds run unattended (owner decision 8).** chain's
confirm stage is the filter for bugs (`not-reproduced`, `duplicate`). On a
target whose fleet.toml enables the chain route, fleet files a discovered bug
as `fleet:ready` + `class:bug` + `fleet:auto-ok`; on one that does not, as
`fleet:triage` (today's behaviour). An open `agent-reported` issue on a fleet
target with no fleet state label is relabelled the same way on first sight.
`build` issues the planner files carry `fleet:auto-ok` too. Bounds carried over from chain intake: opened by the gh login, no older
than `max_age_days` when first seen, a per-day cap. The approvals
`TaskAnnouncer` must not announce issues fleet has already taken this way
(they leave `fleet:triage`, so this should hold without a change; the builder
confirms it).

An `agent-reported` issue has no `area:` label, and fleet allocates by area.
The builder decides how such an issue gets one (a default area in fleet.toml,
or requiring the filing command to name one).

### 2. `ChainExecutor` (fleet)

A composite executor sends each `StartAgent` to `HerdrExecutor` or
`ChainExecutor` by the role's agent kind, and `agents()` returns both lists.

`ChainExecutor`:

- `StartAgent(task, role, base)` →
  `chain start --fleet <slug>#<task> --base <base> --kind build|bug [--answers <file>] <checkout> [<spec>]`,
  run as the box user. It records the returned run id against the task.
- `agents()` → `chain status --json`, mapped to `AgentState`:
  - `cost_usd` = the run's `run_spent_usd`;
  - status: running → `working`; reached a clean review → `done`; ended or
    aborted → `dirty`;
  - `change_seq` and `transcript_bytes` must move while a stage makes
    progress, so fleet's stall check works. A run paused on a usage limit
    (chain waits up to `usage_retry_s`, default 3600 s) must not be read as a
    stall under `stall_minutes = 10`. Builder's call how; the paused case gets
    a test.
- `StopAgent(task)` → `chain abort <run>`.
How outcomes reach `decide()` (my call). fleet advances a task only on a
handoff packet bound to the current attempt (`decide.py:200-212`), and
`AgentState` has no field for a question or a PR. So `ChainExecutor` writes
the packets a native agent would, through the same validated `fleet` CLI
write commands, attributed to the chain role and bound to the attempt:

- **Clean review:** a handoff to `integrator` plus an `approved` verdict bound
  to the PR's head SHA. The integrator then proceeds as for any approved work.
- **Ended on a question:** `fleet escalate` with chain's question as the
  reason, which moves the issue to `fleet:human` with that text as the
  comment.
- **`not_reproduced` or `duplicate`:** the issue is closed with a comment
  naming the verdict (and the duplicate's number), with no ping, matching the
  laptop's rule that status never goes to Telegram. Which closing label is the
  builder's.
- **Ended any other way (abort, crash):** `dirty`, which fleet's existing
  `_fail` path retries within the attempt cap.

`chain status --json` therefore carries the verdict for confirm outcomes, the
question text, the PR number and head SHA.

fleet's area rule (one live task per area) and `slots` apply to chain tasks
like any other.

### 3. chain fleet mode (chain)

Same code, new options on `chain start`, and a run field `source: fleet`.
Differences from a hand-started run:

- **Base, and no merge.** Branch from `--base` and open the PR against it.
  The run ends successfully at a clean review with the gate passing at the
  reviewed SHA and, for `bug`, the reproduction command exiting 0. chain never
  merges in fleet mode; the integrator does.
- **The gate (my call).** PRs into `integration` get no CI today (measured
  above), so in fleet mode the gate is the driver's own re-run of the test
  command (`verify_timeout_s`), as chain already does after `implement`.
  GitHub checks count as well when they exist. A PR with zero checks is not a
  `no_checks` question in fleet mode.
- **Questions end the run.** Any question other than the usage-limit pause
  (`needs_owner`, `failure`, `interrupted`, `over_run_budget`, a round limit)
  ends the run with status `ended` and an `escalation` `{kind, text}` in the
  run record. No Telegram, no waiting.
- **No ledger writes.** In fleet mode chain sets no `chain:*` labels and
  writes no issue comments. fleet owns the issue.
- **Input.** A `bug` run reads the issue title and body and the answers file
  (below). It ignores issue comments, because on the box they are all authored
  by the owner's account. A `build` run reads the spec named by `--spec` at
  `origin/<base>` and the answers file.
- **`chain status --json`.** One object per run: `id`, `source`, `fleet_task`,
  `status`, `stage`, `run_spent_usd`, `updated_at`, a progress counter,
  `paused_until`, `escalation`, `verdict`, `pr`, `head_sha`. Shape is the builder's; it
  gets a contract test on both sides (chain emits it, fleet parses a captured
  fixture of it).
- **Budgets (my call).** fleet has one `per_task_usd` for every class
  (`model.py:223`), below the cheapest measured chain run. fleet gains an
  optional per-role ceiling (for example `per_task_usd` in the role's
  `[agents.*]` table) used in place of the global one for that role; the
  global default is unchanged. The box's chain config sets stage budgets so a
  run's ceiling fits under it, and fleet's over-budget stop is the backstop.
  Values are the owner's to set in config; any defaults are ESTIMATE
  (unverified) until the acceptance run measures them.

### 4. Escalation and the owner's answer (hermes-harness)

**Box verbs.**

- `list_human` (read-only, ungated): for each `~/TIG/*/fleet.toml` target,
  the open `fleet:human` issues with slug, number, title, assignee, and the
  latest escalation comment, cut to the same length rules
  `TaskAnnouncer` uses (`is_drop_only`, 3500 chars).
- `answer_human {repo, number, text}` (called only by the approvals
  listener): appends `{text, at}` to
  `~/.local/state/fleet/answers/<owner>__<name>/<number>.json`, posts a comment
  marked as the owner's Telegram answer for the record, adds
  `fleet:answered-N` (N = answers so far), removes `fleet:human`, and adds
  `fleet:ready`.

**How the answer resets the counters (my call).** Stripping the attempt labels
would make attempt numbers repeat, and an old handoff packet from attempt 1
would then read as current (`decide.py:200-212`). So attempts keep counting
up and `fleet:attempt-N` labels stay. Each answer raises the caps instead: the
attempt cap becomes `max_attempts × (1 + answers)`, and fix-pass and rebase
caps get the same allowance per answer. `fleet:answered-N` is new vocabulary,
covered by owner decision 6.

**Native escalations get a reason (my call).** The allocator's own
escalations (stalled, attempts exhausted, over budget, dirty exit, unroutable,
model mismatch, stage produced nothing) write no comment today
(`expand.py:104-109`). `Escalate` gains a comment with the reason, so every
`fleet:human` issue has text for the ping and each escalation is a distinct
comment the announcer can key on.
- `drop` reuses `close_task`.
- `take {repo, number}`: assigns the owner; the issue keeps `fleet:human`; the
  announcer stops announcing it (my call).

**Where answers reach the agent.** `ChainExecutor` passes the answers file to
`chain start --answers`. For native fleet roles, `build_prompt`
(`src/fleet/herdr_executor.py`) includes the same file's contents. Agents never
read the comment as the answer.

Known limit: fleet agents run as the same Unix user as the verbs, so an agent
that ignores its permissions could write the answers file. That is the same
trust boundary as every other box state file today; this design does not
widen it.

**Approvals listener.** A `HumanAnnouncer`, built like `TaskAnnouncer`, calls
`list_human` on a timer and sends one message per (issue, escalation) with a
4-digit code from the shared code pool:

    fleet:human  FibonAdithya/fleet-fixture#41  <title>
    <escalation text>
    code 5821
    answer 5821 <your text> | drop 5821 | take 5821

The parser accepts `answer <code> <text>` with multi-line text up to a cap
(builder's value), under the existing rules (owner id, private chat, not
forwarded). A new escalation on the same issue gets a new message and code.
The listener answers in chat with what the verb did.

### 5. Laptop chain changes (chain)

1. Intake skips any repo whose checkout has a `fleet.toml`, and logs it once.
   fleet owns those repos' issues. (No change today: only `FibonAdithya/chain`
   is configured.)
2. Read `integration_branch` from `[repo]` in fleet.toml, defaulting to
   `integration` like fleet does (`src/fleet/config.py:160`). This is a bug
   today (measured above). Fleet mode needs the right value; with item 1 the
   laptop no longer uses it.
3. A run whose PR is merged or closed outside chain ends itself, wherever it is
   waiting (merge question included), with status `ended` and the reason.
4. A merge refused because the branch is behind its base: `gh pr update-branch`,
   wait for checks on the new head, run a fresh review, then ask again (laptop)
   (laptop only; fleet-mode runs do not merge). This replaces the failure
   question described in wiki `chain.md` "Merge-refusal gotcha". The refusal
   strings in `src/chain/engine.py::REVIEW_AGAIN_TEXTS` are ESTIMATE
   (unverified); the builder captures a real refusal before relying on them,
   on a repo with strict protection (hermes-harness master is strict; private
   fleet-fixture cannot have protection, measured above).

### 6. Box install and deploy (hermes-harness)

- chain checkout on the box at `~/chain` for the box user, `make setup`, and a
  user unit for chaind with no `telegram.env` and a config that has no
  `[intake]`. chaind is always on; fleet nights start and stop around it, and
  a chain run can outlive the night that started it (fleet's next night picks
  it up through `agents()`, as it does for orphans today).
- The box user needs systemd linger so chaind survives logout. The builder
  checks `loginctl show-user <user> -p Linger` first.
- `deploy` / `deployed` gain a chain stamp, and the approvals bot accepts
  `deploy chain <prefix>`, following the fleet pattern and PR #57's deploy ask.
- `box/install.sh` installs chain.

## Constraints

- chain's stage sessions on the box use auto mode plus per-stage
  `--allowedTools`, as on the laptop. Whether auto mode works headless with
  the box's `claude` login has not been measured; measure it first.
- If the box's `claude` login and the laptop's are the same Max account,
  laptop runs and box runs share one usage limit. Not measured; the builder
  checks which accounts they are and records it.
- master's required checks in hermes-harness are `broker` and `box`. The new
  tests for verbs and the announcer belong in those suites.
- Box tests run under the executor image's pytest 7.2.1 (see issue #49 on
  helpers named `setup`).
- Public `FibonAdithya/fleet` has no `integration` branch (measured above),
  and the box's fleet checkout still has the pre-switchover history (an owner
  follow-up). Both are fixed before fleet itself is a target of this flow.
  fleet-fixture is the first target.

## Acceptance (with the owner, on `fleet-fixture`)

1. A `class:build` issue naming a committed spec runs through chain on the box,
   hands to the integrator, and is merged into `integration` with no message
   to the owner.
2. A discovered bug becomes `fleet:ready` + `class:bug` + `fleet:auto-ok`,
   chain reproduces and fixes it, and the integrator merges it.
3. A chain PR that touches `.github/workflows/` is rejected by the integrator's
   guard and escalated, and the owner is pinged.
4. A task forced to escalate reaches `fleet:human`; the owner gets one
   Telegram message with the question; `answer <code> <text>` puts the issue
   back in `fleet:ready` with `fleet:answered-1` and room for new attempts;
   the re-dispatched run receives the text and its work is merged.
5. `drop` closes an issue; `take` assigns it and stops further announcements.
6. A bug chain cannot reproduce is closed with a comment and no ping.
7. Laptop: intake skips a repo with a fleet.toml; a run whose PR is merged by
   hand ends itself; a refused-behind merge updates the branch and asks again.

Record, MEASURED: chain spend per task on the box, owner messages per merged
change, time from escalation to ping.

## Calls made by the designer, not the owner

Listed so the owner can check them:

- Found bugs, `agent-reported` issues and planner builds skip triage only on
  targets that enable the chain route.
- chain writes no labels or comments in fleet mode; `ChainExecutor` writes
  fleet packets on its behalf.
- Bugs that are not reproduced or are duplicates close without a ping.
- The fleet-mode gate is chain's own test re-run; zero GitHub checks is not a
  question.
- Answers raise caps instead of stripping counters (`fleet:answered-N`).
- Native escalations gain a reason comment.
- A per-role `per_task_usd` for the chain role.
- `take` assigns the owner and stops announcements.
- Laptop intake skips repos that have a `fleet.toml`.

## Left to the builder

- Names: the chain role, `--fleet` / `--kind` / `--answers` flags, verb names
  beyond those given, the JSON shape of `chain status --json`.
- How policy.py validates the `chain` agent kind.
- How `change_seq` and `transcript_bytes` are derived for a chain run, and how
  a usage-limit pause is kept from reading as a stall.
- How an `agent-reported` issue gets an area.
- The answer text cap and the announcer's poll interval.
- Splitting this into per-repo plans, and their order. The status contract
  (section 3) comes first, since fleet and chain both depend on it.
