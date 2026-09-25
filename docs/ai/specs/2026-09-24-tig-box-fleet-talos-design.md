# The TIG box: fleet, Talos, and self-extension behind the broker — Design

Date: 2026-09-24
Status: agreed in conversation, not yet built.

Extends `docs/superpowers/specs/2026-08-05-hermes-code-and-experiments-design.md`
(the broker, grants, and executor) and `2026-07-28-hermes-assistant-vm-design.md`
(the droplet, the planes, the wiki). Everything in those two documents still
holds unless a section below says otherwise. This document adds a second
compute host with two long-running workloads, a way for the system to grow its
own tool set, and one narrow exception to the no-unattended-dispatch rule.

## Purpose

Three things the owner wants from a new 8-core box:

1. **A knowledge base** for the things sent to Hermes. This already exists as
   `hermes-wiki`; the work here is letting agents on the box read it and letting
   the box's results reach it.
2. **Overnight autoresearch** with Talos, which repeatedly edits a TIG algorithm
   and scores every candidate.
3. **Agent swarms** with fleet, which runs several coding agents against one
   repository's issue backlog, unattended.

And a property over all three: **the box should extend itself.** If Hermes
needs a new tool, or a new repository has to be pulled, that must be possible
without the owner logging in and doing it by hand, and without giving Hermes a
shell on a box that holds credentials.

## Decisions made on 2026-09-24

Recorded so the reasons survive. The owner chose each of these after seeing the
alternatives.

- **Hermes stays on the droplet.** Moving it onto the box was considered and
  rejected: the gateway reads untrusted email and forwarded messages, and the
  box holds subscription logins and a GitHub token. The plane separation in the
  07-28 spec is the reason the assistant can read email at all.
- **fleet and Talos are broker tools**, reached through the grant model of the
  08-05 spec. Hermes asks, the owner approves a code, the broker dispatches.
- **Nightly runs are started by systemd timers on the box**, not by Hermes
  cron. This is the one exception to "no unattended dispatch" and §8 states its
  bounds.
- **The knowledge base is the wiki, and only the wiki.** GBrain was considered
  because `company_brain` already runs on it; it is deferred, because the wiki
  is what the owner actually uses and the 07-28 spec chose ripgrep over a
  database on purpose.
- **Self-extension goes through pull requests.** A gated raw-shell tool was
  considered and rejected for now; §10 says how a new verb or tool arrives.
- **Talos compiles locally first, Modal second.** The TIG dev image ships an
  arm64 build (MEASURED 2026-09-24: `docker manifest inspect` lists amd64 and
  arm64), so the box scores candidates natively at no per-iteration cost. Modal
  is configured for confirming winners on x86.
- **Agents authenticate with logged-in `claude` and `codex` CLIs**, billed
  through subscriptions, not API keys. Hermes keeps its own provider on the
  droplet.
- **The first fleet target is `fleet-fixture`.** It is the only repository with
  the policy files fleet requires. Onboarding a real repository is a follow-up.

## 1. Machines

| Host | What it is | Role |
|---|---|---|
| droplet (`hermes-vm`) | DigitalOcean, 2 vCPU / 4 GB, Ubuntu 24.04, user `hermes` | Gateway, wiki, **broker** |
| the box (`tig-server`) | Hetzner, hostname `adi1`, **aarch64**, 8 vCPU / 15 GB / 150 GB, Ubuntu 26.04 | fleet, Talos, herdr, the dispatcher |
| `tig-gpu` | rented, one GPU | unchanged: `gpuq` via the broker |

Surveyed 2026-09-24 over SSH:

- **The box is bare.** git 2.53, curl, wget, apt. No node, python tooling,
  Docker, gh, herdr, claude, or codex. Root login by key only, no other users.
  It is not the 2 vCPU / 3.7 GB machine the 08-05 spec surveyed; the same
  address now answers as a new, larger ARM host.
- **The droplet runs Hermes v0.19.0** under `systemd --user` with the wiki sync
  and repo mirror timers from the 07-28 as-built notes. The broker is **not**
  deployed there; the 08-05 plan stopped at Task 4 and nothing from that task on
  was ever applied.
