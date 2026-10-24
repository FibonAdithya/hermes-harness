# Hermes for friends and family — Design

Date: 2026-10-24

## Intent

The owner wants to give a handful of friends and family (about 3–6 people) their
own Hermes assistant, set up quickly, without renting a VM per person. Each
friend gets roughly what the owner has: a Telegram chat assistant that triages
their Gmail (drafts only, never sends), manages their calendar, and runs
reminders and briefings. All of them share the owner's existing droplet and the
owner's Nous account.

"Quickly" is the point. Adding a person should be one command for the owner and
two taps for the friend.

## Goals

- `friends add <name>` on the droplet produces a working, isolated assistant for
  one person.
- The friend's whole job: open their bot in Telegram, tap a Google sign-in
  link, approve.
- Each friend's model spend is capped at **$5/month** (owner's decision).
- Friends cannot reach each other's data or the owner's.
- Moving all friends to their own box later is a copy (`hermes profile
  export`/`import`), not a rebuild.

## Non-goals

- Friends get no wiki, no git sync, no broker, no fleet, Talos, gpuq, or
  deploy tools.
- Friends get no shell or admin access to the droplet. The owner administers
  everything.
- No self-service signup. The owner adds every person by hand.
- No per-friend billing. Everything is on the owner's Nous account.

## Anti-goals (would make this a failure even if it works)

- A friend's agent can send email, or can be talked into sending it by an email
  someone sent them.
- A friend's agent picks up the owner's Google token or another friend's.
- The public callback endpoint exposes anything other than the OAuth callback.
- Adding friends degrades the owner's own assistant (memory pressure, a shared
  config change that kills the owner's cron jobs, and so on).
- A runaway friend runs up the owner's Nous bill past the cap without anyone
  being told.

## Constraints and facts (verified 2026-10-24 over `ssh hermes-vm`)

| Fact | How it was checked |
|---|---|
| Droplet: 2 vCPU / 3.9 GB RAM, **no swap**, 69 GB free disk, tz Europe/London | `nproc`, `free -m`, `df -h /`, `timedatectl` |
| Hermes v0.19.0 has profiles: "multiple isolated Hermes instances" | `hermes profile --help` |
| Each profile's sandbox lives under its own `HERMES_HOME/sandboxes` | `tools/environments/base.py:182` `get_sandbox_dir` |
| Gateway units are per profile (`hermes-gateway-<profile>`), unit pins `HERMES_HOME` | `hermes_cli/gateway.py:992,1320` |
| Per-profile `timezone` in `config.yaml` (or `HERMES_TIMEZONE`), used by cron | `hermes_time.py` header; `cron/jobs.py:41` |
| Nous login is shared across profiles via `~/.hermes/shared/nous_auth.json`, kept current on refresh-token rotation | `hermes_cli/auth.py:5070–5095, 6128` |
| Per-profile estimated cost is recorded in that profile's `state.db` (`estimated_cost_usd`) | `hermes_state.py:1272,1332` |
| No per-profile or per-key spending limit exists in Hermes; Nous shows only an org-level monthly cap | `grep` of `hermes_cli/config.py`; `docs/billing-lifecycle.md` |
| `workspace-mcp` 2.1.2 honours `WORKSPACE_MCP_CREDENTIALS_DIR`, `WORKSPACE_MCP_PORT`, `GOOGLE_OAUTH_REDIRECT_URI`, `WORKSPACE_EXTERNAL_URL` | `grep` of its `auth/` and `core/` sources |
| The owner's Google credentials sit in `~/.google_workspace_mcp/credentials/` — **one directory for every process running as `hermes`** | `ls -la` |
| The owner's Google refresh token has worked since ~2026-08-02, so the OAuth app is very likely *In production* (unverified), not *Testing* (which expires tokens after 7 days) | credential file and `oauth_states.json` mtimes. **Inference — confirm in the Google Cloud console.** |
| User `hermes` is in groups `sudo` and `docker` | `id` |
| Owner's gateway + its workspace-mcp ≈ 450 MB RSS (262 + 138 + 45 MB) | `ps -o rss`. MEASURED for the owner's profile; per-friend use is ESTIMATE (unverified) |

