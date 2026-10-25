# Hermes for Friends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `friends` CLI on the droplet that gives one person an isolated Hermes assistant (own bot,
own sandbox, own Google sign-in, $5/month cap) with one owner command and a two-step friend flow.

**Architecture:** A new Python package `friends/hermes_friends/` in this repo, deployed by the
existing `harness-pull` timer. Each friend is a Hermes profile at `~/.hermes/profiles/<name>/`,
discovered by the presence of `friend.json` in it (no separate registry). Pure modules render
files and decide spend actions. One `System` class performs every side effect, so tests swap in a
fake. Caddy serves only per-friend OAuth callback paths on `auth.fibonadithya.me`.

**Tech Stack:** Python ≥3.11 stdlib only (`zoneinfo`, `sqlite3`, `json`, `argparse`, `urllib`),
plus PyYAML for `config.yaml`. pytest. uv. systemd `--user` units. Caddy 2. bash for droplet
scripts (shellcheck clean).

**Spec:** `docs/ai/specs/2026-10-24-hermes-for-friends-design.md`. Read it first. Section numbers
below (§N) refer to it.

## Global Constraints

- Hermes source is never modified. Hermes is driven only through its CLI (`hermes …`) with
  `HERMES_HOME=/home/hermes/.hermes/profiles/<name>` set explicitly on **every** call (§2).
- The owner's default profile (`~/.hermes`) is read-only to this package, except `hermes send` to
  message the owner.
- Friend `config.yaml` and `.env` are written mode `600`. Friend profile dirs are `700`.
- `TELEGRAM_ALLOWED_USERS` is a single positive integer and never blank (07-28 lesson 6).
- Friend profiles never contain an MCP server other than `google`. In particular, no `compute`
  server.
- Cap default `$5.00`. Warn at `80%`. Runaway trip at `> $1.00` in a trailing 60 minutes. Spend
  timer every 15 minutes (§5).
- Public host `auth.fibonadithya.me`. Callback path `/<name>/oauth2callback`. Every other path → 404 (§4).
- Every dollar figure shown to a person is labelled `ESTIMATE`.
- No mail-derived or owner-personal content goes into the repo. The friend skill is written from
  scratch, not copied from the owner's `gmail-management` skill.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage
  explicit paths only.
- Task 7 edits `.github/workflows/ci.yml`. Push that branch over **SSH**; the HTTPS token lacks
  `workflow` scope.

## Owner prerequisites (block Task 0 and Task 8)

1. A **test** Telegram bot from BotFather, and a second Telegram account's numeric ID.
2. A **test** Google account.
3. A DNS `A` record `auth.fibonadithya.me` → the droplet's public IPv4.
4. In Google Cloud console: confirm the OAuth app's publishing status is **In production**
   (§4, inference only so far), and be ready to add redirect URIs to the existing Web client.

## Review Focus

1. **Session cost that is still accumulating.** A long Telegram session's `estimated_cost_usd`
   grows after `started_at`. Runaway detection therefore uses the month total's growth between
   timer samples, never "sessions started in the last hour". Pinned in Task 4
   (`test_runaway_counts_growth_of_old_session`).
2. **Reboot after a cap stop.** The unit is `WantedBy=default.target`, so cap and runaway stops
   must `disable` as well as `stop`. Pinned in Task 4 (`test_stop_disables_unit`).
3. **A `$0` cap or a `0.0` spend.** `if cap:` / `if spend:` guards break both. Pinned in Task 4
   (`test_zero_cap_stops_immediately`, `test_zero_spend_month_no_actions`).
4. **A second friend's sign-in landing in the owner's or another friend's credentials dir.**
   Pinned in Task 2 (`test_credentials_dir_is_inside_profile`) and Task 8 step 3 (mtime check).
5. **Re-running `friends add` for an existing name, or with a bot token already in use.** It must
   refuse before touching anything. Pinned in Task 3
   (`test_add_refuses_existing_name_without_side_effects`, `test_add_refuses_reused_bot_token`).

---

### Task 0: Spike on the droplet (no product code)

Facts the code depends on, confirmed by experiment with a throwaway profile `spike`. Uses the
owner's test bot (prerequisite 1). Result: a `## Spike results` section appended to **this plan
file**, giving the command and output for each item. Then delete the profile.

**Files:** Modify: `docs/ai/plans/2026-10-24-hermes-for-friends.md` (append only)

- [ ] **Step 1:** `hermes profile create spike --no-alias --no-skills`. Record which files and
  dirs it creates under `~/.hermes/profiles/spike/`.
- [ ] **Step 2:** Write a minimal `.env` (test bot token, `TELEGRAM_ALLOWED_USERS`, `TERMINAL_ENV=docker`,
  `TERMINAL_DOCKER_IMAGE=hermes-sandbox:latest`). Run
  `HERMES_HOME=… hermes gateway install --start-now --start-on-login`. Record the exact unit
  name, and confirm `systemctl --user cat <unit>` pins `HERMES_HOME`.
- [ ] **Step 3:** Set `platform_toolsets.telegram` to the list in Task 2's `TELEGRAM_TOOLSETS`.
  Restart the unit. Record `HERMES_HOME=… hermes tools --summary`. Pass condition: `browser`,
  `image_gen`, `delegation`, `code_execution` absent for telegram. If a plain list is not
  honoured, record the form that is (for example via `hermes tools disable --platform telegram`).
