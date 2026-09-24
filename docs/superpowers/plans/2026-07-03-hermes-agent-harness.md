# Hermes Agent Harness Implementation Plan

> **SUPERSEDED (2026-07-28)** by `2026-07-28-hermes-assistant-vm.md`. This plan
> builds the laptop-hosted experiment runner, which has been replaced by a
> cloud-VM personal assistant. Kept for the as-built lessons it records.

> **STATUS (2026-07-19): EXECUTED.** The harness is built and verified on the
> Mint box `ramiz` (192.168.1.20). Do not follow this plan as written — parts
> of it are wrong. Read the **As-built corrections** section of the spec
> first. In particular: sandbox config is `.env`/`TERMINAL_ENV`, not
> `config.yaml`; `docker_volumes`/`docker_env` are inert; tools must be baked
> into a custom image because only `/root` and `/workspace` persist; the wiki
> sync runs inside a container, not as the host user; and the one-shot CLI
> flag is `hermes -z --yolo`, not `hermes -p`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn this laptop into an always-on Hermes Agent box, reachable via Telegram, that clones a named repo, runs an ML/data-science experiment, and opens a GitHub PR with the results — using a git identity clearly separate from the owner's own commits, a self-managed knowledge wiki it grows over time, and disk hygiene that keeps it lean without ever handing the agent host-level delete power.

**Architecture:** Keep the existing **Linux Mint** install (not reflashed) with sleep/suspend disabled so it stays always-on. The Hermes Agent gateway runs as a boot-time systemd service. Telegram is the messaging front end, restricted to a single allowlisted owner user ID. OpenRouter provides the model. The agent's own command execution (`terminal`/`execute_code`/file tools) runs inside a persistent, root-capable Docker sandbox container — not on the host — so it can install anything it needs without hitting Hermes' command-approval prompts, while the host OS stays untouched no matter what happens inside. A dedicated SSH keypair + `~/.ssh/config` host alias, bind-mounted into the container, give Hermes its own git identity on top of the owner's existing GitHub account. Memory is two layers: Hermes' built-in hard-capped `MEMORY.md`/`USER.md` (auto-injected at session start, and seeded with a pointer) plus an unbounded `hermes-wiki` git repo of cross-linked markdown the agent reads/writes via file tools. Disk stays lean via agent self-pruning inside the sandbox plus OS-owned systemd timers (docker prune, journald cap, wiki auto-commit) — the agent is never given host-level uninstall/delete power.

**Tech Stack:** Linux Mint (Ubuntu-based), Docker Engine, Hermes Agent (installed via official install script), systemd (system service + user timers), Telegram Bot API, OpenRouter, git + GitHub CLI (`gh`), ripgrep.

## Global Constraints

- OS: **keep the existing Linux Mint install — do not reflash** (per spec §1). Mint is Ubuntu-based, so all `apt`/Docker/systemd steps below apply unchanged.
- Always-on: sleep/suspend/lid-close must be disabled or the gateway dies when the lid closes (per spec §1).
- Messaging: Telegram only, allowlisted to the owner's Telegram user ID only — no open access (per spec §2, and `website/docs/user-guide/security.md` "Best Practices for Production Deployment" #1).
- Model provider: OpenRouter, starting on a free-tier model (per spec §2).
- Execution: inside a persistent Docker sandbox container (`terminal.backend: docker`), running as root (`docker_run_as_host_user: false`) so it can install anything without approval friction — the container, not command-level approval, is the safety boundary (per spec §2).
- Git identity: a **new** SSH keypair + distinct `user.name`/`user.email`, added as an extra key on the owner's **existing** GitHub account — not a new account (per spec §2).
- Git workflow: every experiment is a fresh `experiment/<slug>` branch, ending in a PR. **Never** push directly to `main`/`master` for experiment repos. The `hermes-wiki` repo is the deliberate exception — its host-side timer commits straight to `main` (per spec §4).
- No default experiments repo — every message names which repo/directory to work in (per spec §3).
- Errors (push/PR failures) must be reported back to Telegram, not swallowed (per spec §4).
- Memory: built-in `MEMORY.md`/`USER.md` plus a self-managed `hermes-wiki`; **no external memory provider** (per spec §2, "Out of scope").
- Disk hygiene: the agent may only delete inside its own sandbox; all host-level cleanup is done by OS-owned timers, never by the agent (per spec §5).

---

### Task 1: Prepare the existing Linux Mint box (owner, manual)

This task is physical/manual — it configures the very machine the agent will run on, so the owner does it, not an agent.

**Files:**
- Create: `/etc/systemd/logind.conf.d/hermes-nosuspend.conf`

**Interfaces:**
- Produces: a Linux Mint box that (a) is reachable over SSH, (b) never suspends on lid-close or idle, and (c) has been pared down to a lean single-purpose state. Consumed by every later task.