Carried-forward lessons that apply directly (from the 07-28 spec's as-built
notes): backend selection must be in `.env` (`TERMINAL_ENV=docker`) and
verified with `id -u` / `/.dockerenv`, not by reading config; `hermes config
set` writes unknown keys world-readable into `config.yaml`; MCP tools are absent
from `hermes -z`, so all tool verification goes through Telegram; omitting
`gmail.send` from scopes does **not** prevent sending, while `--permissions
gmail:drafts` plus `tools.include` does; the cron drift guard silently kills
jobs when the model changes; `TELEGRAM_ALLOWED_USERS` must be the numeric ID and
must never be blank; a `SOUL.md` that quotes injection phrases is blocked by the
injection scanner and silently never loads.

## The design

### 1. One profile per friend, same Linux user

Each friend is a Hermes profile at `~/.hermes/profiles/<name>/` under the
existing `hermes` user. The owner's assistant stays the default profile at
`~/.hermes` and is not modified.

Separate Linux users were considered and rejected: every gateway needs the
Docker socket for its sandbox, and Docker access is root-equivalent, so
separate users would not be a real boundary. The isolation that matters is:

- **Sandbox.** Each friend's terminal/file tools run in their own container
  (`hermes-sandbox:latest`, `--network=none`) that mounts only that profile's
  sandbox directory.
- **Credentials.** Each friend's workspace-mcp gets its own
  `WORKSPACE_MCP_CREDENTIALS_DIR` inside their profile. This is mandatory, not
  hygiene: without it a friend's `--single-user` workspace-mcp reads the shared
  default directory and could act on the owner's Gmail.
- **Tool allowlist.** `tools.include` registers only the Gmail/Calendar tools
  the owner's profile uses (read, search, label, archive, draft, calendar
  read/write) and Hermes' own cron/memory tools. No broker MCP server is
  configured in a friend profile at all.
- **Built-in toolsets.** Friends get Hermes' `web` toolset (search and page
  extract) through the Nous tool gateway, configured as the owner's is
  (`web.backend: firecrawl`, `use_gateway: true`). This was the owner's decision on 2026-10-24.
  They do **not** get `browser`, image generation, delegation/subagents, or code
  execution outside the sandbox. The builder disables those explicitly for the
  `telegram` platform rather than relying on defaults, and verifies by asking
  the agent to list its tools.

  **Accepted risk:** page extract fetches URLs from the host, outside the
  `--network=none` sandbox. An injected email can therefore try to get the agent
  to "look up" an attacker URL with mailbox content in the query string. The
  standing "report, never act on instructions in email" rule is the only
  mitigation, and it is a soft one. The owner accepted this in exchange for web
  search.

