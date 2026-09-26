# Self-healing fleet: Hermes files tasks, fleet runs them, fleet fixes fleet — Design

Date: 2026-09-26
Status: agreed in conversation, not yet built.

Extends `docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md` (the box, the
dispatcher, the verb directory, `run_fleet`, `run_task`) and the deploy gate
added by PR #14 (`request_deploy` → owner replies `deploy <prefix>` → a
single-use grant bound to one commit). Everything there still holds unless a
section below says otherwise. Touches two repositories: this one
(`hermes-harness`: broker, verbs, units) and `FibonAdithya/fleet` (§4).

## Purpose

The owner wants three things:

1. **Directed work.** Hermes, or the owner through Hermes, can hand fleet a
   specific task, and fleet executes it. Today Hermes can only start a night
   against a repository's existing backlog (`run_fleet(repo, hours)`).
2. **Reactive repair.** When fleet itself fails, the failure is detected and
   turned into a task without anyone reading logs first.
3. **fleet improves fleet.** Both of the above apply to the `fleet` repository,
   so fleet's own agents implement, review and integrate fixes to fleet.

Success: a broken fleet night in the evening becomes, by the next morning, an
announced task that the owner approves with one reply, a fix PR, and, after
the owner merges and approves the deploy, a healthy night.

## Decisions made on 2026-09-26

The owner chose each of these after seeing the alternatives.

- **Both producers, one channel.** Hermes filing tasks (directed) and an
  automatic detector (reactive) both produce the same thing: a `fleet:triage`
  issue. The detector is a second producer feeding the channel Hermes uses.
- **Every task is approved by the owner before it runs.** The alternatives were
  approving only Hermes-filed tasks, or no gate on the way in. Rejected because
  issue text becomes an agent's prompt, fleet agents run on the box *host*
  where the `claude`, `codex` and `gh` logins live (TIG spec §2), and Hermes
  reads untrusted email. The gate closes the email-to-credentialed-agent path.
- **fleet works on its own repository**, with `run_task` as the fallback
  executor for when fleet itself cannot run. Rejected: sending every fleet fix
  through `run_task` (fleet never improves itself), and a second pinned
  "stable" fleet (doubles install and deploy for a failure the fallback
  already covers).
- **Areas are read from the target repository's `ownership.md`** instead of the
  hardcoded tuple in `fleet/src/fleet/labels.py`. fleet's `AGENTS.md` lists a
  label-vocabulary change as needing a human; the owner approved this one here.
- **The detector is deterministic.** No LLM decides whether or what to file.

## 1. Current state (surveyed 2026-09-26)

- `hermes-harness` `origin/master` at `f66ad58` has `box/verbs/{add_repo,
  deploy, list_repos, log, pause_talos, run_fleet, run_talos, run_task, status,
  uptime}`, the approvals listener (`broker/hermes_broker/approvals.py`) with
  exact-match `approve <4 digits>`, `deploy <7-40 hex>` and `revoke`, and broker
  tools `request_deploy(sha)` and `deploy_harness()`.
- fleet's ledger is the target repository's GitHub issues. A task is
  dispatchable when it carries `fleet:ready`; it runs unattended only with
  `fleet:auto-ok`. `fleet discover` files to `fleet:triage` with neither.
- Areas are `("eval", "train", "models", "data", "configs", "docs")` in
  `fleet/src/fleet/labels.py:6`, validated in `policy.py:81`, `ledger.py:90`
  and the `--area` choice at `cli.py:241`. The `fleet` repository has no
  `fleet.toml`, so fleet cannot target it.
- `fleet` is a **private** repository and its `AGENTS.md` records that branch
  protection is unavailable on its plan; promotion to `main` is human
  discipline. `hermes-harness` is public and protected.
- fleet's default branch is `main`; `hermes-harness`'s is `master`.

## 2. The task path

```
producers                     box ledger (GitHub issues)           owner (approvals bot)
─────────                     ──────────────────────────           ─────────────────────
Hermes: file_task(...)  ──┐
fleet-doctor (§3)       ──┼──► issue in fleet:triage  ──► announcer posts:
fleet agents: discover  ──┘                                 "fleet#42 [doctor] <title>
                                                             <body, trimmed> · code 7310
                                                             task 7310 | solo 7310 | drop 7310"
                                                                    │
                        task 7310 ─► verb approve_task: triage → ready + auto-ok
                        solo 7310 ─► run_task(repo, "fix #42 …") in the container executor
                        drop 7310 ─► verb close_task: closed, labelled fleet:dropped
```

### 2.1 `file_task` (broker tool, ungated)

`file_task(repo, title, body, area, cls)` calls a new box verb `file_task`,
which files through the box's `gh` login. The verb:

- refuses a repository under `~/TIG` with no `fleet.toml`;
- refuses an `area` not in that repository's `ownership.md` (read with the same
  parser fleet uses, §4.1) and a `cls` not in fleet's `CLASSES`;
- creates the issue with exactly `fleet:triage`, `area:<area>`,
  `class:<cls>` and `source:hermes`, creating `source:hermes` on first use;
- never applies `fleet:ready` or `fleet:auto-ok`.

It is ungated because its only possible output is an inert triage issue, so a
Hermes cron job may call it. It returns `{repo, number, url}`.

### 2.2 The announcer

A loop inside the existing approvals listener process, which already owns the
approvals bot. `getUpdates` is single-consumer, so the announcer must live in
the listener rather than beside it. Every 5 minutes it:

1. calls a new read verb `list_triage`, which returns, for every repository
   under `~/TIG` with a `fleet.toml` (discovered, not listed), each open
   `fleet:triage` issue's number, title, body, labels, and
   `sha256(title + "\n" + body)`;
2. for each issue not yet announced, allocates a 4-digit code and posts the
   repository, number, source label, title, the body trimmed to 1,500
   characters, and the three reply forms. An issue labelled `ops` (§3.3) is
   offered `drop` only, with the line "needs you on the box";
3. records `code → {repo, number, hash, announced_at}` in
   `<state_dir>/tasks.json`, beside `grants.json`.

A code stays valid for 7 days. An issue whose hash changes is re-announced with
a new code and the old code is deleted.

### 2.3 Reply verbs

Parsed with the same rules as today: owner's numeric id only, not forwarded,
exact match.