- [ ] **Step 1: Confirm SSH access is available**

On the Mint box:

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
ip -4 addr show | grep inet
```

From another machine, confirm login and note the LAN IP:

```bash
ssh <username>@<laptop-lan-ip> "cat /etc/os-release | grep PRETTY_NAME"
```

Expected: a line like `PRETTY_NAME="Linux Mint 22 ..."`, confirming SSH works and Mint is the OS.

- [ ] **Step 2: Disable sleep/suspend/hibernate system-wide**

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl status sleep.target | grep -i masked
```

Expected: `Loaded: masked`.

- [ ] **Step 3: Make lid-close a no-op**

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/hermes-nosuspend.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl restart systemd-logind
```

Then, in the Cinnamon GUI once, open **Power Management** and set screen blank / automatic suspend to **Never** (the GUI power manager can override logind on a desktop session).

- [ ] **Step 4: One-time strip-down of GUI/personal cruft**

Remove software this single-purpose box no longer needs (adjust to how each was actually installed — apt, snap, flatpak, or AppImage):

```bash
# examples — verify names on the box first with `apt list --installed | grep -i <name>`
sudo apt remove --purge -y kdenlive || true
flatpak uninstall -y com.kdenlive.kdenlive || true   # if installed via flatpak
# Cursor is commonly an AppImage or a per-user install; remove its files directly, e.g.:
rm -f ~/.local/bin/cursor ~/Applications/cursor*.AppImage 2>/dev/null || true
sudo apt autoremove --purge -y
```

Delete stale personal files the owner no longer wants on an always-on autonomous box (browser profiles, downloads, media). This is owner judgement — there is no scripted list. This step is the manual equivalent of the wipe we are no longer doing.

- [ ] **Step 5: Verify suspend is truly disabled**

```bash
systemctl is-enabled sleep.target suspend.target 2>&1 | sort -u
```

Expected: `masked`. Close the laptop lid for ~30 seconds and confirm from another machine that SSH stays alive:

```bash
ssh <username>@<laptop-lan-ip> "uptime"
```

Expected: a normal uptime line (the box did not suspend while the lid was shut).

---

### Task 2: Base OS preparation

**Files:** none (package installs only)

**Interfaces:**
- Consumes: SSH access from Task 1.
- Produces: `curl`, `git`, `gh`, `ripgrep`, and Docker available on PATH for later tasks.

- [ ] **Step 1: Update the system**

```bash
sudo apt update && sudo apt full-upgrade -y
```

Expected: completes with no errors (reboot if the kernel updated: `sudo reboot`, then reconnect).

- [ ] **Step 2: Install prerequisites**

```bash
sudo apt install -y curl git ca-certificates build-essential ripgrep
```

Expected: `git --version`, `curl --version`, and `rg --version` all print version strings.

- [ ] **Step 3: Install GitHub CLI**

```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install gh -y
```

Expected: `gh --version` prints a version string.

- [ ] **Step 4: Install Docker Engine**

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in (or `newgrp docker`) so the group membership takes effect, then verify:

```bash
docker version
docker run --rm hello-world
```

Expected: `docker version` prints both Client and Server sections with no errors, and `hello-world` prints its "Hello from Docker!" message.

- [ ] **Step 5: Enable lingering for the service account**

User-level systemd timers (wiki sync, docker prune — Tasks 12-13) must run without an active login session:

```bash
sudo loginctl enable-linger $USER
loginctl show-user $USER | grep Linger
```

Expected: `Linger=yes`.

- [ ] **Step 6: Authenticate the GitHub CLI**

```bash
gh auth login
gh auth status
```

Choose GitHub.com, HTTPS or SSH as preferred (this authenticates `gh` for `gh repo create`/`gh pr create`; the separate Hermes push identity is set up in Task 6).

Expected: `gh auth status` reports logged in as the owner's account.

---

### Task 3: Install Hermes Agent

**Files:**
- Creates: `~/.hermes/` (config home), shell profile update in `~/.bashrc`

**Interfaces:**
- Consumes: `curl`, `git` from Task 2.
- Produces: `hermes` CLI on PATH, used by every subsequent task.

- [ ] **Step 1: Run the installer**

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc
```

- [ ] **Step 2: Verify the install**

```bash
hermes --version
hermes doctor
```

