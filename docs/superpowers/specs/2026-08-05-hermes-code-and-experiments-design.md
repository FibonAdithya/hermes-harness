# Hermes: Code, Experiments, and Pull Requests — Design

Date: 2026-08-05

Extends `2026-07-28-hermes-assistant-vm-design.md`. That design deliberately
placed "burst/GPU dispatch and the experiment-PR workflow" out of scope when
the agent's role narrowed to personal assistant, while recording that the idea
was sound and worth revisiting. This is the revisit. Everything in the assistant
spec still holds; this document adds a fourth plane and a gated dispatch path,
and changes none of the existing security controls.

## Purpose

Let the owner ask Hermes, from Telegram, to run an ML experiment or do a piece
of software work, and get back a result and a pull request — without the
assistant itself becoming a coding agent, and without the assistant's untrusted
inputs reaching work infrastructure ungated.

Three facts shape the whole design:

1. **The assistant reads untrusted text.** Email from anyone who knows the
   owner's address, and forwarded Telegram messages (§As-built #20). Any new
   capability is reachable by an attacker who can write to those channels.
2. **The compute is not the assistant's.** `tig-gpu` and `tig-server` are work
   machines provisioned to the owner and shared with other agents. Actions there
   affect colleagues.
3. **Hermes is a poor coding agent.** As-built #16/#17 record a model
   confabulating tool names and failing to drive `terminal`. The fix is not a
   better prompt; it is not making Hermes write the code.

## 1. The machines

| Host | What it is | Role here |
|---|---|---|
| droplet | 2 vCPU / 4 GB, DigitalOcean, always on | Gateway, broker, air-gapped sandbox |
| `tig-gpu` | vast.ai rented container, one GPU, shared | Compute, via `gpuq` only |
| `tig-server` | Hetzner 2 vCPU / 4 GB / 75 GB, Ubuntu 26.04 | Executor host for Claude Code |

Surveyed 2026-08-05:

- **`tig-gpu` runs its own queue.** `python -m gpuqueue.cli_runner --config
  /workspace/gpuq.toml` has been up 10 days. Submission and execution both
  happen on the box, so nothing here depends on the owner's laptop being awake.
- **`gpuq` is at `/venv/main/bin/gpuq`, not on the `PATH`** for a
  non-interactive SSH shell. Same trap class as As-built #5. Invoke by absolute
  path.
- **`gpuq.toml` is a project registry.** Each `[project.<name>]` block names a
  git remote, a checkout, a venv, and `commit_artifacts`. A job can only run
  against a registered project, and registering one is a deliberate edit on
  `tig-gpu` that the agent never makes. **This registry is the compute
  allowlist and it already exists.**
- **`tig-server` is empty.** Only `sshd` and `systemd-resolved` listening. No
  Docker, no services, no non-root users, 11% disk used. It is a bare box, and
  is treated here as disposable scratch.
- **`tig-gpu` is rented and will move.** Host and port change when the instance
  is recreated. Its SSH target is configuration, and "the box is gone" must be a
  clean error rather than a hung turn.

## 2. Four planes

Extends the three planes of the assistant spec §2. The point of the split is
that no plane holds both untrusted input and a durable credential.

| Plane | Runs | Holds |
|---|---|---|
| Gateway (droplet) | `hermes gateway` under systemd `--user` | Telegram token, Nous key |
| Integrations + **broker** (droplet, host subprocesses) | Gmail/Calendar MCP, **compute MCP** | Google OAuth tokens, **`tig_id`, `tig_gpu`** |
| Sandbox (droplet, Docker, `--network=none`) | agent's `terminal`/file tools | wiki working copy — no credentials, no egress |
| **Executor (`tig-server`, per-task container)** | `claude -p` | Claude Code login, GitHub token — **only during a run** |

Two properties follow, and they are the reason for the shape:

- **The GitHub credential never touches the droplet.** Claude Code does its own
  `git` and `gh` inside the executor. The broker has no `push_branch` and no
  `open_pr` tool, because it needs none.
- **The Hermes sandbox keeps its air gap.** `TERMINAL_DOCKER_NETWORK=false`
  stays exactly as As-built #1 set it. Nothing in this design gives the agent's
  own shell a socket.

## 3. Broker tool surface

A host-side stdio MCP server on the droplet, registered in `config.yaml`
alongside `workspace-mcp`, following the pattern As-built #11–13 proved: the
agent calls tools, the credentials stay in the subprocess, and
`mcp_servers.<name>.tools.include` names exactly the tools registered.

| Tool | Gated | What it does |
|---|---|---|
| `request_access(box, minutes, reason)` | — | Asks for a grant. Grants nothing. |
| `run_task(repo, prompt, minutes)` | yes | Launches a Claude Code task on `tig-server`; returns a task id immediately |
| `task_status(id)`, `task_log(id)` | no | Reads a task this broker launched |
| `submit_job(project, commit, lane, artifacts, cmd)` | yes | `ssh tig-gpu /venv/main/bin/gpuq submit …` |
| `job_status(id)`, `job_logs(id)`, `job_cancel(id)` | no | `gpuq show` / `list` / `cancel` |
| `fetch_artifacts(id)` | no | Copies a finished job's artifacts into the sandbox bind mount |

