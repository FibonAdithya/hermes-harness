# Hermes Agent Harness — Design

Date: 2026-07-03
Revised: 2026-07-12 — keep Linux Mint instead of reflashing; add agent disk
hygiene and a self-managed memory wiki.

## Purpose

Turn the current laptop into a dedicated, always-on machine running
[Hermes Agent](https://github.com/NousResearch/hermes-agent). The goal:
message the agent from Telegram to kick off an experiment (mostly ML/data
science work), have it write and execute the code locally, then push results
to GitHub as a branch + PR for review and pull on another machine.

## 1. OS & lifecycle

- **Keep the existing Linux Mint install — do not reflash.** Mint is
  Ubuntu-based (same apt, Docker, systemd, uv, Node), so Hermes' installer
  and toolchain have identical compatibility to Ubuntu Server; the desktop
  environment's idle cost (a few hundred MB RAM, ~0% CPU) is negligible next
  to Docker-based ML work; and keeping Mint avoids an entire
  backup/wipe/restore cycle while staying reversible. (This reverses the
  earlier reflash-to-Ubuntu-Server plan — the compatibility argument for it
  doesn't hold, since Mint already provides the same toolchain.)
- **Disable sleep/suspend for always-on operation.** A laptop desktop
  session will otherwise suspend on lid-close or idle and take the bot down:
  - set `HandleLidSwitch=ignore` and `HandleLidSwitchExternalPower=ignore`
    in `/etc/systemd/logind.conf`
  - `sudo systemctl mask sleep.target suspend.target hibernate.target
    hybrid-sleep.target`
  - disable Cinnamon's screen-blank / automatic-suspend power settings
- **One-time host strip-down (owner, at setup — not the agent).** Remove
  now-unneeded GUI software (Cursor, kdenlive, etc.) and stale personal
  files, paring Mint down to a lean, server-like single-purpose box. This is
  the manual equivalent of the wipe we are no longer doing. Ongoing disk
  hygiene is handled separately (see §5); the agent is never given host-level
  delete/uninstall power.
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
  `.gitconfig` of its own. Because the key also lives on the host, host-side
  maintenance timers (§5) can commit and push as "Hermes Agent" without
  entering the container.
- **Persistent memory (two layers).** The agent both reads and manages its
  own memory, entirely outside the Docker sandbox:
  - *Layer 1 — built-in memory (auto-injected, hard-capped).* Hermes'
    native `~/.hermes/memories/MEMORY.md` (~2,200 char) and `USER.md`
    (~1,375 char) are loaded into the system prompt as a frozen snapshot at
    session start, and the agent edits them with the gateway-level `memory`
    tool (`add`/`replace`/`remove`). This is a host-side tool independent of
    the execution sandbox — no container hole is needed. Because it is
    hard-capped, this layer cannot bloat. `MEMORY.md` is seeded with a
    durable **pointer to the wiki**: where it lives (`/root/hermes-wiki`),
    its entry point (`index.md`), and the tending rules below.
  - *Layer 2 — self-managed LLM wiki (unbounded, on-demand).* A dedicated
    `hermes-wiki` git repo cloned into the sandbox at `/root/hermes-wiki`,
    holding cross-linked markdown pages the agent reads/writes via its
    normal file tools. It is far too large to inject into the prompt, so
    Layer 1 is what makes the agent reliably remember it exists on a cold
    session. Retrieval is `index.md` + `ripgrep` over the pages — no vector
    store or external memory provider, and negligible disk footprint.
    **Tending rules** (taught as a standing skill): one topic per page, add
    an `index.md` line for each page, cross-link related pages with
    `[[…]]`, check for an existing page before creating a new one, and
    delete pages that turn out to be wrong.
- **Workflow convention** — taught to the agent once, as a standing
  instruction/skill, not repeated per message: when asked to run an
  experiment in repo X,
  1. `ripgrep` the wiki `index.md`/pages for related prior work and read
     any relevant pages
  2. clone/pull X (using the `github.com-hermes` remote alias)
  3. create branch `experiment/<slug>`
  4. write and execute the code, iterating as needed
  5. commit as "Hermes Agent" with a results summary in the message
  6. push the branch
  7. `gh pr create` with a description of what ran and the results
  8. record durable lessons/results as `hermes-wiki` pages (the host-side
     timer in §5 commits and pushes the wiki; the agent only edits files)
  9. reply in Telegram with the PR link and a short summary

There is no default/dedicated experiments repo — each message names which
existing repo/directory to work in.

## 3. Data flow

```
Telegram message
  -> Hermes gateway process (MEMORY.md/USER.md injected at session start)
  -> agent turn (consults wiki, plans + runs the experiment)
  -> shell/git tool calls, executed inside the Docker sandbox container
  -> code runs (train/benchmark/etc.) inside the container
  -> commit as "Hermes Agent" via github.com-hermes remote
  -> push branch, gh pr create with results summary
  -> agent writes lessons/results into /root/hermes-wiki pages
  -> Telegram reply with PR link
  -> owner reviews PR on GitHub, pulls locally

(async, host-side timers — §5)
  hermes-wiki dir  -> auto-commit + push as "Hermes Agent"
  host             -> docker system prune + journald cap
```

## 4. Guardrails & error handling

- The agent **never pushes directly to `main`/`master`** for experiment
  repos — always a fresh `experiment/<slug>` branch, always via PR, never
  auto-merged. (The `hermes-wiki` repo is the deliberate exception: it is the
  agent's own notebook, not reviewable experiment code, so its host-side
  timer commits straight to `main`.)
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

## 5. Host maintenance & disk hygiene

Disk bloat is addressed without giving the agent any host-level reach. The
dominant, fast-growing bloat (datasets, checkpoints, caches, cloned repos)
lives inside the sandbox and the agent prunes it itself; slow host-side
growth is handled by OS-owned timers.

- **Agent self-pruning (inside the sandbox).** As a standing instruction,
  the agent reclaims space in its own `/workspace` and `/root` (stale
  datasets, model checkpoints, pip/uv caches, finished experiment repos).
  This is inside its existing boundary — no host access. It must **not**
  delete `/root/hermes-wiki` or `/root/.ssh`.
- **Host janitor (`systemd` timer, OS-owned, not the agent).** Periodically
  runs `docker system prune -f` (dangling images/build cache) and caps
  journald via `SystemMaxUse=` in `/etc/systemd/journald.conf`. Keeps slow
  host growth bounded automatically.
- **Wiki auto-commit (`systemd` timer, OS-owned).** On a ~15-minute
  schedule, snapshots the bind-mounted `hermes-wiki` directory on the host:
  `git add -A`, commit only if there are changes, then push as "Hermes
  Agent" via the `github.com-hermes` alias using the host-resident SSH key.
  Push failures are logged and non-fatal. The agent therefore never has to
  perform git ceremony for the wiki — it only edits files.

## 6. Verification

- **Smoke test** — message the bot a trivial prompt, confirm a reply.
- **End-to-end test** — ask it to run a trivial experiment in a scratch
  repo, confirm a PR appears with the expected branch name and commit
  author.
- **Memory test** — tell the agent a fact worth remembering, confirm it
  writes to `MEMORY.md`/`USER.md`, start a new session, and confirm it
  recalls the fact from the injected snapshot.
- **Wiki test** — ask the agent to record a lesson as a wiki page, confirm
  the page + `index.md` line appear, confirm the host timer commits and
  pushes it to the `hermes-wiki` repo on GitHub, and confirm a later session
  can find it via `ripgrep`.
- **Disk-hygiene test** — leave a dangling Docker image and confirm the
  janitor timer removes it; confirm journald respects the configured cap.
- **Reboot test** — reboot the machine, confirm systemd brings the gateway
  (and the maintenance timers) back up automatically and Telegram responds
  again without manual intervention; confirm the machine does not suspend on
  lid-close.
- **Access control test** — confirm a message from a non-allowlisted
  Telegram account is ignored.

## Out of scope

- Reflashing the OS — superseded by keeping Mint plus the one-time host
  strip-down (§1).
- A dedicated GitHub bot account — declined in favor of a separate SSH key
  + commit identity on the existing account.
- External memory providers (Mem0, Supermemory, etc.) — the built-in memory
  plus the self-managed wiki cover the need without an extra service to run
  or a store that itself grows.
- Giving the agent host-level uninstall/delete power — explicitly rejected;
  host hygiene stays with OS-owned timers so the sandbox boundary holds.