Expected: a version string, and `hermes doctor` reports no blocking issues (advisory warnings about optional/lazy deps are fine — those aren't installed yet).

---

### Task 4: Configure OpenRouter as the model provider

**Files:**
- Modifies: `~/.hermes/.env` (adds `OPENROUTER_API_KEY`)
- Modifies: `~/.hermes/config.yaml` (sets `model`)

**Interfaces:**
- Consumes: `hermes` CLI from Task 3.
- Produces: a configured `model:` value all later agent turns use.

- [ ] **Step 1: Set the OpenRouter API key**

Obtain a key from https://openrouter.ai/keys, then:

```bash
hermes config set OPENROUTER_API_KEY sk-or-v1-...
```

- [ ] **Step 2: Pick a free-tier model interactively**

```bash
hermes model
```

Select OpenRouter, then pick a model tagged free-tier (e.g. one of the `:free` suffixed models OpenRouter lists — the exact free catalog changes over time, so pick whichever is current in the interactive list).

- [ ] **Step 3: Verify the model responds**

```bash
hermes -p "reply with exactly: pong"
```

Expected: output containing `pong`.

---

### Task 5: Enable free web search (DDGS) and free-tier extract (Firecrawl)

**Files:**
- Modifies: `~/.hermes/.env` (adds `FIRECRAWL_API_KEY`)
- Modifies: `~/.hermes/config.yaml` (sets `web.search_backend` and `web.extract_backend`)

**Interfaces:**
- Consumes: `hermes` CLI from Task 3.
- Produces: a working `web_search` tool (DDGS, no key, no cost) and a working `web_extract` tool (Firecrawl free tier, 500 credits/month) the agent can use mid-experiment.

- [ ] **Step 1: Get a Firecrawl API key**

Sign up at https://firecrawl.dev and copy an API key from the dashboard (free tier: 500 credits/month, no card required).

- [ ] **Step 2: Set the Firecrawl key**

```bash
hermes config set FIRECRAWL_API_KEY fc-your-key-here
```

- [ ] **Step 3: Split search and extract to different backends**

```bash
cat >> ~/.hermes/config.yaml <<'EOF'
web:
  search_backend: "ddgs"
  extract_backend: "firecrawl"
EOF
```

- [ ] **Step 4: Install the DDGS package if it isn't lazy-installed automatically**

```bash
pip install ddgs
```

(Hermes lazy-installs this into its own venv on first use if `security.allow_lazy_installs` is left at its default of `true` — this step is just a fallback.)

- [ ] **Step 5: Verify web search works**

```bash
hermes -p "Search the web for the current version of Ubuntu Server LTS and tell me what it is."
```

Expected: a response naming a real, current Ubuntu LTS version, showing the `web_search` tool was actually invoked.

- [ ] **Step 6: Verify web extract works**

```bash
hermes -p "Use web_extract to fetch https://ubuntu.com/server and summarize what the page says in 2 sentences."
```

Expected: a 2-sentence summary reflecting actual page content, confirming Firecrawl extract is wired up.

---

### Task 6: Generate Hermes' own git identity

**Files:**
- Creates: `~/.ssh/id_ed25519_hermes`, `~/.ssh/id_ed25519_hermes.pub`
- Modifies: `~/.ssh/config` (adds a `github.com-hermes` host block)
- Modifies: `~/.gitconfig` (sets a global `[user]` name/email)

**Interfaces:**
- Produces: the `github.com-hermes` SSH host alias and `Hermes Agent <hermes-agent@...>` commit identity that Task 8 verifies, Task 9's wiki clone uses, and Task 11's SOUL.md references.

- [ ] **Step 1: Generate the keypair**

```bash
ssh-keygen -t ed25519 -C "hermes-agent@<owner-domain>" -f ~/.ssh/id_ed25519_hermes -N ""
cat ~/.ssh/id_ed25519_hermes.pub
```

- [ ] **Step 2: Add the public key to the owner's GitHub account**

Go to https://github.com/settings/keys → "New SSH key" → paste the contents from Step 1. Title it "hermes-agent-harness".

- [ ] **Step 3: Add the host alias**

```bash
cat >> ~/.ssh/config <<'EOF'

Host github.com-hermes
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_hermes
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
```

- [ ] **Step 4: Set the Hermes commit identity globally**

Because this box is single-purpose for Hermes, a global git identity is fine (no other git user commits here). This identity is also what the host-side wiki auto-commit timer (Task 13) uses:

```bash
git config --global user.name "Hermes Agent"
git config --global user.email "hermes-agent@<owner-domain>"
```

- [ ] **Step 5: Verify SSH auth works through the new alias**

```bash
ssh -T git@github.com-hermes
```

Expected: `Hi <owner-github-username>! You've successfully authenticated, but GitHub does not provide shell access.`

---

### Task 7: Configure the Docker sandbox backend

**Files:**
- Modifies: `~/.hermes/config.yaml` (adds the `terminal:` block)
- Creates: `~/hermes-wiki-data/` (empty host dir; the wiki repo is cloned into it in Task 9)

**Interfaces:**
- Consumes: Docker Engine from Task 2, the `~/.ssh` directory (key + `github.com-hermes` alias) and git identity from Task 6.
- Produces: every future `terminal`/`execute_code`/file-tool call runs inside this container, with root access, the mounted git identity at `/root/.ssh`, the wiki at `/root/hermes-wiki`, and a persistent filesystem.

- [ ] **Step 1: Create the wiki host directory (mount target)**

```bash
mkdir -p ~/hermes-wiki-data
```

- [ ] **Step 2: Set the terminal backend and write the block**

```bash
hermes config set terminal.backend docker
cat >> ~/.hermes/config.yaml <<EOF
terminal:
  backend: docker
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  docker_run_as_host_user: false
  container_persistent: true
  docker_persist_across_processes: true
  container_cpu: 2
  container_memory: 6144
  container_disk: 51200
  docker_volumes:
    - "$HOME/.ssh:/root/.ssh:ro"
    - "$HOME/hermes-wiki-data:/root/hermes-wiki"
  docker_env:
    GIT_AUTHOR_NAME: "Hermes Agent"
    GIT_AUTHOR_EMAIL: "hermes-agent@<owner-domain>"
    GIT_COMMITTER_NAME: "Hermes Agent"
    GIT_COMMITTER_EMAIL: "hermes-agent@<owner-domain>"
EOF
```

`nikolaik/python-nodejs` is a reasonable ML/data-science base (Python 3.11 + Node 20 + pip); since the container runs as root the agent can `apt install`/`rustup` anything else on top. The `.ssh` mount is read-only; the `hermes-wiki` mount is read-write and points at the explicit host dir from Step 1 (so the Task 13 timer and the agent share one known path, independent of Hermes' internal persistence layout). The `GIT_*` env vars replace an in-container `.gitconfig`.

- [ ] **Step 3: Verify the sandbox is active and identity + wiki mount are reachable**

```bash
hermes -p "Run: whoami && ls -ld /root/hermes-wiki && ssh -T git@github.com-hermes ; echo done"
```

Expected: `whoami` prints `root`, `/root/hermes-wiki` exists, and the SSH command prints `Hi <owner-github-username>! ...`, confirming the mounted key and wiki directory both work from inside the sandbox.

- [ ] **Step 4: Verify persistence across a session reset**

```bash
hermes -p "Run: touch /workspace/persistence-check.txt"
hermes -p "/new"
hermes -p "Run: ls /workspace/persistence-check.txt"
```

Expected: the file still exists after `/new`, confirming `container_persistent: true`.

- [ ] **Step 5: Install ripgrep inside the persistent container**

```bash
hermes -p "Run: apt-get update && apt-get install -y ripgrep && rg --version"
```

Expected: `rg` version prints. Because the container is persistent, this survives future sessions and gives the agent fast wiki search.

---

### Task 8: Verify the git identity end-to-end with a scratch repo

**Files:** none permanent — uses a throwaway repo

**Interfaces:**
- Consumes: `github.com-hermes` alias and commit identity from Task 6.
- Produces: confirmation that clone → branch → commit → push → PR works before wiring it into standing instructions.

- [ ] **Step 1: Create a scratch repo on GitHub**

```bash
gh repo create hermes-harness-smoketest --private --clone
cd hermes-harness-smoketest
git remote set-url origin git@github.com-hermes:$(gh api user -q .login)/hermes-harness-smoketest.git
```

- [ ] **Step 2: Make a commit via the Hermes identity**

```bash
echo "smoke test" > README.md
git add README.md
git commit -m "chore: smoke test commit"
git log -1 --format='%an <%ae>'
```

Expected: `Hermes Agent <hermes-agent@<owner-domain>>`.

- [ ] **Step 3: Push a branch and open a PR**

```bash
git checkout -b experiment/smoketest
git push -u origin experiment/smoketest
gh pr create --title "Smoke test" --body "Verifying the dedicated Hermes git identity works end to end." --base main
```

Expected: a PR URL is printed; on GitHub the commit author shows "Hermes Agent".

- [ ] **Step 4: Clean up**

```bash
cd ~
gh repo delete hermes-harness-smoketest --yes
```

---

### Task 9: Create, seed, and clone the hermes-wiki repo

**Files:**
- Creates: the `hermes-wiki` GitHub repo, `~/hermes-wiki-data/` working clone, `~/hermes-wiki-data/index.md`, `~/hermes-wiki-data/CONVENTIONS.md`

**Interfaces:**
- Consumes: `gh` auth (Task 2), the `github.com-hermes` alias and commit identity (Task 6), and the `~/hermes-wiki-data` mount target (Task 7).
- Produces: a git-backed wiki visible to the agent at `/root/hermes-wiki` and pushable by the Task 13 timer. Referenced by the MEMORY.md pointer (Task 10) and SOUL.md workflow (Task 11).

- [ ] **Step 1: Create the private repo**

```bash
gh repo create hermes-wiki --private
```

Expected: prints the new repo URL.

- [ ] **Step 2: Initialize the clone in the mounted host directory**

The directory already exists (Task 7) and is empty, so initialize in place and point `origin` at the Hermes alias:

```bash
cd ~/hermes-wiki-data
git init -b main
git remote add origin git@github.com-hermes:$(gh api user -q .login)/hermes-wiki.git
```

- [ ] **Step 3: Seed the entry point and conventions**

```bash
cat > ~/hermes-wiki-data/index.md <<'EOF'
# Hermes Wiki — Index

This is the agent's knowledge base: cross-linked notes on experiments,
techniques, environment quirks, and lessons learned. Every page has one
line here. Search with `rg "<term>" /root/hermes-wiki` before assuming a
topic is undocumented.

## Pages
(none yet — add one line per page as pages are created)
EOF

cat > ~/hermes-wiki-data/CONVENTIONS.md <<'EOF'
# Wiki conventions

- One topic per page. Filenames are short and kebab-case, e.g.
  `lora-finetuning-notes.md`.
- Every page gets a one-line entry under "## Pages" in `index.md`.
- Cross-link related pages with `[[page-name]]` (no `.md`).
- Before creating a page, `rg` the wiki to check it doesn't already
  exist — extend the existing page instead of duplicating.
- Record real, measured results and concrete gotchas, not restated theory.
- Delete or correct pages that turn out to be wrong.
- Only ever edit files here. A background timer commits and pushes the
  wiki; never run git inside this repo yourself.
EOF
```

- [ ] **Step 4: Commit and push the seed**

```bash
cd ~/hermes-wiki-data
git add -A
git commit -m "chore: seed wiki index and conventions"
git push -u origin main
```

Expected: push succeeds; the repo on GitHub shows `index.md` and `CONVENTIONS.md` authored by "Hermes Agent".

- [ ] **Step 5: Confirm the agent sees the wiki through the mount**

```bash
hermes -p "Run: rg -n 'Index' /root/hermes-wiki/index.md"
```

Expected: a match from `/root/hermes-wiki/index.md`, confirming the host clone is visible inside the sandbox at the wiki path.

---

### Task 10: Seed the built-in memory (pointer + user profile)

**Files:**
- Creates/Modifies: `~/.hermes/memories/MEMORY.md`, `~/.hermes/memories/USER.md`

**Interfaces:**
- Consumes: the wiki path/conventions from Task 9.
- Produces: a hard-capped, auto-injected front page that reliably reminds the agent the wiki exists and how the owner works. The agent maintains these files thereafter via its `memory` tool.

- [ ] **Step 1: Write the MEMORY.md pointer (must stay under ~2,200 chars)**

```bash
mkdir -p ~/.hermes/memories
cat > ~/.hermes/memories/MEMORY.md <<'EOF'
# Agent memory

## Knowledge wiki
I keep a personal knowledge wiki (a git repo) mounted at /root/hermes-wiki.
- Entry point: /root/hermes-wiki/index.md — a table of contents linking every page.
- Search it with: rg "<term>" /root/hermes-wiki  (then read the matching page).
- Before starting any experiment, search the wiki for related prior work and read relevant pages.
- After finishing, record durable lessons/results as wiki pages.

Tending rules (full copy in /root/hermes-wiki/CONVENTIONS.md):
- One topic per page; short kebab-case filenames (e.g. lora-finetuning-notes.md).
- Maintain a one-line entry in index.md for every page.
- Cross-link related pages with [[page-name]].
- rg first to avoid duplicate pages — extend the existing page instead.
- Delete or correct pages that turn out to be wrong.

I only edit wiki files. A background timer commits and pushes them, so I never run git inside the wiki repo myself.

## Disk hygiene
Free space by deleting inside my sandbox only: stale datasets, model
checkpoints, pip/uv caches, and finished experiment clones under /workspace
and /root. Never delete /root/hermes-wiki or /root/.ssh.
EOF
```

- [ ] **Step 2: Write the USER.md profile (must stay under ~1,375 chars)**

```bash
cat > ~/.hermes/memories/USER.md <<'EOF'
# User profile

- Owner is the sole authorized operator, reachable via Telegram.
- Keep replies concise: a 2-4 sentence summary plus the PR link. Put detail
  in the PR body, not the chat.
- Work is mostly ML / data-science experiments in named GitHub repos.
- The owner reviews every experiment as a PR before merging — never
  auto-merge, never push to main/master on experiment repos.
- Report real measured results, including failures and actual error text —
  never a guess at what the output would be.
EOF
```

- [ ] **Step 3: Verify both are injected at session start**

```bash
hermes -p "/new"
hermes -p "Without running any tools, answer from memory: where is your knowledge wiki, and what is the owner's preferred reply length?"
```

Expected: the reply names `/root/hermes-wiki` and the "2-4 sentence summary + PR link" preference, confirming both memory files loaded into the system prompt.

---

### Task 11: Author the standing experiment workflow in SOUL.md

**Files:**
- Modifies: `~/.hermes/SOUL.md`

**Interfaces:**
- Consumes: the branch-naming/PR-only convention (Global Constraints), the wiki (Task 9), and the bundled `github-auth`/`github-pr-workflow` Hermes skills.
- Produces: the standing instruction every "run an experiment" message relies on, so it never needs restating.

- [ ] **Step 1: Append the workflow section**

```bash
cat >> ~/.hermes/SOUL.md <<'EOF'

## Experiment workflow (standing instruction)

When asked to run an experiment in a named repo:

1. Consult the wiki first: `rg "<relevant terms>" /root/hermes-wiki` and read
   any matching pages before designing the experiment. Check index.md.
2. Clone or `cd` into the repo. Use the `github.com-hermes` remote alias for
   any remote you add or rewrite (see `~/.ssh/config`), so pushes go out
   under the Hermes Agent identity.
3. `git fetch origin && git checkout main && git pull origin main`
4. Create a new branch `experiment/<short-slug>`. Never commit directly to
   `main`/`master` on an experiment repo.
5. Do the work: write the code, run it, iterate. Capture the actual
   metrics/output, not a guess.
6. Commit with a message summarizing what ran and the headline results.
7. Push the branch: `git push -u origin HEAD`.
8. Open a PR with `gh pr create`, results summary in the body (see the
   github-pr-workflow skill for the format).
9. Record durable lessons/results as pages under `/root/hermes-wiki`
   following the tending rules in memory (one topic per page, add an
   index.md line, cross-link, rg first to avoid duplicates). Only edit the
   files — a background timer commits and pushes the wiki.
10. Reply in the chat with the PR link and a 2-4 sentence summary.

If a push or PR step fails (auth, conflicts, no repo access), report the
actual error back in the chat — do not retry silently or claim success.

## Disk hygiene (standing instruction)

Keep your own sandbox lean: after an experiment, delete stale datasets,
model checkpoints, pip/uv caches, and finished experiment clones under
`/workspace` and `/root`. Never delete `/root/hermes-wiki` or `/root/.ssh`.
Host-level cleanup is handled automatically outside your sandbox — you do
not manage it.
EOF
```

- [ ] **Step 2: Verify SOUL.md loads**

```bash
hermes -p "What are your standing instructions for running an experiment? Summarize in two sentences, and say what you must never delete."
```

Expected: a reply referencing consulting the wiki, branch + PR, recording wiki pages, and never deleting `/root/hermes-wiki` or `/root/.ssh`.

---

### Task 12: Install the host janitor (docker prune timer + journald cap)

**Files:**
- Creates: `/etc/systemd/journald.conf.d/hermes-cap.conf`, `~/.config/systemd/user/hermes-docker-prune.service`, `~/.config/systemd/user/hermes-docker-prune.timer`

**Interfaces:**
- Consumes: Docker + lingering from Task 2.
- Produces: OS-owned, agent-independent cleanup of dangling Docker images/build cache and a bounded journal. Verified in Task 18.

- [ ] **Step 1: Cap the systemd journal**

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/hermes-cap.conf >/dev/null <<'EOF'
[Journal]
SystemMaxUse=500M
EOF
sudo systemctl restart systemd-journald
journalctl --disk-usage
```

Expected: journal disk usage is reported and will stay at/under ~500M going forward.

- [ ] **Step 2: Create the docker-prune user service and timer**

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/hermes-docker-prune.service <<'EOF'
[Unit]
Description=Prune dangling Docker images and build cache

[Service]
Type=oneshot
ExecStart=/usr/bin/docker system prune -f
EOF

cat > ~/.config/systemd/user/hermes-docker-prune.timer <<'EOF'
[Unit]
Description=Daily Docker prune

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF
```

- [ ] **Step 3: Enable the timer**

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-docker-prune.timer
systemctl --user list-timers hermes-docker-prune.timer
```

Expected: the timer is listed with a NEXT run time.

- [ ] **Step 4: Confirm the prune service runs cleanly on demand**

```bash
systemctl --user start hermes-docker-prune.service
journalctl --user -u hermes-docker-prune.service -n 20 --no-pager
```

Expected: the service ran and exited `status=0/SUCCESS` (a "Total reclaimed space" line from docker is normal).

---

### Task 13: Install the wiki auto-commit timer

**Files:**
- Creates: `~/.local/bin/hermes-wiki-sync.sh`, `~/.config/systemd/user/hermes-wiki-sync.service`, `~/.config/systemd/user/hermes-wiki-sync.timer`

**Interfaces:**
- Consumes: the `~/hermes-wiki-data` clone + `origin` remote (Task 9), the Hermes commit identity and `github.com-hermes` alias (Task 6), lingering (Task 2).
- Produces: the wiki is committed and pushed to `main` on a schedule with no agent involvement. Verified in Task 17.

- [ ] **Step 1: Write the sync script**

```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/hermes-wiki-sync.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/hermes-wiki-data" || exit 0
git add -A
# nothing staged? exit quietly.
if git diff --cached --quiet; then
  exit 0
fi
git commit -m "wiki: auto-snapshot $(date -Iseconds)"
git push origin HEAD:main
EOF
chmod +x ~/.local/bin/hermes-wiki-sync.sh
```

The commit identity comes from the global git config set in Task 6; the push goes through the `github.com-hermes` alias baked into `origin` in Task 9.

- [ ] **Step 2: Create the service and timer**

```bash
cat > ~/.config/systemd/user/hermes-wiki-sync.service <<'EOF'
[Unit]
Description=Commit and push the Hermes wiki

[Service]
Type=oneshot
ExecStart=%h/.local/bin/hermes-wiki-sync.sh
EOF

cat > ~/.config/systemd/user/hermes-wiki-sync.timer <<'EOF'
[Unit]
Description=Sync the Hermes wiki every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
EOF
```

- [ ] **Step 3: Enable the timer**

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-wiki-sync.timer
systemctl --user list-timers hermes-wiki-sync.timer
```

Expected: the timer is listed with a NEXT run time.

- [ ] **Step 4: Prove a change round-trips with no manual git**

```bash
echo "- test entry $(date -Iseconds)" >> ~/hermes-wiki-data/index.md
systemctl --user start hermes-wiki-sync.service
journalctl --user -u hermes-wiki-sync.service -n 20 --no-pager
git -C ~/hermes-wiki-data log -1 --format='%an: %s'
```

Expected: service exits success, and the latest commit is `Hermes Agent: wiki: auto-snapshot ...`. Confirm on GitHub that the push landed on `main`. (Clean up the test line afterward: remove it from `index.md`; the next run commits the removal.)

---

### Task 14: Set up the Telegram bot and gateway with an owner-only allowlist

**Files:**
- Modifies: `~/.hermes/.env` (adds `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`)
- Modifies: `~/.hermes/config.yaml` (gateway platform config)

**Interfaces:**
- Consumes: `hermes` CLI from Task 3.
- Produces: a Telegram bot that only responds to the owner's Telegram user ID (per `website/docs/user-guide/security.md`, "User Authorization (Gateway)").

- [ ] **Step 1: Create the bot via BotFather**

In Telegram, message **@BotFather**: `/newbot`. Follow the prompts for display name and username (must end in `bot`). BotFather replies with a token like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`.

- [ ] **Step 2: Find your own Telegram user ID**

Message **@userinfobot** (or **@RawDataBot**) — it replies with your numeric user ID.

- [ ] **Step 3: Configure the gateway**

```bash
hermes gateway setup
```

Choose Telegram, paste the bot token when prompted.

- [ ] **Step 4: Restrict access to the owner only**

```bash
hermes config set TELEGRAM_ALLOWED_USERS <your-telegram-user-id>
```

- [ ] **Step 5: Verify the allowlist is active**

```bash
hermes gateway
```

Run in the foreground temporarily. Message the bot from your own Telegram account — expect a normal reply. Message from a second account — expect no response. Then `Ctrl+C` to stop before Task 15.

---

### Task 15: Install the gateway as a boot-time systemd service

**Files:**
- Creates: the system-level `hermes-gateway` unit (via `hermes gateway install --system`)

**Interfaces:**
- Consumes: the configured gateway from Task 14.
- Produces: an always-on gateway process that survives reboots and crashes.

- [ ] **Step 1: Install the service**

```bash
sudo hermes gateway install --system
```

- [ ] **Step 2: Start it and check status**

```bash
sudo hermes gateway start
hermes gateway status --system
```

Expected: status shows the service active/running.

- [ ] **Step 3: Confirm it's reachable**

Message the bot from Telegram again. Expect a reply within a few seconds.

- [ ] **Step 4: Commit a non-secret config snapshot to the harness repo**

```bash
cd ~/hermes-harness
mkdir -p config
cp ~/.hermes/config.yaml config/config.yaml.example
git add config/config.yaml.example
git commit -m "docs: snapshot non-secret Hermes config for reference"
```

Double-check `config/config.yaml.example` contains no API keys before committing (secrets live only in `~/.hermes/.env`, untouched here).

---

### Task 16: Reboot + no-suspend verification

**Files:** none

**Interfaces:**
- Consumes: the systemd service from Task 15 and the suspend masks from Task 1.
- Produces: confirmation the whole stack survives a real reboot and the box never suspends.

- [ ] **Step 1: Reboot**

```bash
sudo reboot
```

- [ ] **Step 2: Check gateway + timers came back**

```bash
ssh <username>@<laptop-lan-ip> "hermes gateway status --system && systemctl --user list-timers 'hermes-*' --no-pager"
```

Expected: gateway active/running, and both `hermes-docker-prune.timer` and `hermes-wiki-sync.timer` listed — all with no manual intervention.

- [ ] **Step 3: Confirm Telegram responds and the box won't suspend**

Message the bot (expect a reply). Then:

```bash
ssh <username>@<laptop-lan-ip> "systemctl is-enabled sleep.target suspend.target 2>&1 | sort -u"
```

Expected: `masked`.

---

### Task 17: Memory + wiki end-to-end verification

**Files:** none permanent

**Interfaces:**
- Consumes: memory (Task 10), the wiki (Task 9), and the sync timer (Task 13).
- Produces: proof the agent recalls memory across sessions and that a wiki page it writes is committed, pushed, and later findable.

- [ ] **Step 1: Ask the agent to record a lesson as a wiki page**

Via Telegram:
```
Create a wiki page called env-cuda-notes documenting that this box has no
GPU, so experiments must run on CPU. Add it to the index and cross-link it.
```

- [ ] **Step 2: Confirm the page + index entry exist and were pushed**

```bash
ls ~/hermes-wiki-data/env-cuda-notes.md
grep -n "env-cuda-notes" ~/hermes-wiki-data/index.md
systemctl --user start hermes-wiki-sync.service   # force a sync rather than wait
git -C ~/hermes-wiki-data log -1 --format='%an: %s'
```

Expected: the page exists, `index.md` links it, and the latest commit is by `Hermes Agent`. Confirm on GitHub the page is on `main`.

- [ ] **Step 3: Confirm recall in a fresh session**

Via Telegram:
```
/new
Does this box have a GPU? Check the wiki before answering.
```

Expected: the agent `rg`s the wiki, finds `env-cuda-notes`, and answers "no GPU, CPU only" — proving the wiki round-trips as durable memory.

---

### Task 18: Disk-hygiene verification

**Files:** none

**Interfaces:**
- Consumes: the janitor timer (Task 12) and the agent self-prune instruction (Task 11).
- Produces: confirmation host junk is reclaimed automatically and the agent prunes its own sandbox without touching protected paths.

- [ ] **Step 1: Confirm the janitor removes a dangling image**

```bash
docker pull alpine:3.19
docker rmi alpine:3.19 || true          # leaves layers dangling if referenced elsewhere
docker image prune --dry-run 2>/dev/null; docker images -f dangling=true
systemctl --user start hermes-docker-prune.service
docker images -f dangling=true
```

Expected: after the service runs, the dangling-images list is empty (or smaller), confirming the prune works.

- [ ] **Step 2: Confirm the agent prunes its sandbox but protects the wiki/keys**

Via Telegram:
```
Free up disk in your sandbox: clear pip/uv caches and any stale files under
/workspace. Then confirm /root/hermes-wiki and /root/.ssh are untouched.
```

Then verify from the host:
```bash
ls ~/hermes-wiki-data/index.md && ls -l ~/.ssh/id_ed25519_hermes
```

Expected: the agent reports freeing cache space; both the wiki index and the SSH key still exist — the agent respected the protected paths.

---

### Task 19: End-to-end experiment test

**Files:** none permanent — uses a throwaway repo

**Interfaces:**
- Consumes: everything from Tasks 6-15.
- Produces: proof the full "message → experiment → PR" loop works.

- [ ] **Step 1: Create a trivial scratch repo**

```bash
gh repo create hermes-e2e-test --private --clone
cd hermes-e2e-test
echo "# E2E test repo" > README.md
git add README.md && git commit -m "chore: init"
git push -u origin main
```

- [ ] **Step 2: Message the bot to run a trivial experiment**

Send via Telegram:
```
Run an experiment in hermes-e2e-test: generate 100 random points, fit a
linear regression, and report the R^2 score. Push it as a PR.
```

- [ ] **Step 3: Verify the result**

```bash
gh pr list --repo <owner>/hermes-e2e-test
gh pr view <pr-number> --repo <owner>/hermes-e2e-test --json author,headRefName
```

Expected: a PR exists, `headRefName` starts with `experiment/`, and the branch commits show `Hermes Agent` as author. Confirm the Telegram reply included the PR link and a results summary.

- [ ] **Step 4: Clean up**

```bash
cd ~
gh repo delete hermes-e2e-test --yes
```

---

### Task 20: Access-control verification test

**Files:** none

**Interfaces:**
- Consumes: the `TELEGRAM_ALLOWED_USERS` allowlist from Task 14.
- Produces: confirmation the box can't be triggered by strangers.

- [ ] **Step 1: Message the bot from a non-allowlisted account**

Using a second Telegram account (or a friend), send any message to the bot.

- [ ] **Step 2: Verify no response and check logs**

```bash
journalctl -u hermes-gateway -n 50 --no-pager | grep -i denied
```

Expected: the unauthorized message produced no reply, and the gateway log shows a denial/unauthorized entry for that user ID.