Gated tools return `LOCKED: no active grant for <box>` when no live grant
exists. Read-only tools stay ungated so that a grant expiring mid-task does not
prevent the assistant from reporting the result.

Two allowlists sit underneath, and the agent can write to neither:
`gpuq.toml`'s project registry decides what can run on `tig-gpu`, and a
broker-side repo list decides what `run_task` may check out.

## 4. Access: the agent asks, the owner answers

1. The agent calls `request_access("tig-server", 60, "fix the failing parser test in X")`.
2. **The broker** generates a four-digit code — not the model — and records a
   pending request that expires in two minutes.
3. The agent tells the owner in Telegram what it wants, why, and the code.
4. The owner replies `approve 7391`. A gateway hook sees the reply and writes
   `{box, expires_at}` (epoch seconds) to a root-owned token file **only if**
   the sender is the owner **and the message is not forwarded**.
5. Gated tools work for that box until expiry. `revoke` kills it immediately.

Four properties worth stating explicitly:

- **The agent's belief is irrelevant.** Only the broker reads the token, so an
  injected turn asserting "the owner approved this" changes nothing.
- **The forwarded-message check is load-bearing.** Without it, As-built #20's
  injection road runs straight through the approval step: text someone else
  wrote, forwarded in, arrives through the channel `TELEGRAM_ALLOWED_USERS` has
  already vetted as the owner.
- **Cron can never hold a grant.** Approval requires a live human inside two
  minutes. This closes the As-built #16/#19 runaway path by construction rather
  than by discipline — an agent-created job cannot dispatch anything.