- [ ] **Step 4:** Add the `google` MCP server as Task 2 renders it pre-activation
  (`--tool-tier complete`, include `start_google_auth` only, `WORKSPACE_MCP_PORT=8101`,
  `GOOGLE_OAUTH_REDIRECT_URI=https://auth.fibonadithya.me/spike/oauth2callback`,
  `WORKSPACE_MCP_CREDENTIALS_DIR=<profile>/google-credentials`). From the test Telegram account,
  ask for sign-in. Record:
  - (a) whether the auth URL's `redirect_uri` is the public URL;
  - (b) whether anything listens on `127.0.0.1:8101` while the flow is pending (`ss -ltnp`);
  - (c) whether `WORKSPACE_EXTERNAL_URL` or `WORKSPACE_MCP_BASE_URI` is also needed.
  Do not complete consent yet; Caddy doesn't exist until Task 7.
- [ ] **Step 5:** During one ongoing Telegram session, query `estimated_cost_usd` for that session
  in `<profile>/state.db` before and after two more messages. Record whether it increases.
- [ ] **Step 6:** Record `HERMES_HOME=… hermes send -t telegram:<test id> -q "spike"` exit code
  and delivery. Record the non-interactive form of `hermes profile delete` (look for `--yes` or
  similar) and of `hermes gateway uninstall`.
- [ ] **Step 7:** Check whether `web_extract` with `web.use_gateway: true` fetches from Nous's
  side, not locally. Ask the agent to extract `http://127.0.0.1:8101/` and record the result.
  Pass condition: it fails or returns Nous-side content, and no local connection appears.
