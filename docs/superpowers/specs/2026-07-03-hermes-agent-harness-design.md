# Hermes Agent Harness — Design

Date: 2026-07-03

## Purpose

Turn the current laptop (being reflashed anyway) into a dedicated, always-on
machine running [Hermes Agent](https://github.com/NousResearch/hermes-agent).
The goal: message the agent from Telegram to kick off an experiment (mostly
ML/data science work), have it write and execute the code locally, then push
results to GitHub as a branch + PR for review and pull on another machine.

## 1. OS & lifecycle

- Reflash to **Ubuntu Server 24.04 LTS**, headless (no desktop environment).
  Chosen over keeping Linux Mint because this machine will only ever be
  reached via Telegram or SSH — a desktop environment is unnecessary
  overhead on a single-purpose box, and Ubuntu Server has the widest
  tooling compatibility (apt, Docker, uv, Node) for Hermes' install script.
- Install Hermes via the official installer script.
- Run `hermes gateway` as a **systemd service** under the user account:
  - `Restart=on-failure` with backoff, so a crash doesn't take the bot down
    permanently.
  - `WantedBy=multi-user.target` so it starts on boot without manual login.

## 2. Components & access control

- **Hermes gateway** — the always-on process handling the Telegram
  conversation and dispatching agent turns.
- **Telegram bot** — created via BotFather, configured with
  `hermes gateway setup`. The gateway is allowlisted to respond only to
  the owner's Telegram user ID — this agent executes code autonomously
  from remote messages, so it must not respond to unknown senders.
- **Model provider** — OpenRouter, starting on a free-tier model. Switchable
  anytime with `hermes model` without reconfiguring anything else.
- **Execution sandbox** — the agent's `terminal`/`execute_code`/file-tool
  calls run inside a single long-lived **Docker container** (`terminal.backend:
  docker`), not directly on the host. The container runs as root internally
  (`docker_run_as_host_user: false`) so it can install anything it needs
  (`apt install`, `rustup`, etc.) without hitting Hermes' dangerous-command
  approval flow — inside a container, that flow is intentionally skipped,
  since the container itself is the security boundary. `container_persistent:
  true` bind-mounts `/workspace` and `/root` from the host
  (`~/.hermes/sandboxes/docker/`), so installed toolchains and cloned repos
  survive across conversations. If the agent ever wrecks its own
  environment, the fix is deleting/recreating the container — the host OS
  is never at risk. The Hermes gateway process itself (Telegram handling,
  the `hermes` CLI) still runs directly on the host under systemd; only the
  agent's own command execution is containerized.
- **Git identity (sandboxed)** — a **new SSH keypair** (e.g.
  `id_ed25519_hermes`) generated on this machine and added as an
  additional authorized key on the existing GitHub account (not a new
  account). A dedicated `~/.ssh/config` host alias (`github.com-hermes`)
  routes git operations through this key explicitly. Git commit identity
  is set separately (e.g. `user.name "Hermes Agent"`,
  `user.email hermes-agent@<domain>`) so every commit/PR the agent makes
  is visibly distinct from the owner's own commits in `git log` and PR
  authorship, even though it shares the same GitHub account's repo access.
  The keypair is generated on the host, then bind-mounted read-only into
  the Docker sandbox (`docker_volumes`) at `/root/.ssh/`, and the commit
  identity is injected via `docker_env` (`GIT_AUTHOR_NAME`/`_EMAIL`,
  `GIT_COMMITTER_NAME`/`_EMAIL`) so the container needs no separate
  `.gitconfig` of its own.
- **Workflow convention** — taught to the agent once, as a standing
  instruction/skill, not repeated per message: when asked to run an
  experiment in repo X,
  1. clone/pull X (using the `github.com-hermes` remote alias)
  2. create branch `experiment/<slug>`
  3. write and execute the code, iterating as needed
  4. commit as "Hermes Agent" with a results summary in the message
  5. push the branch
  6. `gh pr create` with a description of what ran and the results
  7. reply in Telegram with the PR link and a short summary

## 3. Data flow

```
Telegram message
  -> Hermes gateway process
  -> agent turn (plans + runs the experiment)
  -> shell/git tool calls, executed inside the Docker sandbox container
  -> code runs (train/benchmark/etc.) inside the container
  -> commit as "Hermes Agent" via github.com-hermes remote
  -> push branch, gh pr create with results summary
  -> Telegram reply with PR link
  -> owner reviews PR on GitHub, pulls locally
```

There is no default/dedicated experiments repo — each message names which
existing repo/directory to work in.

## 4. Guardrails & error handling

- The agent **never pushes directly to `main`/`master`** — always a fresh
  `experiment/<slug>` branch, always via PR, never auto-merged.
- If a push or PR creation fails (auth, conflicts, missing repo access),
  the agent replies in Telegram with the actual error rather than failing
  silently.
- Telegram allowlist restricts execution triggers to the owner's user ID
  only.
- systemd `Restart=on-failure` keeps the gateway resilient to crashes
  without manual intervention.
- The Docker sandbox means Hermes' own dangerous-command approval layer
  (interactive yes/no in Telegram before risky commands) does **not**
  apply to anything the agent runs — that protection is intentionally
  traded for "can install anything" freedom, on the understanding that the
  container, not command-level approval, is the safety boundary. The host
  filesystem, host `apt`/systemd, and other containers remain out of
  reach regardless of what the agent does inside its own container.

## 5. Verification

- **Smoke test** — message the bot a trivial prompt, confirm a reply.
- **End-to-end test** — ask it to run a trivial experiment in a scratch
  repo, confirm a PR appears with the expected branch name and commit
  author.
- **Reboot test** — reboot the machine, confirm systemd brings the gateway
  back up automatically and Telegram responds again without manual
  intervention.
- **Access control test** — confirm a message from a non-allowlisted
  Telegram account is ignored.

## Out of scope

- A dedicated GitHub bot account — declined in favor of a separate SSH key
  + commit identity on the existing account.
- Restoring the rest of the laptop's personal data (browser profile, email,
  Zotero, etc.) — covered separately by the pre-wipe backup checklist, not
  part of this harness setup.