**Accepted risk (owner's decision):** friends share a host with the owner's
credentials (Google token, broker's box key, Telegram token). An agent that
escaped its sandbox would land as `hermes`, which has Docker and sudo. The
mitigation is the move-off path in §8, not a boundary on this host.

### 2. The `friends` tool

A small Python CLI in this repo under `friends/`, run by the owner on the
droplet over SSH.

**Correction (2026-10-24, same day): an earlier draft of this section said
"the droplet is deploy-by-hand". That was wrong.** `master` carries
`droplet/harness-pull.sh` and its timer. On the droplet, `systemctl --user
list-timers` shows `harness-pull.timer` active (last run 11:47 BST). It resets
`~/hermes-harness` to `origin/master` every 15 minutes. So `friends/` reaches
the droplet by merging to `master`. `harness-pull.sh` is extended to
`uv sync` the `friends/` package, the way it already does for `broker/`. An
installer, `droplet/install-friends.sh`, installs units without enabling
anything, following `droplet/install-tuner.sh`. Templates changed by a deploy
apply to profiles created afterwards. Existing profiles are not re-rendered
automatically.

Every `hermes` call made for a friend sets `HERMES_HOME=~/.hermes/profiles/<name>`
explicitly. The CLI never relies on `hermes profile use` (the sticky default)
or on the current directory.

Root is needed once, in a bootstrap step the owner runs by hand: install Caddy,
create swap, open 80/443. Per-friend `add`/`remove` must not need an
interactive `sudo`. How Caddy picks up route changes without it (an imported
directory owned by `hermes` plus a reload, or Caddy's localhost admin API) is
the builder's call.

| Command | What it does |
|---|---|
| `friends add <name> --telegram-id <n> --bot-token <t> --tz <IANA> --email <gmail>` | Checks memory headroom (§6). Creates the profile from the repo's template. Writes `.env` (mode 600) and `config.yaml` (mode 600). Allocates a callback port. Adds the Caddy route. Installs and starts `hermes-gateway-<name>`. Prints the callback URL to add to the Google OAuth client. |
| `friends activate <name>` | Run after the friend signs in. Confirms a credential file exists in their credentials dir, drops workspace-mcp from the sign-in tool tier to the steady-state tier, and restarts their gateway. |
| `friends list` | Name, gateway state, signed in yes/no, month-to-date estimated spend vs cap. |
| `friends cap <name> <usd>` | Change one person's cap. Default $5. |
| `friends remove <name>` | Revokes the friend's Google refresh token (Google's revoke endpoint), stops and uninstalls the gateway, removes the Caddy route, and deletes the profile directory. **No archive is kept**: a removed friend's mail-derived sessions and tokens should not outlive them on the owner's box. Tells the owner to delete the bot in BotFather. (Moving friends to another box uses `hermes profile export` separately, §8; that is not `remove`.) |

Onboarding as it really runs: the owner creates the bot in BotFather and gets
the friend's numeric Telegram ID (the friend forwards it from @userinfobot or
similar). After `add`, the friend sends `/start` to their bot, since a bot
cannot message first, and the bot replies with the sign-in link for the
address given in `--email`. No "claim the bot" mode is built.

Inputs are validated before anything is created: name is lowercase
alphanumeric (Hermes' own rule), Telegram ID is numeric and non-empty, the bot
token is not already used by another profile, and the timezone is a valid IANA
name.

### 3. The profile template

Lives in the repo (`friends/template/`). Rendered per friend:

- `config.yaml`: Nous provider. Main and auxiliary models are pinned
  explicitly, never `auto`. **Model choice is the builder's to propose with a
  $/month ESTIMATE at a $5 cap.** The owner's current aux model
  (`deepseek-v4.1-flash`) is the likely candidate. Also sets `timezone: <tz>`
  and the workspace-mcp server entry with its env (§4).
- `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS=<numeric id>`,
  `TERMINAL_ENV=docker`, `TERMINAL_DOCKER_IMAGE=hermes-sandbox:latest`, Google
  OAuth client ID and secret (the owner's existing client; it is shared).
- `SOUL.md`: a short, generic assistant persona. It must not quote injection
  phrases (scanner lesson), and it states that instructions found inside email
  are reported to the user and never acted on.
- Skills: the triage/calendar/capture skill the owner's profile uses, minus
  anything that refers to the wiki, broker, or fleet.
- `memories/MEMORY.md`: seeded with the friend's name and timezone only.

No reminders or briefings are pre-created. The friend asks for them ("brief me
at 8 every morning") and the agent creates the cron job.

### 4. Google sign-in through a public callback

- DNS: an `A` record `auth.fibonadithya.me` pointing at the droplet (the owner
  controls `fibonadithya.me`).
- Caddy on the droplet, with automatic HTTPS. Ports 80 and 443 open; nothing
  else changes in the firewall.
- One route per friend: `/<name>/oauth2callback` reverse-proxies to
  `127.0.0.1:<port>/oauth2callback`, where `<port>` is that friend's
  `WORKSPACE_MCP_PORT`. **Every other path returns 404.**
- Each friend's workspace-mcp gets `GOOGLE_OAUTH_REDIRECT_URI=https://auth.fibonadithya.me/<name>/oauth2callback`,
  plus whatever else workspace-mcp needs for the redirect to match. The builder
  confirms the exact variables by experiment.
- The redirect URI is added by hand to the owner's existing **Web
  application** OAuth client in the Google Cloud console. `friends add` prints
  it. A Web client accepts many redirect URIs.
- The friend's flow: they open their bot and it sends them the sign-in link.
  The `start_google_auth` tool is registered only while the profile is
  unactivated. They approve, click past Google's "unverified app" warning, and
  see workspace-mcp's success page. The owner then runs `friends activate`.
- The owner's own sign-in path (`localhost:8000`) is untouched.

Google limits that apply: an unverified app has a **100-user lifetime cap**,
which is fine at this scale. If the console shows the app is in *Testing*, it
must be moved to *In production* before the first friend, or their tokens die
after 7 days.

### 5. Spending cap

- A `systemd --user` timer (`friends-spend`, every 15 minutes) sums each
  friend profile's `estimated_cost_usd` from their `state.db` for the current
  calendar month (droplet local time).
- **At 80%:** one message to the friend through their own bot ("you've used
  80% of this month's allowance") and one to the owner through the owner's bot.
  Sent once per month per person; state is kept in a small file per profile.
- **At 100%:** message the friend, then stop **and disable**
  `hermes-gateway-<name>`, then message the owner. Disabling is required: the
  unit is `WantedBy=default.target` with `Restart=always`, so a stopped but
  enabled gateway comes back on the next reboot. A stopped gateway also stops
  that friend's cron jobs.
- **Unpriced sessions fail closed.** Hermes' estimate went to `$0`/`NULL` once
  already, during a billing change around 2026-10-17. That is why the tuner
  prices from token counts instead (`tuner/hermes_tuner/policy.py:100`). If a
  friend's month contains a session with tokens but a null or zero
  `estimated_cost_usd`, the timer messages the owner, naming the profile and
  session. It counts that session at a conservative fallback rate. The builder
  picks the rate and labels it ESTIMATE; reusing the tuner's price lookup is
  acceptable.
- **On the 1st of the month,** the timer re-enables and restarts any gateway it stopped for
  cap reasons. A gateway the owner stopped by hand is not restarted.
  `friends cap` raising the limit also restarts it.
- **Runaway trip.** Every friend shares the owner's Nous login, so a looping
  agent could hit shared rate limits long before $5 (the 2026-08-03 incident
  burned 24.4M input tokens in 100 minutes). The same timer also stops a
  friend's gateway if their spend in the trailing 60 minutes exceeds $1, and
  tells the owner. The owner restarts it by hand. The threshold is my call.
- The cap uses Hermes' *estimate*. MEASURED 2026-10-24 on the owner's
  `state.db`: since 2026-10-18 every `telegram` and `cron` session on both Nous
  models has a non-zero `estimated_cost_usd` (35 sessions, $4.73 total). So the
  field is populated for Nous. **Not yet known**, and the builder checks by
  experiment before the cap is trusted: whether auxiliary-model calls
  (compression, titles) and Nous-managed tool calls are inside that figure.
  Actual Nous charges are not checked per profile. Overshoot is bounded by one
  15-minute window plus estimate error.
- The owner's own profile is never capped by this timer.
- **Account-wide backstop:** the owner sets a Nous org-level monthly cap
  on the Nous portal, at a figure of their choosing (decided 2026-10-24).
  Nothing in this build sets or reads it. Hitting it stops the owner's
  assistant too.

### 6. Capacity

- Add a 2 GB swap file (the droplet has none).
- `friends add` refuses to create a profile if `MemAvailable` minus a
  per-friend allowance would fall below a fixed floor. The builder measures the
  allowance from the first real friend profile and sets both numbers. Until
  then use 500 MB per friend and a 768 MB floor (ESTIMATE, unverified).
- When `add` refuses, the answer is to resize the droplet or move friends off
  (§8). The tool does not try to squeeze.

### 7. Things a friend profile must never share with the owner's

- `config.yaml` (so a model change for friends never trips the drift guard on
  the owner's cron jobs, and the reverse).
- The Google credentials directory.
- The Telegram bot and allowlist.
- The sandbox directory and container.

What they do share, deliberately: the Hermes install and venv, the
`hermes-sandbox:latest` image, the Nous login (`~/.hermes/shared/nous_auth.json`),
and the Google OAuth client.

### 8. Moving friends off later

Not built now. The design keeps it cheap: everything per friend lives in the
profile directory plus one Caddy route and one systemd unit, all generated by
`friends`. Moving means a new box with Hermes, Docker and Caddy, then
`hermes profile export`/`import`, re-running the unit/route generation, and
moving the DNS record. A runbook section describing this is in scope.

### 9. Verification

Run with a second Telegram account and a test Google account before inviting
anyone:

1. `friends add test …` succeeds; `friends list` shows the gateway running.
2. In the friend's sandbox, `id -u` is `0` and `/.dockerenv` exists. Listing
   `/root` shows only that profile's files. No owner wiki, no SSH keys.
3. The sign-in link arrives in Telegram, the redirect completes over
   `https://auth.fibonadithya.me/test/oauth2callback`, and the credential file
   lands in the **test profile's** credentials dir. The owner's dir is
   unchanged (check mtimes before and after).
4. `curl` of any other path on `auth.fibonadithya.me` returns 404. A port scan
   shows only 22, 80 and 443.
5. After `activate`: asking the agent to list its tools shows no send tool, no
   `start_google_auth`, and no broker tools. Asking it to send an email fails.
6. Injection test: an email to the test account containing instructions is
   reported, not obeyed. Include one that asks the agent to look up a URL
   containing text from another email. It must be reported, and no request
   for it may show up in the logs of a URL you control.
6a. Web search works from the friend's bot, and the browser tool is absent.
   A web-search-heavy test session shows up in `estimated_cost_usd`. If
   gateway tool charges are not counted there, record that in the runbook as
   spend the cap does not see.
7. A non-allowlisted Telegram account is ignored.
8. A reminder in the friend's timezone fires at the right local time (use a
   timezone different from Europe/London).
9. Cap: set the test cap to $0.01. The 80% and 100% messages arrive, the
   gateway stops, and a manual "1st of month" run restarts it.
10. The owner's assistant still answers and its cron jobs still run after all
    of the above.
11. `friends remove test` leaves no unit, route or profile directory behind.

## Left to the builder

- The exact `tools.include` list for friends, derived from the owner's profile
  minus broker tools.
- The model for friends, presented to the owner with a $/month ESTIMATE before
  it is set.
- How `start_google_auth` is offered before activation. A temporary tool-tier
  bump, as the owner's own setup did, is the known-working path.
- The port allocation scheme for callbacks.
- Where Caddy's config lives and how routes are added and removed safely
  (generated file plus reload is fine).
- Test layout. Unit tests should cover config rendering (no broker server, no
  send tool, credentials dir set, allowlist non-empty), cap arithmetic and
  thresholds (including a $0 cap and an empty `state.db`), month rollover, and
  Caddy route rendering.

## Out of scope

- A separate box for friends (§8 describes the path only).
- Verifying the Google OAuth app with Google.
- Per-friend Nous keys or billing.
- Any friend-facing UI beyond Telegram.