- **The wiki has 82 pages and 176 commits** at
  `FibonAdithya/hermes-wiki`, written from inside the droplet's sandbox as
  container-root and pushed every 15 minutes by the host timer. Its
  `CONVENTIONS.md` says the agent only edits files and never runs git there.
- **ARM matters twice.** Cargo builds on the box produce aarch64 binaries, so a
  Talos score from the local backend is an ARM number and is not comparable to
  `tig-gpu` or the laptop. And every container image the box runs must have an
  arm64 build; the TIG dev image and `node:22-bookworm-slim` do.

## 2. Planes, extended

The 08-05 spec's four planes become five. The rule is unchanged: no plane holds
both untrusted input and a durable credential.

| Plane | Runs | Holds |
|---|---|---|
| Gateway (droplet) | `hermes gateway` | Telegram token, Nous key |
| Integrations + broker (droplet) | Gmail/Calendar MCP, compute MCP | Google tokens, `tig_id`, the box key |
| Sandbox (droplet, `--network=none`) | agent's terminal and file tools | wiki working copy |
| **Box, host** | herdr, fleet, the dispatcher, timers | **claude, codex, gh logins, resident** |
| **Box, containers** | Talos jobs, `run_task` executor | per-run credentials only |

What changed and why: the 08-05 spec kept the executor host empty and treated
it as scratch. That is no longer true. fleet launches `claude` and `codex`
through herdr on the host, so their logins live on the box permanently. This is
the concrete reason Hermes is not moved there, and it is why the SSH path from
the broker to the box is a forced command (§4) rather than a shell.

## 3. Provisioning the box

One non-root user, `adi`, in the `docker` and `sudo` groups. Root stays for SSH
recovery only. Everything below runs as `adi`.

**Toolchain.** node 24; `uv` with Python 3.10 (Talos) and 3.11 (fleet, the
broker tests); Docker; `gh`; herdr 0.7.5 (matching the laptop; client and
server skew is the known breakage risk); `claude` and `codex` from npm. Each of
`claude`, `codex`, and `gh` is logged in once, interactively, over `ssh -t`.
`herdr --remote tig-server` from the laptop is the daily entry point, and the
herdr server runs as a user service with lingering enabled so it is up before
anyone logs in.

**Guards.** The owner's `~/.claude/CLAUDE.md`, hooks, and settings are copied to
`~adi/.claude`, so the git and transfer guards that apply on the laptop apply
to agents on the box.

**Repositories** under `~adi/TIG`: `fleet`, `fleet-fixture`, `Talos`,
`hermes-harness` (the deployed copy, see §10), and a read-only `hermes-wiki`
mirror (§9). More arrive through `add_repo` (§5).

**Provisioning is a script**, `box/provision.sh` in this repository, idempotent,
run over SSH as root once and re-runnable. The interactive logins are the only
steps it cannot do and it says so at the end.

## 4. The dispatcher

The broker reaches the box as `adi` over SSH with a dedicated key. That key's
`authorized_keys` line carries `command="~/.local/bin/dispatch"` plus
`no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding`. Whatever the
broker sends arrives in `SSH_ORIGINAL_COMMAND`; the dispatcher parses it as a
verb and JSON arguments and refuses anything else. There is no shell on that
path.

**The verb table is a directory**, `box/verbs/<verb>`, each an executable that
takes its arguments as JSON on stdin and writes JSON to stdout. The dispatcher
lists the directory at call time, so adding a verb is adding a file (§10). A
verb that is not a file in that directory does not exist.

**Long verbs detach.** `run_fleet`, `run_talos`, and `run_task` create a
transient unit with `systemd-run --user --unit night-<id> --property
RuntimeMaxSec=<limit>` and return `{id}` within seconds. The unit logs to
`~/nights/<id>/log` and writes `~/nights/<id>/status` (`running`, `done`,
`failed`, `killed`) and `exit_code` on exit, the same contract the 08-05
`run-task.sh` used. `RuntimeMaxSec` is the hard stop nothing can extend from
chat.