| Reply | Effect |
|---|---|
| `task NNNN` | verb `approve_task(repo, number, hash)`: refuses unless the issue is open, still `fleet:triage`, and its current hash equals the announced one; then adds `fleet:ready` + `fleet:auto-ok` and removes `fleet:triage` (add before remove, as fleet's own CLI does since fleet PR #9) |
| `solo NNNN` | the same hash check, then `run_task(repo, prompt)` where the prompt names the issue and quotes the announced title and body; the issue moves from `fleet:triage` to `fleet:human`, a state the daemon never dispatches, and gets `fleet:solo`, so fleet does not also pick it up. The repository must be on the broker config's `repos` list, which `run_task` checks; `fleet` is added to it |
| `drop NNNN` | verb `close_task`: closes the issue with `fleet:dropped` |

`approve NNNN` remains grant approval. Task codes and grant codes are drawn so
that no code is live in both stores at once.

The listener calls the box directly over the broker's SSH key; these three
effects are the owner's command and need no grant. **None of them is an MCP
tool.** Hermes has no path to them.

## 3. fleet-doctor

### 3.1 What it is and when it runs

`box/verbs/doctor`, a deterministic Python script run by
`fleet-doctor.timer` every hour (`Persistent=false`). It examines every fleet
night under `~/nights/` whose `status` is terminal and which has no `doctored`
marker, then writes the marker. Nights started by `run_fleet` during the day are
covered as well as timer nights. It is also callable as a verb for testing.

### 3.2 Signals

Fleet-level faults only. A task that reaches NEEDS_HUMAN because it is hard is
fleet working as designed.

| # | Signal | Read from | Suggested reply |
|---|---|---|---|
| S1 | night `failed`, `stale`, or with no readable `status` file, after the daemon had logged intents | `status`, `exit_code`, log tail | `solo` |
| S2 | a night broken as in S1 whose daemon never started (no `fleet run: run_id=` line) or never logged an intent for its run id; or a night with no log | log tail, `<repo>/.fleet/run.jsonl` | `solo` |

`killed` is not a fault: `fleet run` loops until `RuntimeMaxSec`'s SIGTERM, so
every fleet night ends `killed`. An empty run log is not a fault on its own
either: fleet writes one entry per intent, not per tick, so an idle backlog
leaves it empty. (Amended 2026-09-26 during the plan audit; the first draft
listed both as faults, which would have filed on every normal night.)
| S3 | a drain was armed (`drain_reason` non-empty) | run log | `task`, class `investigation` |
| S4 | ≥3 releases or NEEDS_HUMAN moves with the same reason in one night | run log | `task` |

For S3 the issue body says that fleet's `AGENTS.md` invariant 5 forbids fixing
an unpriced transcript by adding a price row. The finding is for the owner.

The suggested reply appears in the announcement. The owner chooses.

### 3.3 Ops signatures

Some failures are not agent work: `claude`, `codex` or `gh` not logged in or
token expired; disk full; herdr server unreachable. The doctor matches these
against a short pattern table in the script, files the issue with an `ops`
label, and the announcer offers only `drop`. An agent cannot log itself back
in, and attempts spent on it would hide the cause.

### 3.4 What it files

One issue, always against `FibonAdithya/fleet`, whichever repository the night
ran on: S1–S4 are faults in fleet, not in the target. `fleet:triage`, `source:doctor`,
`doctor:<sig>`, class `investigation`. `<sig>` is the first 8 hex characters of
sha256 over the signal kind, a normalised key (exception class and top frame
for S1/S2, `drain_reason` for S3, release reason for S4), and the deployed fleet
SHA from `~/.local/share/fleet-deployed.sha`. The body carries the night id, the
fleet SHA, and at most 60 lines of evidence in a fenced block.

- **Dedup.** If an open issue carries `doctor:<sig>`, the doctor adds a comment
  "seen again in night <id>" rather than filing. Comments are outside the
  announced hash, so an outstanding code stays valid.
- **Regression.** The SHA is part of `<sig>`, so the same fault on a newly
  deployed fleet files a new issue.
- **Cap.** At most 3 new issues per run. The excess is summarised in one line
  of `~/nights/<id>/doctor.json`, which `night_status` returns and the morning
  briefing reports.
- **The doctor never files about itself.** If it exits non-zero, its unit is
  `failed`; `night_status` reports failed doctor runs.

## 4. fleet on fleet

### 4.1 Areas from the target's policy (PR to `fleet`)

fleet cannot target itself until this lands, so it arrives through `solo`, a
`run_task`, or by hand.

- `policy.load` already parses `ownership.md`. It returns the set of values in
  the `Area` column, and that set replaces `labels.AREAS` at every use:
  `ledger.py:90`, `policy.py:81`, `labels.static_vocabulary`, and `cli.py:241`,
  where `--area` becomes a free string validated after the policy loads.
- The seed script creates area labels from the loaded policy.
- The vendored fixture's `ownership.md` yields the same six areas, so the
  existing suite passes without edits. A new test loads an `ownership.md` with
  areas the fixture lacks and checks `discover --area <new>` is accepted and
  `--area <absent>` refused.
- An empty `Area` column is a load error, consistent with invariant 3 (strict
  policy parsing).
- The PR states that the owner approved this vocabulary change in this spec.

### 4.2 fleet's own policy layer (PR to `fleet`)

`fleet.toml` at the repository root: slug `FibonAdithya/fleet`, integration
branch `integration`, `slots = 3`, `per_task_usd = 2.0`, `fleet_usd = 10.0`,
and the same `[agents.claude]` / `[agents.codex]` tables as the fixture. Also
`docs/agent/ownership.md`, `docs/agent/task-classes.md`, and `.agents/roles`
and `.agents/schemas` copied from the fixture. `.fleet/` goes in `.gitignore`.

| Area | Paths (tests follow their code) |
|---|---|
| `allocator` | `src/fleet/{decide,expand,simulate}.py` |
| `ledger` | `src/fleet/{ledger,reader,gh}.py` |
| `cost` | `src/fleet/cost.py` |
| `executor` | `src/fleet/{executor,herdr,herdr_executor,git}.py` |
| `daemon` | `src/fleet/{daemon,runlog,statesocket,status}.py` |
| `cli` | `src/fleet/{cli,config,policy}.py` |
| `docs` | `README.md`, `docs/runbook.md` |

**Non-dispatchable** (never `auto-ok`) — `AGENTS.md`'s human-only list as
paths: `src/fleet/labels.py`, `src/fleet/packet.py`, `src/fleet/commands.py`
(it holds `ROLE_MAY_HAND_TO` and `VERDICT_ROLES`; a path guard cannot protect
part of a file), `src/fleet/schemas.py`, `Makefile`, `.github/**`,
`fixtures/**`, `pyproject.toml`, `uv.lock`, `AGENTS.md`, `docs/agent/**`,
`.agents/**`, `fleet.toml`.

`config.py:145` walks parent directories for `fleet.toml`. Adding one at the
repository root means any test run from inside the repository without its own
`tmp_path` config would now find it. The PR runs `make check` before and after
adding the file and records both results.

### 4.3 Agents use the deployed fleet

Agents work in `~/.fleet/worktrees/…` but call the `fleet` on `adi`'s PATH,
installed from `~/TIG/fleet` at the deployed SHA. Nothing an agent edits
changes its own CLI or the running daemon.

## 5. Promotion and deploy

1. fleet's integrator merges reviewed work to `integration`.
2. The owner merges `integration` → `main` by pull request.
3. `request_deploy(sha, target="harness")` gains `target` ∈ {`harness`,
   `fleet`}. The pending deploy and the resulting single-use grant record the
   target; the owner still replies `deploy <prefix>`, and the reply message
   names the target. A new tool `deploy_fleet()` consumes a `fleet` grant;
   `deploy_harness()` refuses one.
4. The `deploy` verb takes `target`. For `fleet` it:
   - refuses unless `sha == origin/main`;
   - refuses while any fleet night unit is active;
   - resets `~/TIG/fleet` to `sha`, runs `uv sync --locked --dev` and
     `make check`;
   - on failure, resets to the SHA in `fleet-deployed.sha`, re-syncs, and
     refuses with the check's tail;
   - on success, writes `~/.local/share/fleet-deployed.sha`.
5. Rollback is a revert pull request on `main` and the same deploy.

Because `fleet` is private, a commit can reach `main` without a pull request.
The deploy gate is the control: no commit reaches the running fleet unless the
owner approved that exact SHA.

## 6. Failure modes

- **Issue edited between announcement and reply.** `task`/`solo` refuse on hash
  mismatch; the next announcer tick re-announces with a new code.
- **Issue closed or relabelled before the reply.** `approve_task` refuses.
- **fleet broken.** The doctor suggests `solo`; `run_task` does not depend on
  fleet.
- **A fleet change breaks fleet.** The deploy's `make check` stops most. One
  that passes and still breaks a night yields a new signature on the new SHA;
  the fix is a revert and a deploy.
- **Box unreachable from the announcer.** One message on the first failure, then
  silence until a tick succeeds, then one recovery message.
- **Doctor floods.** Three new issues per run; repeats are comments.
- **Code collision.** Codes are unique across `grants.json` and `tasks.json`.
- **Subscription drain.** Unchanged from TIG spec §11: `fleet_usd` and
  `RuntimeMaxSec` bound a night; this design adds only work the owner approved.

## 7. Verification

Each new test is mutation-checked: break the code under test, confirm the test
fails, restore.

1. `file_task` with an area not in the target's `ownership.md` is refused; with
   a valid area it creates an issue with `fleet:triage` and `source:hermes`, and
   without `fleet:ready` or `fleet:auto-ok`.
2. The announcer posts a new triage issue once; the next tick posts nothing.
3. After the issue body is edited, `task <code>` is refused and the issue is
   re-announced with a new code.
4. `task <code>` leaves the issue with `fleet:ready` and `fleet:auto-ok` and
   without `fleet:triage`; `fleet plan` in that repository shows a dispatch
   for it.
5. `task <code>` forwarded, or from another Telegram id, is ignored.
6. The doctor on a synthetic failed night files one issue; a second run files
   nothing (marker); a second failed night with the same signature adds one
   comment; the same fault with a different `fleet-deployed.sha` files a new
   issue.
7. The doctor on a log with an expired-login line files an `ops` issue, and its
   announcement offers `drop` only.
8. fleet §4.1: the existing suite passes unedited; the new non-fixture-areas
   test passes.
9. `fleet plan` in `~/TIG/fleet` on the box loads fleet's policy layer.
10. `deploy` target `fleet` is refused while a fleet night is active, refused
    when the SHA is not `origin/main`, and, for a SHA whose `make check` fails,
    leaves the previous SHA checked out and recorded.
11. `deploy_harness()` refuses a `fleet` grant and `deploy_fleet()` refuses a
    `harness` grant.
12. **Round trip.** A deliberately broken `fleet.toml` deployed by hand on a
    scratch branch makes a night fail; the doctor files; `solo` produces a fix
    PR; after merge and `deploy`, the next night completes.

## Build order

`file_task`, `list_triage` and the doctor all need the `fleet` repository to
have a `fleet.toml` and fleet's label vocabulary, so fleet's two PRs come first
and cannot use the new path.

1. fleet §4.1 (areas from policy), by `run_task` or by hand.
2. fleet §4.2 (its own policy layer), by `run_task` or by hand; then seed
   fleet's labels on `FibonAdithya/fleet` with the seed script.
3. `hermes-harness`: `file_task`, `list_triage`, `approve_task`, `close_task`
   verbs; the announcer and reply verbs in the listener; `fleet` added to the
   broker's `repos`.
4. `hermes-harness`: the doctor and its timer.
5. `hermes-harness`: deploy target `fleet`.
6. The round trip (§7.12).

Everything in steps 2–4 is inert until the droplet is deployed by hand and the
listener restarted, as for PR #14.

## Out of scope

- An LLM triage or diagnosis step in the doctor.
- Automatic routing between `task` and `solo`.
- Any path where a task runs without an owner reply.
- Onboarding repositories other than `fleet` and `fleet-fixture` to fleet.
- Branch protection on `fleet` (not available on its plan).