- **The approval gate is configured, not patched.** It rides on a gateway hook
  declared in `config.yaml`, not a source edit, because
  `updates.non_interactive_local_changes: stash` silently reverts source patches
  (As-built #20) and a gate that stops working without saying so is worse than
  no gate.

**The residual risk is inherent to the session-unlock model and is accepted.**
The owner approves a window on a box, not a specific command, and the agent
writes the reason the owner reads. What bounds it is what the window can reach:
on `tig-gpu`, a queue and a project registry; on `tig-server`, a box that holds
nothing.

## 5. The executor

Claude Code runs the code. Hermes never does.

**Why.** As-built #16/#17 is the record of what happens when a model that
cannot reliably drive its tools is asked to do engineering work. Claude Code is
a purpose-built coding agent with its own permission system, and it is covered
by the owner's existing subscription (Pro $17–20/mo, Max from $100/mo), so the
marginal cost of a task is quota rather than tokens.

**Disposable, per task.** Each `run_task` starts a throwaway container on
`tig-server`:

```
docker run --rm  hermes-exec:latest  …
  → clone <repo> at a fresh checkout
  → claude -p "<task>" --dangerously-skip-permissions --output-format json
  → git push  hermes/<slug> ; gh pr create
  → container destroyed
```

Credentials are injected for the run rather than resident on the host between
runs. Docker is not currently installed on `tig-server`; installing it is part
of the build.

**Permissions: skipped inside, constrained outside.** The container runs Claude
Code without per-command approval. Enumerating an allowlist for real work —
`npm`, `pytest`, `uv`, `make`, whatever the repo uses — fails constantly, and
each failure aborts a run mid-task, which pushes the allowlist wider until it is
unrestricted with extra steps. The constraint is moved to the outcome instead.

**What actually makes "pull requests only" true is branch protection, not a
Claude Code flag.** A token that can push a branch can also push to `main` and
merge the PR — both are `Contents: write`. So every repo on the broker's list
requires: protected `main`, pull request required, direct pushes blocked,
auto-merge disabled. Without that, "it only opens PRs" describes an intention.
With it, the owner's review is a real gate.

**What this does and does not buy.** The disposable container removes *durable*
credential exposure: nothing is left on the host to steal between runs, and the
blast radius of one bad task ends when the container does. It does **not**
remove in-run exposure — during a task, unrestricted `bash` inside the container
can read the credentials it was given. Closing that would need a push proxy that
injects the token outside the sandbox, which is out of scope here. This is
recorded plainly because As-built #12 is the cost of a security claim that was
stated more strongly than it held.

**Do not use `--bare`.** The Claude Code docs recommend it for scripted calls,
and it is wrong here: bare mode does not read the subscription login and expects
`ANTHROPIC_API_KEY`, so it silently moves every run from flat-rate subscription
onto per-token API billing. This is the same failure shape as As-built #8 and
#19 — a silent provider switch discovered from the bill.

Other flags in use, confirmed from the current docs: `--output-format json`
returns `total_cost_usd` and a session id per invocation; `--append-system-prompt`
carries the scope constraints (which repo, what "done" means, what not to touch);
`--resume <id>` continues a specific task when the owner wants a follow-up.

**Commits are attributable.** Git identity inside the container is set to the
"Hermes Agent" identity from the assistant spec §5, so every commit and PR the
executor produces is visibly distinct from the owner's own work in `git log`
and in PR authorship.

**Runs are detached.** `run_task` returns a task id immediately and writes to a
log the broker can read. A Telegram turn must not block for the minutes a real
coding task takes.

## 6. Data flow

An experiment, end to end:

```
Telegram: "run the v0 eval on wgan-synthetic"
  → agent greps the wiki for prior work
  → request_access("tig-server", 60, reason) → code shown
  → owner: approve 7391
  → run_task(repo, "make the change and push a branch")
      → container: claude -p … → commit, push hermes/<slug>, gh pr create
  → agent polls task_status / task_log
  → request_access("tig-gpu", …) → approve
  → submit_job(project, commit=<the pushed sha>, lane=gpu, artifact=…)
  → gpuq queues; the runner checks out that exact commit
  → job_status / job_logs → fetch_artifacts → results land in the sandbox
  → agent reads them, writes wiki pages
  → Telegram: PR link, the numbers, what it means
```

The ordering is not a convention — it falls out of `gpuq`'s own rule that the
commit must be pinned. **Push first, then submit.** Nothing runs on the GPU that
is not in a commit the owner can read back.

## 7. Failure modes

- **`tig-gpu` vanishes.** Rented; its host and port will change. SSH target
  lives in broker config, `ConnectTimeout` is set, and a dead box returns
  `tig-gpu unreachable` rather than hanging a turn.
- **File ownership.** The agent writes as container-root; host-user `git` over
  the same bind mount fails. This bit the previous build (§7.5). Anything the
  broker does across that mount runs as root in a throwaway container — the
  proven pattern from the wiki sync timer.
- **Grant expires mid-run.** The task and the queued job keep going; they are on
  other machines. Only the gated tools lock, and since status and log tools are
  ungated the agent can still report the outcome.
- **Task or PR failure** surfaces verbatim in Telegram, per the assistant spec's
  no-silent-failure rule.
- **Clocks.** The token stores epoch seconds. The host runs BST, the sandbox
  runs UTC (As-built #22), and `tig-gpu`'s container has its own clock again.
- **`hermes update`.** The approval hook is configuration, not a source patch,
  for the reason in §4.

## 8. Cost

The assistant currently costs ~$1/month on `deepseek/deepseek-v4-pro`. This
design deliberately keeps the expensive work off that meter: Claude Code runs on
the owner's existing subscription, so a task costs **quota, not tokens**.

Two honest caveats. Headless runs draw on the same allowance as the owner's own
interactive Claude Code use, so a runaway spends the owner's working capacity
rather than money — a better failure mode than As-built #19's 24.4M tokens in
100 minutes, but still a reason the unlock gate exists. And Hermes' own token
use rises somewhat, since composing tasks and reading back results is more
context than a morning briefing. Measure with the existing method
(`grep 'API call #' ~/.hermes/logs/agent.log` summed per session) and review
after a week; log the `total_cost_usd` from each `run_task` alongside it.

## 9. Verification

1. **Locked by default** — `run_task` and `submit_job` with no grant return `LOCKED`.
2. **Forged approval** — the agent claims approval; the broker still refuses.
3. **Forwarded approval rejected** — forward a message containing `approve <code>`; no grant is issued. This is the injection road.
4. **Cron cannot hold a grant** — a scheduled job that requests one never gets it.
5. **Air gap intact** — `curl https://example.com` from the Hermes sandbox still fails. Regression test on the whole point of As-built #1.
6. **Subscription, not API** — `--bare` absent from the launch command, and a run confirmed to consume subscription rather than `ANTHROPIC_API_KEY`.
7. **Executor is disposable** — container is gone after the run; no credential files remain on `tig-server`.
8. **Branch protection holds** — a direct push to `main` from inside the executor is refused by GitHub, and the PR cannot self-merge.
9. **Repo allowlist** — `run_task` against an unlisted repo is refused.
10. **gpuq round trip** — a trivial `--lane cpu` job: submit, id, logs, artifacts back in the sandbox.
11. **Expiry and revoke** — tools re-lock on their own; `revoke` is immediate.
12. **Dead box** — a wrong `tig-gpu` target returns a clean error inside the timeout.
13. **Injection** — an email instructing the assistant to run a job; expect a report, not a `request_access` call.
14. **Spend and quota check** after one week of real use.

## Out of scope

- **Merging.** The owner merges. Branch protection enforces it.
- **Work-org repos.** Personal repos only. A work-account credential is a
  separate authorisation question and is not answered here.
- **`tig-server` as a service host.** It stays scratch. Nothing valuable lives
  there; no deploys, no restarts, no long-lived state.
- **A push proxy.** It would close the in-run credential exposure described in
  §5, and it is not built now.
- **vast.ai API access.** The agent must never be able to create or destroy
  instances.
- **Editing `gpuq.toml`.** Registering a project is the owner's deliberate act
  on `tig-gpu`.
- **Unattended dispatch.** No cron path to compute, by construction.