**Read verbs answer from files.** `status` lists `~/nights/*/status` with start
times and the unit's active state; `log` returns the last N lines of one log;
`list_repos` lists `~/TIG`. They are cheap enough to call from a cron job.

## 5. Broker tools

Added to the 08-05 §3 table. Gating is the same code path: a live grant for
`tig-server`, or `LOCKED: no active grant for tig-server`.

| Tool | Gated | Verb | What it does |
|---|---|---|---|
| `run_fleet(repo, hours)` | yes | `run_fleet` | `fleet run --new-run` in `~/TIG/<repo>`; budget comes from that repo's `fleet.toml`, the hour limit becomes `RuntimeMaxSec` |
| `run_talos(challenge, direction, iterations, backend)` | yes | `run_talos` | `talos run --challenge … --direction … --budget-iterations … --yes` in `~/talos-<backend>`, whose `talos.config.json` fixes the backend |
| `run_talos(resume=<job id>, backend)` | yes | `run_talos` | `talos run --resume <job id> --yes` in `~/talos-<backend>`; the job keeps its own challenge, direction and budget, so passing those with `resume` is refused, as is a resume while any Talos night on that backend is live (Talos keeps no per-job lock) |
| `run_task(repo, prompt, minutes)` | yes | `run_task` | the 08-05 executor: Claude Code in a throwaway container, PR only |
| `add_repo(name)` | yes | `add_repo` | clones `FibonAdithya/<name>` into `~/TIG/<name>` |
| `night_status()` | no | `status` | every night, running or finished |
| `night_log(id, lines)` | no | `log` | tail of one night's log |
| `list_repos()` | no | `list_repos` | what is checked out |

**Allowlists the agent cannot edit.**

- `run_fleet` refuses a repository with no `fleet.toml`; fleet itself refuses
  one whose `task-classes.md` or `[agents.<kind>]` tables are incomplete, at
  startup, before dispatching anything.
- `run_talos` accepts only challenge names the pinned Talos knows and only the
  two backends that were set up.
- `add_repo` clones only repositories the owner's GitHub account **owns**,
  checked on the box with `gh api repos/<owner>/<name>` (owner login must match,
  and not a fork). This is the
  same rule the droplet's mirrors use: no list to maintain, and nobody else's
  code can arrive this way. Forks and org repositories are out.
- `run_task` keeps the 08-05 repo list on the broker side.

**`run_task` keeps its container.** §5 of the 08-05 spec chose a disposable
container with per-run credentials. That still holds on the box, and it is
stricter than running on the host would be, so it stays: the image is
`executor/Dockerfile` built for arm64, the Claude credential is the host
login's file mounted read-only, the GitHub token is injected as an environment
variable for the run. Whether a read-only mount of the host's credentials file
survives a token refresh inside the container is unverified; the plan's
first-run-by-hand step checks it, and the fallback is the plan's separate
credential copy.

## 6. fleet on the box

fleet is used as its runbook describes; nothing in fleet changes.

- `uv sync --locked --dev` in `~/TIG/fleet`; `fleet` on `adi`'s PATH.
- The herdr server on the box is fleet's executor. `HerdrExecutor` shells out
  to `herdr worktree create` and `herdr agent start --kind claude|codex`, so
  both CLIs must be logged in and `fleet.toml` must carry an
  `[agents.claude]` and `[agents.codex]` table for every routed kind.
- Worktrees under `~/.fleet/worktrees`, outside every repository.
- `FLEET_ROLE` and `FLEET_AGENT` are set by fleet for each agent it launches;
  the dispatcher sets neither, because it never runs an agent itself.
- The fixture backlog is seeded with `fixtures/seed/seed_issues.sh` against
  `FibonAdithya/fleet-fixture`, and `fleet run --once` is run by hand and its
  rendering read before the first unattended night.
- A night is `fleet run --new-run` with the fixture's `fleet_usd` cap. The
  `--new-run` flag matters: fleet resumes the last run id by default, and a
  resumed night that already drained would dispatch nothing while looking
  alive.