- [ ] **Step 7a:** Environment leak check. Hermes says a profile without its own key "silently
  inherited API keys from the shell environment" (`hermes_cli/profiles.py:1108`). With the spike
  gateway running, list the variable **names** (never values) in
  `/proc/<gateway pid>/environ` and in its workspace-mcp child's environ
  (`tr '\0' '\n' < /proc/<pid>/environ | cut -d= -f1 | sort`). Compare with the key names in the
  owner's `~/.hermes/.env`. Pass condition: no owner-only key appears (owner's
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_USERS` values differ from the spike's, and no
  DeepInfra or other provider key is present). If one leaks, record its source (systemd user
  manager env vs the calling shell) and stop.
- [ ] **Step 8:** Uninstall the unit, delete the profile, and confirm with
  `ls ~/.hermes/profiles` and `systemctl --user list-units 'hermes-gateway*'`.
- [ ] **Step 9:** Commit the appended results.

If any pass condition fails, stop and report. Steps 3, 4, 7 and 7a each decide part of Task 2's
design. (Already confirmed on the droplet on 2026-10-24, no need to re-check: every name in
`TELEGRAM_TOOLSETS` and the excluded list exists in `toolsets.py`; `profile create/delete`
resolve `~/.hermes/profiles` correctly even when `HERMES_HOME` points at the friend's profile
(`hermes_constants.get_default_hermes_root`); workspace-mcp credentials are
`<email>.json` holding a `refresh_token` key; nothing listens on 8101–8199.)

---

### Task 1: Package skeleton, validation, discovery, owner settings

**Files:**
- Create: `friends/pyproject.toml` (pattern: `tuner/pyproject.toml`; deps `pyyaml>=6`; dev `pytest>=8`; console script `friends = "hermes_friends.cli:main"`), `friends/.python-version` (copy tuner's), `friends/hermes_friends/__init__.py`, `policy.py`, `model.py`, `validate.py`, `registry.py`, `owner.py`
- Test: `friends/tests/test_validate.py`, `test_registry.py`, `test_owner.py`, `friends/tests/conftest.py` (fixture `hermes_root(tmp_path)` building a fake `~/.hermes` with an owner `.env`)

**Interfaces:**
- Produces, `policy.py` (constants, verbatim):
  `PUBLIC_HOST = "auth.fibonadithya.me"`, `PORTS = range(8101, 8200)`, `DEFAULT_CAP_USD = 5.0`,
  `WARN_FRACTION = 0.8`, `RUNAWAY_USD = 1.0`, `RUNAWAY_WINDOW_S = 3600`,
  `FALLBACK_USD_PER_MTOK = 1.89` (ESTIMATE: the Nous v4-pro *output* list price, as recorded in
  `tuner/tests/factory.py` `NOUS_V4_PRO`, applied to every token. Friends run a cheaper model, so
  this over-counts. Not DeepSeek's direct $0.87: the droplet is billed through Nous),
  `MEM_PER_FRIEND_MB = 500`, `MEM_FLOOR_MB = 768` (both ESTIMATE, §6),
  `MAIN_MODEL = AUX_MODEL = "deepseek/deepseek-v4.1-flash-1017"` (proposal, owner confirms in Task 8),
  `DEFAULT_CADDY_DIR = Path("/etc/caddy/friends.d")`, `CADDY_FILE_MODE = 0o644` (Caddy runs as user
  `caddy` and must read these files at boot; `600` would break Caddy on the next reboot),
  `def gateway_unit(name: str) -> str` (value from Task 0 step 2).
  The Caddy dir is never read from the constant directly by code under test: `cli.main` takes
  `caddy_dir: Path | None` (default `DEFAULT_CADDY_DIR`), so tests pass `tmp_path / "caddy"`.
- Produces, `model.py`: `@dataclass Friend(name: str, telegram_id: int, email: str, tz: str, port: int, cap_usd: float, activated: bool)`
  with `Friend.load(profile_dir: Path) -> Friend` and `Friend.save(profile_dir: Path) -> None` (writes `friend.json`, mode 600).
- Produces, `validate.py`: `validate_name(s) -> str`, `validate_telegram_id(s) -> int`,
  `validate_tz(s) -> str`, `validate_email(s) -> str`, `validate_bot_token(s) -> str`. Each raises
  `ValueError` with a one-line reason.
- Produces, `registry.py`: `profile_dir(root: Path, name: str) -> Path`, `list_friends(root: Path) -> list[Friend]`
  (only profiles containing `friend.json`; sorted by name), `allocate_port(friends: list[Friend]) -> int`
  (lowest free in `PORTS`; `RuntimeError` when exhausted), `bot_token_in_use(root: Path, token: str) -> bool`
  (checks the owner's `.env`, every profile's `.env`, **and** `approvals_bot_token` in
  `<root>/broker/broker.json` if that file exists. The approvals bot's token lives there, not in
  `.env` (checked on the droplet 2026-10-24), and a second `getUpdates` consumer on it would break
  approvals).
- Produces, `owner.py`: `@dataclass OwnerEnv(chat_id: int, google_client_id: str, google_client_secret: str)`,
  `read_owner_env(root: Path) -> OwnerEnv`. Raises if `TELEGRAM_ALLOWED_USERS` holds anything other
  than exactly one positive integer, or if either Google value is missing.

- [ ] **Step 1: Write failing tests**
  - `test_name_rules`: `"alice"`, `"bob2"` accepted. `""`, `"Alice"`, `"al-ice"`, `"default"`
    and `"../x"` raise.
  - `test_telegram_id_rules`: `"123456"` → `123456`. `""`, `"0"`, `"-5"`, `"@alice"`, `" 12"` raise.
  - `test_tz_rules`: `"Asia/Kolkata"` accepted. `"Mars/Base"` and `""` raise.
  - `test_bot_token_rules`: `"123456:" + "A"*35` accepted. `"abc"` raises.
  - `test_list_friends_ignores_plain_profiles`: a profile dir without `friend.json` is not
    returned. Catches: discovery by directory listing alone.
  - `test_allocate_port_fills_gap`: friends on 8101 and 8103 → 8102. All 99 taken → `RuntimeError`.
  - `test_bot_token_in_use_checks_owner_env`: owner's token → True. Catches: only scanning
    profiles.
  - `test_bot_token_in_use_checks_approvals_bot`: the conftest fixture writes
    `broker/broker.json` with `approvals_bot_token`; that token → True. A root without
    `broker.json` does not raise. Catches: checking `.env` files only.
  - `test_owner_env_rejects_two_ids`: `TELEGRAM_ALLOWED_USERS=1,2` raises. Blank raises.
  - `test_friend_roundtrip_and_mode`: save then load is equal, and the file mode is `0o600`.
- [ ] **Step 2:** `cd friends && uv sync --extra dev && uv run pytest -q`. Expected: FAIL (imports).
- [ ] **Step 3:** Implement the modules above.
- [ ] **Step 4:** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit: `feat(friends): package skeleton, validation, profile discovery`.

---

### Task 2: Rendering the profile and the Caddy route

**Files:**
- Create: `friends/hermes_friends/render.py`, `friends/hermes_friends/template/SOUL.md`,
  `friends/hermes_friends/template/skills/email/assistant/SKILL.md`
- Test: `friends/tests/test_render.py`

**Interfaces:**
- Consumes: `Friend`, `OwnerEnv`, `policy` (Task 1).
- Produces: `render_config(f: Friend) -> str` (YAML; pre- or post-activation chosen by `f.activated`),
  `render_env(f: Friend, bot_token: str, owner: OwnerEnv) -> str`, `render_soul(f: Friend) -> str`,
  `render_memory(f: Friend) -> str`, `render_caddy(f: Friend) -> str`, `skill_files() -> dict[str, bytes]`
  (relative path → content, for copying into `<profile>/skills/`).
- Produces, constants in `render.py`: `GOOGLE_TOOLS` (the 7 names, verbatim:
  `search_gmail_messages`, `get_gmail_message_content`, `draft_gmail_message`,
  `modify_gmail_message_labels`, `list_calendars`, `get_events`, `manage_event`),
  `SIGNIN_TOOLS = ("start_google_auth",)`, `TELEGRAM_TOOLSETS = ["web", "terminal", "file",
  "cronjob", "memory", "todo", "session_search", "clarify", "skills"]` (adjust to Task 0 step 3 if needed).

What `render_config` emits (beyond the tested keys, copy the owner's values):
- `model` (provider `nous`, base URL `https://inference-api.nousresearch.com/v1`, default `MAIN_MODEL`);
- `auxiliary` (`web_extract`, `compression`, `title_generation` pinned to nous and `AUX_MODEL`);
- `terminal` (backend docker, persistent, `container_memory: 1024`);
- `web: {backend: firecrawl, use_gateway: true}`;
- `timezone: <f.tz>`;
- `platform_toolsets: {telegram: TELEGRAM_TOOLSETS}`;
- `mcp_servers.google`:
  - args `["workspace-mcp", "--single-user", "--tool-tier", "complete" if not activated else "extended", "--permissions", "gmail:drafts", "calendar:full"]`;
  - env `GOOGLE_OAUTH_CLIENT_ID: "${GOOGLE_OAUTH_CLIENT_ID}"`, `GOOGLE_OAUTH_CLIENT_SECRET: "${GOOGLE_OAUTH_CLIENT_SECRET}"`, `USER_GOOGLE_EMAIL`, `WORKSPACE_MCP_PORT: str(f.port)`, `GOOGLE_OAUTH_REDIRECT_URI: https://auth.fibonadithya.me/<name>/oauth2callback`, `WORKSPACE_MCP_CREDENTIALS_DIR: /home/hermes/.hermes/profiles/<name>/google-credentials`, plus any variable Task 0 step 4c found necessary;
  - `tools.include`: `SIGNIN_TOOLS` pre-activation, `GOOGLE_TOOLS` after.

