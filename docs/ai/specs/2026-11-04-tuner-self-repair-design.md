# Tuner self-repair: undo bad changes, patch the checks, merge stricter fixes unattended — Design

Date: 2026-11-04. Status: direction agreed with the owner on 2026-11-04; full
document awaiting owner approval.

Extends `docs/ai/specs/2026-10-19-hermes-tuner-design.md` and
`docs/ai/specs/2026-10-28-tuner-carried-bytes-design.md` (the tuner, its
shadow, trial and rollback) and reuses the executor path from
`docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md` (`run_task` on the
box, PR only). Everything there still holds unless a section below says
otherwise. Hermes Agent is not modified.

## Purpose

When the tuner makes a mistake, the owner wants the harness to find it, undo
it, and fix the cause in its own code, with as few owner approvals as
possible. The owner does not want to be, or to have Claude Code be, the one
who spots the mistake and runs the undo.

Two loops:

1. **Undo.** The tuner detects that a change it made is causing harm and rolls
   it back at its next nightly cycle, without waiting for the cost verdict.
2. **Repair.** When a mistake got through because a check was missing or too
   loose, the harness files a repair task, the executor writes the missing
   check with a test, and the PR merges and deploys without the owner if it
   only makes the tuner stricter.

**Success:** the 2026-11-03 briefing incident (below) is handled end to end
with zero owner approvals: the change is rolled back at the first cycle
after loop 1 ships, a repair request is filed from that rollback, the
executor opens a PR adding the missing check ("a job prompt may only name
tools that are live", P1 in §2.2), the gate merges it, harness-pull puts it
live on the droplet, and the owner receives one-line notices of each step.
P1 is deliberately not written by hand: it is the first real repair.

Approvals needed to roll this out (once, not per incident): approving this
document, merging the build PRs, and one `deploy <sha>` for the box changes
in §3.3 and §4.3.

## The incidents behind this (MEASURED 2026-11-04 unless stated)

**A. A bad prompt went live.** Fix `20261103-23272612` (job_prompt, morning
briefing `3bc4e11d29b1`) passed its shadow and went live at the 2026-11-04
03:30 cycle. Measured from `~/.hermes/tuner/versions/20261103-23272612/` and
the 08:00 run `cron_3bc4e11d29b1_20261104_080057` in `~/.hermes/logs/agent.log`:

- The new prompt tells the model to fetch mail with
  `tuned_batch_fetch_gmail_contents`. That composed tool was fix
  `20261024-949b9ef4`, rolled back on 10-25 ("trial: not cheaper than
  baseline"); `~/.hermes/tuner/tools/` is empty. The model looked it up
  (`tool_describe` → "is not a deferrable tool"), then made three malformed
  `get_gmail_message_content` calls ("expected_sha256 and page_size require
  page_offset"), after which Hermes marked the Google MCP server unreachable.
  The model waited 65 s in a terminal call and finished.
- The new prompt (999 chars, was 1769) replaced the owner's explicit
  delete/archive/file/keep lists with "classify each message with the
  existing triage rules". Those rules existed only in the old prompt.
- Priced with the tuner's own `breakdown`/`priced_breakdown`, the run cost
  $0.1547 against a baseline median of $0.1600 (17 runs, range
  $0.0650–$0.3627). The trial needs `JOB_TRIAL_RUNS = 5` runs before judging
  cost, so this change would stay live until at least 2026-11-08.

Why nothing caught it (read from code at `origin/master` 7f55593):

- `limits.check_fix` does not check which tools a job prompt names.
- `trial.tool_check` (shadow) fails a shadow that *successfully* used a
  composed tool that is not live. A lookup that errored is not counted.
- `cycle.judge_trials` early rollback (`_early_failures`) only counts
  failures of a composed tool's or reducer's own calls. A job_prompt fix has
  no early harm check at all; it waits for the cost verdict.

**B. The tuner was stuck for four days.** From 2026-10-30 to 2026-11-02
every proposal (2 reducers, 3 composed tools, 1 job prompt) was rejected
by the copy check, because allowlisted tool names were in the copy index.
Nothing noticed; the owner found it. Fixed by PR #36 (7f55593). (MEASURED
2026-11-02, see PR #36.)

## Decisions made by the owner on 2026-11-04

- The harness and Hermes fix themselves. The owner and Claude Code are not
  the detection or undo step.
- Minimise owner approvals.
- **Stricter goes in without the owner; looser waits for the owner.** A code
  fix that can only make the tuner reject, fail or roll back more is merged
  and deployed unattended. Any other fix needs one owner approval: the merge.

## Calls made in this document (not owner decisions; check these)

- Rolling back is unattended. An undo returns to a state the owner already
  had, so it needs no approval.
- Repair tasks are filed and started without an owner approval, because
  their brief is built only from fixed text and validated structured fields
  (§3.2). No email-derived text reaches the executor.
- **Job prompt edits become append-only (§2.2, P2).** The tuner may only add
  or replace a block it owns, delimited by markers, and the owner's text must
  stay byte-identical. This removes incident A's second failure (dropped
  rules) as a class. Cost: the tuner can no longer shorten the owner's text.
  The 11-03 rewrite saved ~770 chars of a prompt in runs of ~590k tokens, so
  ESTIMATE (unverified) the lost saving is under 0.1% per run.
- Self-repair covers mistakes the tuner makes. Failures Hermes makes on its
  own, not caused by a tuner change (an MCP outage, a provider out of
  credits), are out of scope for this version (§Non-goals).
- A review agent is part of the merge gate, and it can only block (§4.3).
- The droplet learns PR state through new read/mark verbs on the box, which
  already has a `gh` login (§3.4), rather than a new GitHub token on the
  droplet. This costs one box deploy approval at rollout.
- Self-repair executor tasks run without the `/wiki` mount and without
  `context_pages`, because the wiki export may hold text Hermes wrote after
  reading email (§3.3).
- Harm checks apply only to job fixes (`JOB_KINDS`), not to config or skill
  fixes, whose runs include every owner chat (§2.1).
- Quality-compare failures (the shadow decided differently) do not file
  repair requests: the request cannot carry the decisions, so the executor
  could only write a blunt rejection (§3.1).
- New checks get their inputs from one snapshot built up front, so a repair
  rarely needs to touch the wiring code outside the allowlist (§2.4). A
  repair that does is not eligible and waits for the owner.

## 1. Current state (surveyed 2026-11-04)

- Droplet at 7f55593; `harness-pull.timer` pulls and deploys `origin/master`
  every 15 minutes (journal shows runs at :02, :17, :32, :47). Merging to
  master is deploying, for everything that runs on the droplet (tuner,
  broker, plugin). The plugin is loaded from the checkout (symlink), so a
  plugin change also needs a gateway restart, which the tuner's own restart
  step does.
- Box `tig-server` reachable, harness at 8c792cb. `run_task` → executor
  (`executor/run-task.sh`) runs Claude Code in a throwaway container and
  opens a PR from `hermes/task-*`; its system prompt forbids merging. Last
  executor PR merged: #17, 2026-09-28. `run_task` on the broker requires a
  grant (owner `approve <code>`). `fleet-doctor.timer` is not enabled.
- GitHub `FibonAdithya/hermes-harness`: public; `allow_auto_merge: false`;
  master protection requires checks `broker` and `box` only (not `tuner`),
  0 approving reviews, `enforce_admins: true`, `strict: true`.
- The approvals listener (`broker/hermes_broker/approvals.py`) already reads
  the tuner's folder: it announces proposals and sends every
  `~/.hermes/tuner/outbox/*.txt` as one Telegram line, renaming it `.sent`.
- `backoff.reason_class` maps ledger reasons to classes (`copy`, `limits`,
  `replay`, `shadow`, `trial`, ...). The drafting model sees only classes and
  field names, never reason text (`draft_history`).

## 2. Loop 1: undo

### 2.1 Harm checks on runs

A new module (`tuner/hermes_tuner/harm.py`) computes harm signals for one
run. Each signal has a fixed name. They are deterministic; no LLM decides.

Data sources, all on the droplet and readable by the tuner today:
state.db (tool calls and results via `History`) and the job's last run
status through the bridge (`get_job`: `last_status`, `last_run_at`,
`last_delivery_error`). The "not a deferrable tool", "Error calling tool"
and "MCP server ... is unreachable" errors are all stored as tool results
in state.db (MEASURED 2026-11-04 on `cron_3bc4e11d29b1_20261104_080057`),
so `agent.log` is not needed.

| signal | fires when |
|---|---|
| `unknown_tool` | the run looks up or calls a tool that is not registered (Hermes's error for an unknown or non-deferrable tool, from `tool_describe` or `tool_call` results) |
| `self_inflicted_errors` | tool calls in the run fail with argument or validation errors at a rate above the job's baseline (threshold left to the builder) |
| `server_tripped` | Hermes marks an MCP server unreachable during the run **and** the run made failing calls to that server just before (so the run caused it) |
| `run_failed` | the run ended in an error or its delivery failed (job status) |

A run with no tool calls is not harm: `_trial_runs` already treats those as
outages and skips them, and that stays.

Attribution: a signal counts against a fix only if it plausibly comes from
the fix. A server outage that also shows in runs not under trial, in the
same window, is an outage, not harm: no rollback, and it does not count
toward back-off. `unknown_tool`, `server_tripped` and `run_failed` count
from a single run. `self_inflicted_errors` compares against the same job's
runs in the 28 days before the fix, re-read from state.db (the stored
`baseline_runs` hold token counts only), the way `_count_early_failures`
already re-reads runs for re-reads.

Amended 2026-11-04 after replaying the signals on 28 days of state.db (plan
audit): `unknown_tool` counts only a name the fix's own payload mentions and
that is not registered, because every briefing run from 10-30 to 11-03 called
a rolled-back composed tool still listed until the gateway restart, with no
fix in trial; and Hermes returns "not a deferrable tool" for registered core
tools too. Google API failures (`API error in ...`) are outages and count
toward neither `self_inflicted_errors` nor `server_tripped`.

Where the checks run, for job fixes only (`JOB_KINDS`: job_prompt,
composed_tool, reducer). Config and skill fixes keep their current judging,
because their runs include every owner chat and one bad chat would roll
back a global fix.

- **Shadow** (`evaluate_shadows`): a shadow that fires any signal fails,
  in addition to the existing `tool_check` and quality compare.
- **Trial** (`judge_trials`): after each live run of a job fix in `trial`,
  any signal rolls it back at the next cycle (today only composed tools and
  reducers have early checks). This also applies to records already in
  trial when this ships, so `20261103-23272612` is rolled back at the first
  cycle after deploy.
- `harm.py` holds the signals in one registry that the shadow and trial
  code loop over, so a repair that adds a signal touches only `harm.py`.
- Rollback reasons start with `harm: <signal>` so `reason_class` can map
  them to a class per signal (`harm_unknown_tool`, ...). These classes are
  fixed strings and safe to show the drafting model, which therefore learns
  "the last edit for this job named a tool that did not exist" without
  seeing any text.

### 2.2 Checks before apply

- **P2, owner text preserved (built by hand in this build).** A job_prompt
  edit may only add or replace one tuner-owned block, delimited by fixed
  markers; everything outside the block must equal the current prompt byte
  for byte. The drafting instructions change to match: the model writes the
  block, not the prompt. A composed tool's `job_prompt` follows the same
  rule. This is a structural rule the repair loop could not reach from
  structured evidence, so it is not left to the loop.
- **P1, live tools only (left for the repair loop to write).** Expected
  shape, for the acceptance check: inside the tuner-owned block, every token
  matching `policy.TOOL_NAME_RE` or `mcp__<server>__<tool>` must be a live
  composed tool, the fix's own tool, or a registered Hermes tool from the
  snapshot (§2.4). Owner text outside the block is not checked: P2 keeps it
  unchanged, and checking it would reject every edit to a job whose owner
  text names a tool. Until P1 lands, the shadow's `unknown_tool` signal
  catches the same mistake one step later.

### 2.3 Notices

Each rollback writes one outbox line, built only from the fix id, job name
and signal name, for example
`tuner rolled back 20261103-23272612 (morning-briefing): harm_unknown_tool`.
The existing listener sends it.

### 2.4 One input snapshot for every check

Repairs may only touch `limits.py`, `harm.py` and tests (§4.1), so the
data a future check might need is gathered up front by the wiring code
built here:

- `LimitContext` gains `live_tools` (composed tools in applied, trial and
  kept states, as `cycle._live_tools` computes), `registered_tools` (names
  Hermes has registered, from a new bridge op; the `Bridge` protocol has no
  tool listing today), and the target job's current prompt and policy name.
- Harm signals receive the run's calls and results, its job status, the
  fix kind, and the same tool sets.

A repair that needs anything else touches wiring code, is not eligible
(§4.1), and waits for the owner.

## 3. Loop 2: repair requests

### 3.1 What files a request

The tuner (host program, deterministic) writes a repair request when:

- **R1, harm got through.** A fix was rolled back or failed its shadow on a
  harm signal (§2.1). The question for the executor: which check, added
  before apply, would have rejected this fix? Quality-compare failures and
  cost rollbacks do not file R1. The detector also scans records closed on a
  harm signal in the last 14 days, so a rollback that happened before loop 2
  shipped (incident A) still files its request.
- **R2, stuck.** On 3 consecutive cycles every drafted proposal was rejected,
  and the same reason class rejected most of them. This is incident B. A fix
  here usually loosens a check, so its PR is never auto-merged (§4).

Rate limits: at most one request per (trigger, signal or class, fix kind)
per 14 days; at most one repair task started per day; at most one open
self-repair PR at a time. A request that hits a limit is kept and counted
in the weekly line, not dropped silently.

### 3.2 What a request contains

Only fields that cannot carry email text, each validated against a schema
(enum, id pattern, integer range) by the listener before use:

- trigger (`R1`/`R2`), fix id, fix kind, target job id and its policy name
  (`triage`/`calendar`), signal or reason class, payload field names
  (`payload.prompt`), counts (runs, errors, calls).
- Tool names only when they are in the policy allowlists, live, registered,
  or fully match `policy.TOOL_NAME_RE`, each with a live/not-live flag.
- Error *classes* mapped from fixed patterns (for example
  `unknown_tool`, `arg_validation`, `server_unreachable`), never raw error
  text.

Not included: the prompt text, tool results, payload values, mail, ledger
reason strings. The executor writes its regression test from the structure
("a job_prompt whose block names `tuned_x`, which is not live") with
synthetic data. The repo is public, so this also keeps owner data out of
tests.

### 3.3 Dispatch

The tuner writes `~/.hermes/tuner/repairs/<id>.json`. The approvals
listener, which already reads the tuner folder and can reach the box,
validates it, renders a fixed brief template with the fields, and calls the
box `run_task` verb for repo `FibonAdithya/hermes-harness` directly, in a
new self-repair mode: no `/wiki` mount and no `context_pages`. The wiki
export may contain text Hermes wrote after reading email, and the claim
that no untrusted text reaches the executor must hold for everything it can
read, not just the brief. The mode is a change to `run-task.sh` and the
`run_task` verb on the box. This new path skips
the grant on purpose (Decisions); it is not an MCP tool, so the Hermes agent
cannot call it. The agent's terminal and file tools run in Docker sandboxes
that mount only their own `home` and `workspace` (rw) and `skills` (ro); the
tuner folder is not mounted (MEASURED 2026-11-04, `docker inspect` of the
three running `hermes-sandbox` containers), so the agent cannot write a
request.

The brief tells the executor to:

- reproduce the case as a failing test using synthetic data,
- add or tighten the check (in the allowlisted files, §4.1) so the test
  passes,
- or, for R2, find the check rejecting valid proposals and fix it,
- run the tuner suite, open a PR titled `self-repair: <class>`, labelled
  `self-repair`, with the request id in the body.

A task that ends without a PR writes a notice and closes the request.

### 3.4 How the droplet sees GitHub

The broker has no GitHub token or API calls, and the merger runs in
Actions where it cannot write the tuner's outbox. The box has the owner's
`gh` login, so three new box verbs carry everything the listener needs:

- `pr_status`: open and recently merged PRs labelled `self-repair`, with
  head SHA, head repository, mergeable state, behind-master flag, and the
  state of each required check and status.
- `pr_mark`: sets the commit status `self-repair-request` to success on a
  PR's head SHA. The listener calls it only after confirming the PR's head
  repository is this repository (no forks), its branch is the `hermes/task-*`
  branch of a task it dispatched, and that task answers a pending R1
  request. The merger requires this status, so the merger never needs to
  see the droplet's request files.
- `pr_update`: `gh pr update-branch` with the owner's token, for a PR that
  is behind master. Because the update is not made with `GITHUB_TOKEN`, CI
  runs on the new head SHA as usual; `self-repair-request` and the review
  status are bound to SHAs, so the listener marks and reviews the new head
  again.

The listener polls `pr_status` on its existing tick and writes the merge,
"needs you" and "no PR" notices to the outbox. "One open self-repair PR at a
time" (§3.1) is checked against `pr_status`.

## 4. Unattended merge: the stricter-only gate

A PR is merged without the owner only if every check below passes. Anything
else stays open, and the owner gets one notice
(`self-repair PR #n needs you: <first failed check>`), which is the single
approval.

### 4.1 Eligibility (diff only, no PR code run)

- Head repository is this repository (not a fork), branch `hermes/task-*`,
  label `self-repair`, and commit status `self-repair-request` is success on
  the head SHA (set by the listener, §3.4). R2 PRs are never marked, so never
  eligible.
- Changes only these paths: `tuner/hermes_tuner/limits.py`,
  `tuner/hermes_tuner/harm.py`, and files under `tuner/tests/`. Nothing in
  `.github/`, `broker/`, `box/`, `executor/`, the plugin, the copy check
  (`fingerprint.py`), or any other tuner module.
- In existing test files, only added lines: no deleted or changed lines, and
  no new `skip`/`xfail`.
- Diff size under a cap (builder's choice, around 300 lines).

### 4.2 Stricter-only, proved by running both versions

Run in an unprivileged CI job (read-only token, no secrets):

- The full tuner suite passes on the PR.
- Every new test fails on the base commit's code. This shows each test
  encodes behaviour the base did not have.
- **Monotonic replay.** While the full suite runs on the PR code, every call
  to the guarded decision functions (`check_fix`, the shadow and trial harm
  verdicts) is also evaluated with the base commit's version on the same
  input. The job fails if any input that the base rejected (non-empty
  reasons, a failing verdict, a rollback) the PR accepts. This is what makes
  "stricter only" a checked property and not a claim. How to run two
  versions side by side is left to the builder.
- **Replay inputs beyond the suite.** The replay also runs over a fixed
  synthetic corpus committed under `tuner/tests/` (fixes of every kind and
  field shape, plus every input the base suite passes to the guarded
  functions, recorded once and stored). No ledger data: it is email-derived
  and CI is public.
- **Changed lines must be exercised.** Every line the PR adds or changes in
  `limits.py` and `harm.py` must run during the suite or the corpus replay
  (diff coverage 100%). A loosening on a path nothing reaches therefore
  fails here instead of passing the replay unseen.

### 4.3 Review agent

After 4.1 and 4.2 pass (seen through `pr_status`), the listener starts a
second executor task in a fresh container that reads the PR and answers one
question: does this change only add rejections, and nothing unrelated? It
posts a commit status `self-repair-review` (pass/fail) on the head SHA. It
can only block. Its brief contains the PR number and the fixed question,
nothing else.

This needs a review mode in `run-task.sh`: today its fixed system prompt
tells Claude to commit, push and open a PR. Review mode replaces that with
"read the PR, post the status, push nothing", and has no `/wiki` mount. It
ships with the other box changes in one `deploy <sha>`.

### 4.4 Merger and privilege split

- The merge is done by a workflow on the default branch (triggered when the
  required statuses complete), using `GITHUB_TOKEN` with `contents: write`
  and `pull-requests: write`. It never checks out or runs PR code. It
  re-checks 4.1 from the diff itself, confirms 4.2 and 4.3 passed on the
  PR's head SHA and that `self-repair-request` is success there, then
  squash-merges that exact SHA. A PR that is behind master is left for the
  listener's `pr_update` (§3.4); the merger does not update branches, since
  a `GITHUB_TOKEN` push would not trigger CI on the new head.
- `tuner` becomes a required check on master, alongside `broker` and `box`.
- Workflow files are added over SSH: the HTTPS `gh` token lacks the
  `workflow` scope (standing instructions).

Limit: the executor's GitHub token on the box is the owner's and can
already merge (recorded when PR #14 was built; not re-checked here). The gate protects against an
executor that is wrong, not one that is hostile. The executor cannot be
steered by an attacker through this path because nothing it can read in
self-repair or review mode contains untrusted text: the brief is fixed text
plus validated fields, and there is no `/wiki` mount (§3.3).
Scoping the executor's token is a follow-up, not part of this build.

### 4.5 Deploy

`harness-pull.timer` deploys the merge within 15 minutes; the new check
applies from the next 03:30 cycle. Nothing on the box changes (box paths are
outside the allowlist). Notices: `self-repair PR #n merged: <class>` on
merge.

## 5. Safety and failure modes

| failure | effect | handling |
|---|---|---|
| a harm signal fires on a good fix | a good fix is rolled back | cheap: the old state returns; attribution rules (§2.1) exclude outages; back-off treats harm classes like other failures |
| repair PR adds a check that rejects everything | tuner proposes nothing | R2 fires after 3 cycles and files a task; that fix loosens, so it waits for the owner |
| repair loop churns | many tasks or PRs | rate limits (§3.1); one open self-repair PR at a time |
| gate wrongly passes a loosening change | a check gets weaker unattended | monotonic replay + path allowlist + review agent; owner can `git revert`; all merges announced |
| executor or box down | no repair | request stays pending, counted in the weekly line; loop 1 still works (droplet only) |
| email text reaches an executor prompt | injection into a credentialed agent | request schema (§3.2) admits only enums, ids, counts and validated tool names; listener re-validates |

Kill switch: `self_repair: false` in the broker config stops dispatch and
merging; the tuner keeps rolling back. Loop 1 has its own policy flag.

## 6. Verification

- Unit tests for each harm signal, P2, the input snapshot, the request schema (rejects any
  free text), the listener's validation and template, and the gate's
  eligibility rules. Each test names the mutation it catches.
- A replay of incident A on recorded structure: the briefing fix fails its
  shadow on `unknown_tool` (or, if already live, is rolled back at the next
  cycle), and R1 files a request with no prompt text in it.
- Gate tests with four fixture PRs: a tightening change (passes), a
  loosening change in `limits.py` reached by the corpus (monotonic replay
  fails), a loosening on a path nothing reaches (diff coverage fails), a
  change touching `broker/` (ineligible). Plus a fork PR on a
  `hermes/task-*` branch (ineligible: head repository).
- The live round trip is incident A itself: loop 1 rolls back
  `20261103-23272612`, R1 files a request from it, the executor opens a PR
  adding P1 (§2.2), the gate passes, it merges, harness-pull deploys it, the
  notices arrive, and a replayed copy of the 11-03 draft is then rejected
  by P1 before apply. Recorded in the runbook. If the executor's PR does
  not pass the gate, that is a finding about the gate or the brief, and P1
  is then written by hand.

## Goals

- Bad tuner changes are undone at the next cycle, with a notice.
- The cause of an undo becomes a regression test and a check, without the
  owner, when the fix is stricter-only.
- The owner is asked only for changes that loosen a check or touch anything
  outside the tuner's checks.

## Non-goals

- Fixing Hermes failures that no tuner change caused (provider out of
  credits, MCP outages, cron failures). The repair path could take these
  later, with the merge approval.
- Automatic deploys to the box (unchanged: `deploy <sha>`).
- Changing the copy check, the shadow guard, the broker or the scanner
  unattended, in either direction.

## Anti-goals

- A check gets weaker without the owner seeing the PR.
- Any text from email or tool results reaching the executor.
- Approval or notice spam: more than a few lines a week in normal running.

## Left to the builder

- Exact thresholds for `self_inflicted_errors`, and the Hermes error strings
  the signals match (read them from Hermes, do not guess).
- The marker format for P2 and how the drafting instructions change.
- How the monotonic replay runs base and PR code side by side.
- The brief template wording, within §3.3.
- Reason class names, provided they are fixed strings.

## Build order

1. Loop 1 (droplet only): `harm.py` registry, shadow and trial wiring,
   reason classes, notices, P2, the input snapshot (§2.4) with the new
   bridge op. Deployable alone; it rolls back `20261103-23272612` at its
   first cycle.
2. Box changes, one `deploy <sha>`: self-repair and review modes in
   `run-task.sh` and `run_task`, and the `pr_status`, `pr_mark`,
   `pr_update` verbs.
3. Repair requests (droplet): R1/R2 detection including the 14-day scan,
   request schema, listener validation, dispatch, PR polling and notices,
   kill switch.
4. Gate: eligibility, monotonic replay with corpus and diff coverage,
   review dispatch, merger workflow (pushed over SSH), `tuner` added to
   required checks. `allow_auto_merge` is not needed: the workflow merges.
5. Live round trip (incident A → P1) and runbook update.