## 7. Talos on the box

Talos reads and writes its config in the current directory, so two directories:

- `~/talos-local`: `talos setup` with backend `local`, provider `claude-cli`,
  mode single-shot. Setup pulls the 13 GB knapsack dev image and runs one
  warm-up build. The live local smoke test is run once so the first night does
  not spend itself on a cold cache. MEASURED 2026-09-24 on this box (8 cores,
  11 GiB container, knapsack, `talos run --budget-iterations 1`, run
  20260924-120337): baseline ready after 2007 s (build plus 32 training nonces
  plus held-out), one candidate scored 1490 s later (build 6m24s, the rest
  scoring: each nonce runs about 60 s on this box), 58 minutes end to end.
  With the baseline cached, an 8-hour night fits about 18 candidates, so
  `iterations` in the night config should be sized to that, not 30.
- `~/talos-modal`: `talos setup` with backend `modal`, same provider. Used only
  when a `run_talos` call names it, to confirm a local winner on x86.

Both directories are a checkout of the pinned Talos at the same commit as the
laptop's. `runs/` inside each is where Talos writes results; the night's
`~/nights/<id>/` holds the log and a copy of the winning package path.

## 8. Unattended nights

Two user timers on the box, `fleet-night.timer` and `talos-night.timer`, each
calling the same verb script the dispatcher would, with arguments read from
`~/nights/config.toml`. That file is edited over SSH by the owner and by nothing
else: it is outside every repository, not writable by any verb, and not mounted
into any container.

This is a deliberate exception to the 08-05 rule that nothing dispatches
without a live human. Its bounds:

- **Fixed inputs.** The timers take no input from Hermes, chat, email, or the
  wiki. The only way to change what runs at night is to edit the file.
- **Hard caps.** fleet's dollar budget from `fleet.toml`; Talos's iteration
  budget from the config; `RuntimeMaxSec` on both units so nothing outlives the
  morning; `Persistent=false` on the timers so a missed night is skipped, not
  run late into a working day.
- **Loud.** Each night writes `~/nights/<id>/status`, which the morning briefing
  reads (§9). A night that never wrote a status file is reported as such.
- **Off by default.** The timers are installed disabled. The owner enables each
  one deliberately after a dry night with tiny budgets has completed.

The Talos queue: `config.toml` holds a list of `[[talos.queue]]` entries of
challenge and direction; each night pops the first, records it under the
night's directory, and rewrites the file. An empty queue means no Talos night
and a status of `skipped`.

## 9. Knowledge base

The wiki is the knowledge base. Two mechanisms connect the box to it.

**Reading.** A read-only clone at `~/TIG/hermes-wiki`, refreshed by a user
timer every 15 minutes with `git fetch` and `reset --hard origin/main`, using
the box's `gh` login. Agents search it with `rg` exactly as the droplet's agent
does. Nothing on the box commits to it: the wiki's one-writer rule from
`CONVENTIONS.md` is what keeps the droplet's push timer from ever hitting a
non-fast-forward, and that timer does no pull.

**Writing.** The box never writes the wiki. Instead, the droplet's existing
`morning-briefing` cron job gains one instruction: call `night_status()` and,
for any night finished since the last briefing, `night_log()`, then write or
extend a wiki page `nights/<date>.md` with the outcome, and report it in the
briefing. `night_status` and `night_log` are ungated reads, so a cron job may
call them, and the page is written by the wiki's one writer through its normal
file tools. No new cron job, no new credential.

**Not built:** GBrain on the box, `gbrain serve`, or any second store.
`company_brain` continues on the laptop unchanged.

## 10. Self-extension

The box grows by pull request. `hermes-harness` is the deployed source of
truth for both hosts.

**What lives in the repository.** `broker/` (the droplet side, unchanged in
shape), `executor/` (the `run_task` image), and a new `box/`: `provision.sh`,
`dispatch`, `verbs/`, the systemd unit and timer files, and `install.sh`, which
copies the units and the dispatcher into place and reloads the user manager.

**The loop.**

1. Hermes decides it needs a new capability, say a verb that runs a repository's
   test suite, or a new broker tool.