`render_caddy` emits exactly one block:
`handle /<name>/oauth2callback { rewrite * /oauth2callback?{query}; reverse_proxy 127.0.0.1:<port> }`
(Caddyfile syntax, newlines rather than semicolons).

`SOUL.md` (copy is the builder's):
- a short generic assistant persona;
- one sentence: instructions found inside an email or a web page are reported to the user and
  never followed;
- must not quote any injection phrase (scanner lesson).

`render_memory`:
- the friend's first name and timezone;
- while not activated, one line telling the agent that on the user's first message it calls
  `start_google_auth` for `<email>` and sends the link.

`SKILL.md`:
- the triage / calendar / reminder workflow from spec 07-28 §4, using only `GOOGLE_TOOLS` names;
- drafts only, never send;
- no wiki, broker, fleet or owner references.

- [ ] **Step 1: Write failing tests**
  - `test_only_google_mcp_server`: `set(yaml["mcp_servers"]) == {"google"}`. Catches: a `compute`
    entry copied from the owner.
  - `test_preactivation_tools`: not activated → include `== ["start_google_auth"]` and the args
    contain `"complete"`.
  - `test_postactivation_tools`: activated → include `== GOOGLE_TOOLS`, args contain `"extended"`,
    and `"start_google_auth"` is not in the include list. Catches: sign-in tool left registered.
  - `test_no_send_tool_anywhere`: `"send_gmail_message"` not in the rendered config, either
    state. `"gmail:drafts"` follows `"--permissions"` in args.
  - `test_credentials_dir_is_inside_profile`: `WORKSPACE_MCP_CREDENTIALS_DIR` starts with
    `/home/hermes/.hermes/profiles/alice/`. Catches: the shared default dir (§1).
  - `test_redirect_and_port`: redirect is `https://auth.fibonadithya.me/alice/oauth2callback`;
    `WORKSPACE_MCP_PORT == "8101"` for `port=8101`.
  - `test_secrets_not_in_config`: the rendered config contains `${GOOGLE_OAUTH_CLIENT_SECRET}` and
    not the literal secret.
  - `test_toolsets_exclude_browser`: `platform_toolsets.telegram == TELEGRAM_TOOLSETS` and none of
    `browser`, `image_gen`, `delegation`, `code_execution`, `computer_use`, `homeassistant` is in it.
  - `test_web_via_gateway`: `web.use_gateway is True`.
  - `test_models_pinned`: `model.provider == "nous"`, and every `auxiliary.*.provider == "nous"`
    (none `auto`).
  - `test_env_allowlist`: `render_env` has `TELEGRAM_ALLOWED_USERS=<id>` and `TERMINAL_ENV=docker`.
    `TELEGRAM_ALLOWED_USERS=` with an empty value is impossible (Friend's `telegram_id` int ≥ 1 is
    asserted inside `render_env`).
  - `test_caddy_block`: `render_caddy` contains `handle /alice/oauth2callback` and
    `reverse_proxy 127.0.0.1:8101`, and has no `handle_path` and no `/*` wildcard. Catches: a
    prefix route exposing everything under `/alice/`.
  - `test_memory_signin_line_only_before_activation`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `render.py` and the two template files.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: `feat(friends): render friend profile and Caddy route`.

---

### Task 3: The `System` boundary, `friends add` and `friends activate`

**Files:**
- Create: `friends/hermes_friends/system.py`, `friends/hermes_friends/cli.py`
- Test: `friends/tests/fakesystem.py`, `friends/tests/test_add.py`, `friends/tests/test_activate.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces, `system.py`: class `System` with methods
  `hermes(name: str | None, *args: str) -> str` (runs `hermes …` with `HERMES_HOME` = the friend's
  profile, or the owner's root when `name is None`; raises on non-zero),
  `systemctl(*args: str) -> None` (always `--user`),
  `mem_available_mb() -> int` (`/proc/meminfo` `MemAvailable`),
  `caddy_reload() -> None` (`caddy reload --config /etc/caddy/Caddyfile`),
  `write_file(path: Path, data: str | bytes, mode: int) -> None`,
  `remove_tree(path: Path) -> None`,
  `revoke_google_token(refresh_token: str) -> None` (POST `https://oauth2.googleapis.com/revoke`),
  `send(name: str | None, chat_id: int, text: str) -> bool` (`hermes send -q -t telegram:<id>`;
  returns False on failure, never raises).
- `FakeSystem` records every **mutating** call in order, in `self.calls: list[tuple]`
  (`hermes`, `systemctl`, `caddy_reload`, `write_file`, `remove_tree`, `revoke_google_token`,
  `send`). Reads (`mem_available_mb`) are not recorded. It has settable `mem_mb` and failure
  injection. It performs **real** filesystem effects under `tmp_path` so later assertions read
  real files: `write_file` writes (creating parents) and applies the mode, `remove_tree` deletes,
  `hermes(name, "profile", "create", …)` creates the profile dir, and
  `hermes(name, "profile", "delete", …)` deletes it. Without this, tests such as
  `test_remove_leaves_nothing` would pass on files that were never created.
- Produces, `cli.py`: `main(argv: list[str] | None = None, system: System | None = None, root: Path | None = None, caddy_dir: Path | None = None, now: datetime | None = None) -> int`.
  `now` defaults to `datetime.now(ZoneInfo("Europe/London"))`; tests always pass it.
  Subcommands `add`, `activate` here. Task 4 adds `spend-check`. Task 5 adds `list`, `cap`, `resume`, `remove`.

`friends add <name> --telegram-id --bot-token --tz --email [--cap 5]`, in this order:
1. Validate all inputs.
2. Refuse if the profile exists, if `bot_token_in_use`, or if
   `mem_available_mb() - MEM_PER_FRIEND_MB < MEM_FLOOR_MB`.
3. `hermes profile create <name> --no-alias --no-skills`.
4. Write config, env, SOUL, memory, skills and `friend.json` (activated=False).
5. Write `<caddy_dir>/<name>.caddy` with mode `CADDY_FILE_MODE`, then `caddy_reload()`.
6. Install the gateway with the Task 0 command, start it and enable it.
7. Print the redirect URI to add in Google console, and a reminder to tell the friend to send
   `/start` to their new bot.

On a failure after step 3, roll back what was created, in reverse order, and exit non-zero.

`friends activate <name>`: refuse unless `<profile>/google-credentials/<email>.json` exists. Then
set activated=True, re-render `config.yaml` and `memories/MEMORY.md`, and restart the unit.

- [ ] **Step 1: Write failing tests**
  - `test_add_happy_path_call_order`: the profile-create call precedes file writes, which precede
    the caddy reload, which precedes the gateway install. Every `hermes` call is made with
    `name="alice"`.
  - `test_every_hermes_call_sets_home`: run `add` and `activate`; assert no `hermes` call made for
    a friend has `name is None`. Catches: one call hitting the owner's profile.
  - `test_add_refuses_existing_name_without_side_effects`: the profile dir exists →
    `main(...) != 0` and `fake.calls == []`.
  - `test_add_refuses_reused_bot_token`: same, using the owner's token.
  - `test_add_refuses_low_memory`: with `mem_mb = 1267`, add is refused (1267 − 500 = 767 < 768).
    With `mem_mb = 1268`, it proceeds. Catches: an off-by-one in the comparison.
  - `test_add_rolls_back_on_gateway_failure`: gateway install raises → profile dir removed,
    caddy file removed, caddy reloaded again, exit ≠ 0.
  - `test_written_modes`: `config.yaml` and `.env` written with `0o600`; the Caddy file with
    `0o644`. Catches: one blanket `600` for every file, which breaks Caddy at the next reboot.
  - `test_activate_requires_credentials`: no creds file → exit ≠ 0, config unchanged.
  - `test_activate_switches_tools`: creds present → the re-rendered config has `GOOGLE_TOOLS`,
    and the unit is restarted.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: `feat(friends): add and activate`.

---

### Task 4: Spend accounting and `friends spend-check`

**Files:**
- Create: `friends/hermes_friends/spend.py`
- Modify: `friends/hermes_friends/cli.py` (add `spend-check`)
- Test: `friends/tests/test_spend.py` (builds a real SQLite `state.db` with a `sessions` table
  holding columns `id, source, started_at, input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens, estimated_cost_usd`, the subset the tuner reads in
  `tuner/hermes_tuner/history.py:17`)

**Interfaces:**
- Produces:
  - `month_start(now: datetime) -> datetime`: the 1st at 00:00 in `Europe/London`.
  - `@dataclass MonthSpend(priced_usd: float, unpriced: list[str], unpriced_usd: float)`, with
    property `total_usd`.
  - `month_spend(db: Path, start: datetime) -> MonthSpend`. `sessions.started_at` is a REAL Unix
    epoch (`tuner/hermes_tuner/model.py:22`, `tuner/tests/factory.py:34`), so the query is
    `started_at >= ?` with `start.timestamp()`, never a datetime or ISO string. A session with
    tokens > 0 and a null or 0 cost is unpriced, priced as
    `(input + output + cache_read + cache_write) / 1e6 * FALLBACK_USD_PER_MTOK`. A missing
    `state.db` means `MonthSpend(0, [], 0)`; check `db.exists()` before connecting (plain
    `sqlite3.connect` creates an empty file), and open read-only (`file:…?mode=ro`, `uri=True`).
    Months are always London-local: `SpendState.month` and the `new_month` comparison use
    `now.astimezone(ZoneInfo("Europe/London")).strftime("%Y-%m")`.
  - `@dataclass SpendState(month: str, warned: bool, stopped_by_cap: bool, stopped_by_runaway: bool,
    alerted_unpriced: list[str], samples: list[tuple[float, float]])`, with `load(profile) / save(profile)`
    (`<profile>/friend-spend.json`, mode 600; a missing file gives a fresh state for the current month).
  - `decide(f: Friend, spend: MonthSpend, st: SpendState, now: datetime) -> list[str]`, returning
    action names from `{"new_month", "warn", "stop_cap", "stop_runaway", "alert_unpriced"}`.
    `decide` is pure; `cli.spend_check` applies the actions and saves the state.

Rules in `decide`:
1. If `st.month != now's YYYY-MM`, emit `new_month`. Applying it resets the state (including
   `samples`, since last month's totals are not comparable with this month's). If
   `stopped_by_cap` was set **and `stopped_by_runaway` is not**, it enables and starts the unit.
   `stopped_by_runaway` is **kept** across months, and while it is set nothing but
   `friends resume` restarts the unit.
2. `warn` once per month when `total >= WARN_FRACTION * cap` and not yet warned.
3. `stop_cap` when `total >= cap` and not already `stopped_by_cap`.
4. `stop_runaway` when `not st.stopped_by_runaway` and
   `total - (the sample closest to, and at or before, now − 3600 s) > RUNAWAY_USD`. Without the
   guard, the same hour-old sample re-fires the stop and both messages every 15 minutes for up
   to an hour. With no sample that old, nothing is emitted.
5. `alert_unpriced` for session ids not in `alerted_unpriced`.

Samples: `decide` does not record them. After applying actions, `spend_check` appends
`(now.timestamp(), total)` to `st.samples`, prunes samples older than 7200 s, and saves the
state. Without this step the runaway trip can never fire.

Applying:
- `stop_*`: send the friend's message first, then `systemctl stop` and `disable`, then message
  the owner.
- `warn`: message the friend and the owner.

Fixed copy, with amounts to 2 dp:
- Friend at 80%: `You've used 80% of this month's assistant allowance ($4.00 of $5.00, estimate). It resets on the 1st.`
- Friend at 100%: `You've reached this month's assistant allowance ($5.00, estimate). I'll be back on the 1st.`
- Friend on a runaway stop: `I've paused myself because I was using far more than usual. The owner has been told.`
- Owner: `friends: <name> <event> — $<total> of $<cap> this month (ESTIMATE).` Here `<event>` is one
  of `passed 80%`, `reached cap; gateway stopped and disabled`,
  `spent >$1.00 in an hour; gateway stopped and disabled`, or
  `has unpriced sessions <ids>; counted at fallback rate`.

`spend-check` iterates `list_friends`. One friend's error is reported to the owner and does not
stop the others.

- [ ] **Step 1: Write failing tests**
  - `test_month_window_london`: a session at 2026-09-30 23:30 UTC (that is, 2026-10-01 00:30 BST)
    counts for October, not September. Catches: a UTC month boundary. 2026-10-31 23:30 UTC (GMT)
    counts for October.
  - `test_unpriced_session_fallback`: 1,000,000 tokens with a NULL cost → `unpriced_usd == 1.89`,
    and the id is listed. A session with 0 tokens and a NULL cost is ignored.
  - `test_started_at_is_epoch`: the fixture stores `started_at` as a REAL epoch; a session at
    `month_start.timestamp() - 1` is excluded and one at `month_start.timestamp()` is included.
    Catches: comparing against a datetime/ISO string, and `>` for `>=`.
  - `test_missing_db_not_created`: `month_spend` on a nonexistent path returns zero and the file
    still does not exist afterwards.
  - `test_zero_spend_month_no_actions`: `spend=0.0`, `cap=5` → `[]`.
  - `test_zero_cap_stops_immediately`: `cap=0`, `spend=0.0` → includes `stop_cap`. Catches
    `if cap:`.
  - `test_warn_threshold_boundary`: 3.99 → no warn. 4.00 → warn. A second call with
    `warned=True` → no warn.
  - `test_cap_boundary`: 4.99 → no stop. 5.00 → `stop_cap`. Already stopped → no repeat.
  - `test_runaway_counts_growth_of_old_session`: a sample of 1.00 at now − 3600 s and a current
    total of 2.01 → `stop_runaway`, even though no session started in the last hour. A current
    total of 2.00 → none (strictly greater than).
  - `test_runaway_needs_old_enough_sample`: only samples younger than 3600 s → none.
  - `test_runaway_not_repeated`: same inputs as the firing case but `stopped_by_runaway=True` →
    no `stop_runaway`. Catches: deleting the guard (repeated stop and messages).
  - `test_spend_check_records_samples`: two `main(["spend-check"], now=t)` runs 15 minutes apart
    leave two samples in `friend-spend.json`; a sample older than 7200 s is pruned. Then a third
    run at `t + 3600 s` with spend grown by $1.01 stops the unit. Catches: samples never appended
    (runaway can never fire), which the pure `decide` tests cannot see.
  - `test_new_month_restarts_cap_stop_not_runaway`, three cases: cap-only stop → `enable` and
    `start`; runaway-only stop → no `start`; both flags set → no `start` and `stopped_by_runaway`
    still set. Catches: restarting a runaway-held friend on the 1st.
  - `test_new_month_clears_samples`.
  - `test_stop_disables_unit`: applying `stop_cap` records the friend message, then `stop`, then
    `disable`, then the owner message, in that order.
  - `test_one_friend_error_does_not_block_others`: a corrupt `state.db` for one friend → the
    owner is alerted, and the second friend is still evaluated.
- [ ] **Step 2–4:** Run (FAIL), implement, run (pass).
- [ ] **Step 5:** Commit: `feat(friends): monthly cap, runaway trip, unpriced fallback`.

---

### Task 5: `list`, `cap`, `resume`, `remove`

**Files:**
- Modify: `friends/hermes_friends/cli.py`
- Test: `friends/tests/test_manage.py`

**Interfaces:**
- Consumes: Tasks 1–4 (`spend.month_spend`, `SpendState.load` from Task 4).

Behaviour:
- `list`: one line per friend with name, unit active/inactive, `signed-in yes/no`,
  `$x.xx of $y.yy ESTIMATE`.
- `cap <name> <usd>`: accepts `0` (stop immediately at the next check). Rejects negative values.
  If the friend is stopped by cap and the new cap exceeds month spend, clear `stopped_by_cap`, and
  enable and start the unit **unless `stopped_by_runaway` is set**.
- `resume <name>`: clears `stopped_by_runaway` and/or `stopped_by_cap`, then enables and starts
  the unit. This is the only way back from a runaway stop.
- `remove <name>`:
  1. Read `refresh_token` from the creds file, if present, and `revoke_google_token` it.
     Continue on failure, but print the failure.
  2. Stop, disable and uninstall the unit (Task 0 command).
  3. Delete `<caddy_dir>/<name>.caddy`, then `caddy_reload()`.
  4. `hermes profile delete` (non-interactive form from Task 0), then `remove_tree` if anything
     is left.
  5. Print "delete @<bot> in BotFather".

  No archive is written.

- [ ] **Step 1: Write failing tests**
  - `test_cap_zero_accepted`, `test_cap_negative_rejected`.
  - `test_cap_raise_restarts_capped_friend`: the state is `stopped_by_cap` and the new cap is
    above spend → `enable` and `start` calls.
  - `test_cap_raise_keeps_runaway_hold`: both flags set, cap raised → `stopped_by_cap` cleared,
    no `start` call.
  - `test_resume_clears_runaway`.
  - `test_remove_revokes_before_delete`: the revoke call index is lower than the profile-delete
    index.
  - `test_remove_continues_when_revoke_fails`: the profile is still deleted, and output mentions
    the failure.
  - `test_remove_leaves_nothing`: after remove, the profile dir and caddy file are absent, and
    `list_friends` is empty.
  - `test_remove_unknown_name_no_calls`.
  - `test_list_shows_estimate_label`: each line ends `ESTIMATE`; an unsigned-in friend shows `signed-in no`.
- [ ] **Step 2–4:** Run (FAIL), implement, run (pass).
- [ ] **Step 5:** Commit: `feat(friends): list, cap, resume, remove`.

---

### Task 6: Mutation check of Tasks 1–5

**Files:** none committed.

- [ ] **Step 1:** Apply each mutation, run `uv run pytest -q`, confirm at least one failure, and
  revert:
  - `>=` → `>` in the cap and warn comparisons;
  - `>` → `>=` in the runaway check;
  - `if f.cap_usd:` guard inserted in `decide`;
  - `"complete"` ↔ `"extended"` swapped;
  - `WORKSPACE_MCP_CREDENTIALS_DIR` line deleted;
  - `disable` call deleted;
  - `name=None` passed to one `hermes` call in `add`;
  - `< MEM_FLOOR_MB` → `<= MEM_FLOOR_MB`;
  - `handle` → `handle_path /alice/*` in `render_caddy`;
  - `not st.stopped_by_runaway` guard deleted from rule 4;
  - `and not stopped_by_runaway` deleted from the `new_month` restart, and from `cap`;
  - the sample append deleted from `spend_check`;
  - `CADDY_FILE_MODE` replaced by `0o600`;
  - the `broker.json` check deleted from `bot_token_in_use`;
  - `start.timestamp()` replaced by `start.isoformat()` in `month_spend`.
- [ ] **Step 2:** Record a mutation → failing-test table in the PR body. Any mutation that
  survives gets a test in the owning task's file, plus a commit
  `test(friends): close surviving mutation <x>`.

---

### Task 7: Droplet integration: deploy, units, bootstrap, CI

**Files:**
- Modify: `droplet/harness-pull.sh`. After `systemctl --user restart hermes-approvals` (and
  before the stamp is written) add `(cd friends && /usr/local/bin/uv sync --no-dev -q)`. Placing
  it after the restart means a broken `friends` sync never holds back the owner's broker deploy;
  it still blocks the stamp, so it is retried next tick.
- Create: `droplet/install-friends.sh`. Same pattern as `install-tuner.sh`: sync, link
  `~/.local/bin/friends` → `friends/.venv/bin/friends`, copy units, `daemon-reload`, do **not**
  enable.
- Create: `droplet/units/friends-spend.service`. Oneshot,
  `ExecStart=%h/hermes-harness/friends/.venv/bin/friends spend-check`.
- Create: `droplet/units/friends-spend.timer` (`OnCalendar=*:0/15`, `Persistent=true`).
- Create: `droplet/bootstrap-friends.sh`. Run once with sudo by the owner; idempotent:
  - install Caddy from its official apt repo;
  - write `/etc/caddy/Caddyfile` with `auth.fibonadithya.me { import /etc/caddy/friends.d/*.caddy  handle { respond 404 } }`;
  - `install -d -o hermes -g hermes -m 755 /etc/caddy/friends.d`;
  - create a 2 GB `/swapfile` if absent, and add it to fstab;
  - `ufw allow 80/tcp`, `ufw allow 443/tcp`.

  The owner's own Caddy admin API stays on localhost.
- Modify: `.github/workflows/ci.yml`. Add a `friends` job mirroring `tuner`.
  `droplet/*.sh` is already shellchecked by the `box` job.

- [ ] **Step 1:** `shellcheck -S warning droplet/*.sh`. Expected: clean.
- [ ] **Step 2:** `cd friends && uv run pytest -q`. Expected: pass.
- [ ] **Step 3:** Commit: `feat(friends): droplet units, bootstrap, deploy hook, CI`.
- [ ] **Step 4:** Push over SSH, open the PR, and wait for `gh pr checks <n> --watch` to report a
  terminal pass for all jobs (per the `review-pr` skill). Merge only on owner approval. After the
  merge, confirm `harness-pull` deployed it:
  `ssh hermes-vm cat ~/.local/share/harness-deployed.sha` equals the merge SHA.

---

### Task 8: Runbook and live verification

**Files:**
- Create: `runbooks/friends.md`, covering:
  - prerequisites;
  - bootstrap;
  - adding a person end to end, including the Google console redirect URI step;
  - what the friend does;
  - `list`, `cap`, `resume`, `remove`;
  - spend messages and what they mean;
  - "moving friends to their own box" (§8);
  - the accepted risks (§1 shared host, web extract).
- Modify: this plan, appending `## Verification results`.

- [ ] **Step 1:** The owner runs `sudo droplet/bootstrap-friends.sh`, then `droplet/install-friends.sh`,
  then `systemctl --user enable --now friends-spend.timer`.
- [ ] **Step 2:** Model choice. Show the owner per-session cost for `MAIN_MODEL` vs
  `deepseek-v4-pro`, from the owner's `state.db` since 2026-10-18, and the resulting sessions per
  $5, labelled ESTIMATE. Proceed with the model the owner picks; if it changes, edit `policy.py`
  and redeploy.
- [ ] **Step 3:** Run spec §9 checks 1–11 with the test bot and test Google account. For each,
  record the command or action and the observed result in `## Verification results`. Check 3
  includes `stat -c %Y` of the owner's `~/.google_workspace_mcp/credentials/*.json` before and
  after. Check 9 uses `friends cap test 0.01`, then a forced month change: edit `friend-spend.json`'s
  `month` to the previous month and run `friends spend-check`. Also confirm the SOUL loaded: no
  scanner block for the test profile in `journalctl --user -u <unit>`. Repeat Task 0 step 7a's
  environment check against the real test profile's gateway.
- [ ] **Step 4:** `friends remove test`, then confirm check 11.
- [ ] **Step 5:** Commit the runbook and results: `docs(friends): runbook and live verification`.

---

## Spike results (2026-10-25, droplet `hermes-vm`, Hermes v0.19.0)

Throwaway profile `spike`, test bot @Jeeves_fibonadi_bot, allowlisted the owner's own Telegram ID.
All results MEASURED in this session.

| Step | Command / action | Result |
|---|---|---|
| 1 | `hermes profile create spike --no-alias --no-skills` | exit 0. Creates `.env` (600), `.no-bundled-skills`, `SOUL.md` (664), `cron home logs memories plans sessions skills skins workspace`. Profile dir mode **775** — `friends add` must chmod 700. |
| 2 | `HERMES_HOME=$P hermes gateway install --start-now --start-on-login` | Unit `hermes-gateway-spike.service`: `Environment="HERMES_HOME=$P"`, `ExecStart=… -m hermes_cli.main --profile spike gateway run`, `Restart=always`, `WantedBy=default.target`, enabled, active. |
| 3 | `platform_toolsets.telegram: [web, terminal, file, cronjob, memory, todo, session_search, clarify, skills]`; `HERMES_HOME=$P hermes tools list --platform telegram` | Plain list honoured: those enabled; `browser`, `code_execution`, `image_gen`, `delegation`, `computer_use`, `homeassistant` disabled. (`hermes tools --summary` needs a TTY.) |
| 4a | Asked the bot to call `start_google_auth` | Auth URL `redirect_uri=https://auth.fibonadithya.me/spike/oauth2callback` (10 scopes). |
| 4b | `ss -ltnp` while the flow was pending | workspace-mcp listening on `127.0.0.1:8101` and `[::1]:8101`; nothing before the flow started. |
| 4c | — | No extra variable needed; `GOOGLE_OAUTH_REDIRECT_URI` alone suffices. Owner's server entry uses `command: uvx`. |
| 4 (finding) | Asked the bot to list its tools | `tools.include` does not cover Hermes' MCP utility tools: `mcp__google__{list_resources,read_resource,list_prompts,get_prompt}` were registered. Switches: `mcp_servers.<n>.tools.resources/prompts` (default true, `tools/mcp_tool.py:5560`). Friend config sets both false (Ruling 3). Live re-check pending (Task 8 check 5). |
| 5 | Same open session, one more message | `api_call_count` 5→6, `estimated_cost_usd` 0.00249286→0.00308926 (`cost_status=estimated`). Cost accrues while a session is open. |
| 6 | `HERMES_HOME=$P hermes send -t telegram:<id> -q "<text>"` | exit 0 and delivered (owner confirmed) both with the gateway running and stopped. Non-interactive forms: `hermes profile delete -y <name>`, `HERMES_HOME=$P hermes gateway uninstall`. |
| 7 | Asked the bot to `web_extract` `http://127.0.0.1:8101/` | `"Blocked: URL targets a private or internal network address"`, refused before any request. Whether public fetches are local or Nous-side is still unknown; spec §1 accepted risk stands. |
| 7a | Env var **names** of the spike gateway and every descendant vs the owner's `.env` keys | None of the owner's 19 keys present. MCP children receive the Google client vars and no `TELEGRAM_BOT_TOKEN`. |
| 8 | stop, disable, `gateway uninstall`, `profile delete -y spike` | Uninstall exit 0, but the unit lingers as "not-found failed" until `systemctl --user reset-failed`. **`profile delete` failed: Permission denied** on root-owned `sandboxes/docker/prompt-backend-probe/home/.hermes{,/skills}` (the sandbox runs as root). `docker run --rm --network=none --user 0 -v $P:/p hermes-sandbox:latest chown -R $(id -u):$(id -g) /p` then `profile delete -y` → exit 0. `remove` does both (Ruling 9). |
| — | Owner credential mtimes before/after | `<owner>.json` changed 06:00:52→08:00:39 GMT, matching the owner's own triage cron (`cron_3bc4e11d29b1_20261025_080033`) refreshing its token; owner `oauth_states.json` unchanged; the spike's OAuth state was written to `spike/google-credentials/oauth_states.json`. |

Also confirmed: profile commands resolve `~/.hermes/profiles` correctly with `HERMES_HOME` set to the
friend's profile; the approvals bot token lives in `~/.hermes/broker/broker.json`; 0 of 38 owner
sessions since 2026-10-18 had tokens with a NULL/0 cost.

Owner inputs for Task 8: test Google account `adithya@tig.foundation` (Google Workspace — an admin
policy may block the unverified app; unknown until sign-in). The owner's own Gmail is refused by
`friends add` (Ruling 4).