2. It calls `run_task("hermes-harness", "<what to add and how it is tested>",
   minutes)` under a grant. Claude Code adds the verb file, the broker tool if
   one is needed, and tests, and opens a PR.
3. CI runs the broker tests and lints every file in `box/verbs` (shellcheck or
   `python -m py_compile`, by extension). The PR cannot merge red.
4. The owner reads and merges.
5. A pull timer on each host (`harness-pull.timer`, every 15 minutes) fetches
   `main`, and if it moved, runs `box/install.sh` on the box or restarts the
   broker on the droplet. Merge is deploy.

**What makes this safe is branch protection, not the loop.** `main` on
`hermes-harness` requires a pull request, a passing CI check, and blocks direct
pushes. Without that, an executor token could push a verb straight to `main`
and have it installed within 15 minutes. The plan verifies the protection by
attempting the push and reading the refusal.

**Repositories arrive the same way, faster.** `add_repo` is a verb rather than
a PR because cloning a repository the owner owns adds no code the owner did not
write. What a fleet or Talos night may then do with it is still bounded by that
repository's own policy files and budgets.

**Hermes's own skills** are outside this loop: Hermes creates and refines
skills on the droplet as it already does. The loop covers what runs on the box
and what the broker exposes.

## 11. Failure modes

- **A verb hangs.** `RuntimeMaxSec` kills the unit; status reads `killed`; the
  morning page says so.
- **The box is unreachable.** The broker's SSH wrapper has `ConnectTimeout`
  from the 08-05 spec; tools return `tig-server unreachable`, not a hung turn.
- **The forced command is bypassed.** It cannot be through SSH. The remaining
  path is a merged PR that changes `dispatch` itself, which is the owner's own
  review. `harness-pull` runs `install.sh` only from `main`.
- **A night drains the subscription.** Both CLIs draw on the owner's Claude and
  ChatGPT allowances, so a runaway costs working capacity rather than money.
  fleet's dollar cap and Talos's iteration cap bound it; the morning page
  reports fleet's `spent_usd` from the run log.
- **The wiki mirror diverges.** It is a hard reset every 15 minutes; anything an
  agent writes there is lost by design, as with the droplet's repo mirrors.
- **herdr version skew.** The box and the laptop must both run 0.7.5; `herdr
  --remote` fails visibly on skew. `provision.sh` pins the version.
- **Clocks.** Nights are named by UTC date. The droplet runs BST.
- **`talos setup` on Modal drifts.** After pulling a new Talos, `talos setup`
  must be re-run in `~/talos-modal`; a client that reaches an older deploy
  stops with a message naming it. The local backend has no deploy to drift.

## 12. Cost

- fleet nights: bounded by `fleet_usd` in `fleet.toml` (the fixture's is
  $25) and by subscription quota, since agents are the CLIs.
- Talos local nights: subscription quota only; no compute cost.
- Talos Modal runs: container seconds, on demand, never from a timer.
- Hermes: rises slightly, as the 08-05 spec predicted, from reading nights back.
- The box: fixed monthly, already paid.

## 13. Verification

The 08-05 §9 list still applies to the broker. Added:

1. **Bare box provisioned by script** on a fresh login: `provision.sh` runs
   twice and the second run changes nothing.
2. **Forced command holds.** `ssh -i <broker key> adi@box id` returns a
   dispatcher refusal, not a uid.
3. **Locked by default.** `run_fleet`, `run_talos`, `run_task`, `add_repo`
   without a grant return `LOCKED`.
4. **Cron cannot start a night.** A Hermes cron job calling `run_fleet` gets a
   code nobody approves, then `LOCKED`. `night_status` from the same job works.
5. **`add_repo` refuses** a repository the owner does not own.
6. **Talos local live run** on knapsack completes with a measured build time
   recorded in this document's §7.
7. **`fleet run --once`** against the fixture dispatches at least one task and
   `fleet status` renders it.
8. **Dry night.** Both timers with tiny budgets (fleet one slot, Talos two
   iterations) complete, write `status`, and the next morning briefing reports
   them on a `nights/<date>.md` page.
9. **Self-extension round trip.** A `run_task` adds a trivial verb via PR; CI
   passes; after merge the verb is callable from the broker within 15 minutes
   without anyone touching the box.
10. **Branch protection.** A direct push to `main` from the executor container
    is refused.
11. **Wiki mirror** on the box matches `origin/main` after a page is added on
    the droplet.
12. **Subscription, not API.** The executor's `claude -p` runs without
    `--bare` and without `ANTHROPIC_API_KEY` set.
13. **The executor cannot merge its own pull request.** Not met, by the
    owner's decision (As-built item 1): the executor uses the owner's token
    and deploys are manual.

## Out of scope

- Moving Hermes, or running a second Hermes.
- GBrain on the box, or any shared brain server.
- A gated raw-shell tool. If `run_task` proves too slow for small changes, add
  it later as one verb with a logged transcript.
- Onboarding a real repository to fleet. `fleet-fixture` only in this round.
- Talos agentic mode.
- Work-org repositories and credentials.
- A push proxy for the executor's in-run credential exposure (still open from
  08-05).
- x86 comparability of local Talos scores. Modal is the answer when it matters.

## As-built corrections (2026-09-24)

1. **The executor runs on the owner's token, and deploys are manual.** Branch
   protection with zero required reviews only guarantees a PR and a green
   check, and the executor's PAT is the owner's, so it can merge what it
   opens; a review rule cannot help because GitHub forbids approving one's
   own PR. A separate identity (a GitHub App installed on all repositories,
   or a machine account) would restore the "owner reads and merges" gate.
   **Owner's decision, 2026-09-24: keep the owner's token.** Reasoning: a
   separate identity has to be granted access to every new repository by
   hand, and everything the executor does is in git and can be reverted.
   Consequences: `harness-pull.timer` stays disabled on both hosts; a merge
   is deployed by hand with `systemctl --user start harness-pull.service`
   after reading `master`; the gate is the owner's approval of the grant, not
   review of the PR; `master` forbids force-pushes, so a rollback is a revert
   PR. §13 item 13 is therefore not met and is recorded as accepted.
2. **herdr is pinned from the GitHub release**, not `herdr.dev/install.sh`,
   which has no version pin. The headless server is the bare `herdr server`.
3. **The node base image owns uid 1000.** The executor renames that user to
   `runner` so the bind-mounted night directory (adi, uid 1000) is writable.
4. **The container gets `<night>/work`, not the night directory.** `status`,
   `log`, `exit_code` and `meta.json` are outside the mount and are read with
   `O_NOFOLLOW`; a task cannot point them at the owner's files.
5. **A headless run that asks a question ends with no PR.** The executor's
   system prompt now tells it to decide and say so in the PR.
6. **Talos on aarch64** needed an architecture-aware artifact path (Talos
   PR #24) and an 11 GiB container; a candidate takes about 25 minutes and a
   baseline about 33 on this box (§7).
7. **`fleet run --once` launched six agents that exited within minutes** and
   left no Claude transcript. Not diagnosed here; a fleet question.
8. **Deploys are stamped.** `harness-pull` records the deployed SHA only after
   the install succeeded, so a failed install is retried on the next tick.
9. **The agent can deploy the box, one approved commit at a time
   (2026-09-25).** This amends item 1: "deployed by hand after reading
   `master`" becomes "deployed after the owner reads one commit and approves
   it". `request_deploy(sha)` records a pending deploy; the owner approves by
   typing `deploy <prefix>` of the commit they read in the approvals chat; the
   grant names that commit, lasts 10 minutes, and is consumed by one
   `deploy_harness()`. The box's `deploy` verb installs exactly that commit and
   refuses if it is no longer `origin/master`, so a merge that lands after the
   approval is never deployed under it. A `tig-server` grant does not allow a
   deploy. The droplet, which holds the broker's keys and the approvals bot, is
   out of the agent's reach and stays deployed by hand, so no agent-merged
   change can alter the gate that issues grants without the owner deploying it.
