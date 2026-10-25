# Friends runbook

Everything below runs on the droplet as the `hermes` user, except the bootstrap
step. From the DigitalOcean console you are `root`: run `sudo -iu hermes`, then
`export XDG_RUNTIME_DIR=/run/user/$(id -u)`. Without the switch,
`systemctl --user` looks at root's manager; without the export it fails with
"Failed to connect to bus: No medium found".

## What it does

Each friend gets their own Hermes profile under `~/.hermes/profiles/<name>/`, run by
the same Linux user (`hermes`) as yours: their own Telegram bot, their own Google
sign-in, their own gateway unit `hermes-gateway-<name>`, and a monthly spend cap.
The `friends` command creates, caps and removes them. A timer
(`friends-spend.timer`, every 15 minutes) enforces the cap.

## Prerequisites

- DNS: an `A` record `auth.fibonadithya.me` pointing at the droplet IP. Already in place.
- Google Cloud console: the OAuth consent screen of the app that owns your Web
  application OAuth client must be **In production**. In *Testing*, refresh tokens
  expire after 7 days. An unverified app is limited to 100 users for its lifetime;
  every Google account that has ever signed in counts.
- A Telegram bot per friend, created by you in @BotFather. Keep the bot token.
- The friend's numeric Telegram ID. The friend can get it by messaging @userinfobot.
- The friend's Google address (`--email`). It must not be one of your own Google
  accounts (see "Adding a person").

## Bootstrap (once)

1. As root (from the console, or `sudo` from another user):

   ```
   sudo ~hermes/hermes-harness/droplet/bootstrap-friends.sh
   ```

   It creates a 2 GB `/swapfile` (and the `/etc/fstab` line), runs
   `ufw allow 80/tcp` and `ufw allow 443/tcp`, installs Caddy, creates
   `/etc/caddy/friends.d` (owned by `hermes`), writes `/etc/caddy/Caddyfile`, and
   reloads or restarts Caddy. An existing `/etc/caddy/Caddyfile` that differs is
   copied to `/etc/caddy/Caddyfile.bak.<timestamp>` first. It is safe to re-run and
   prints a summary of what it changed. It does not enable or disable ufw.

   If a DigitalOcean cloud firewall is attached to the droplet, it must also allow
   inbound 80 and 443. I have not checked whether one is attached
   (unverified); Caddy cannot get its certificate without port 80 or 443 reachable.

2. As `hermes`:

   ```
   cd ~/hermes-harness && droplet/install-friends.sh
   systemctl --user enable --now friends-spend.timer
   ```

   `install-friends.sh` runs `uv sync` in `friends/`, links
   `~/.local/bin/friends`, and installs `friends-spend.service` and
   `friends-spend.timer` into `~/.config/systemd/user/`. It does not enable the
   timer; the second command does.

3. Check: `systemctl --user list-timers friends-spend.timer` shows a next run, and
   `friends list` prints `no friends`.

## Adding a person

1. In @BotFather, create the bot (`/newbot`). Note its token.
2. Get the friend's numeric Telegram ID and the Google address they will sign in with.
3. Run:

   ```
   friends add alice --telegram-id 123456789 --bot-token <token> --tz Europe/London --email alice@example.com [--cap 5]
   ```

   - `name`: 1 to 32 lowercase letters and digits. `default` is reserved.
   - `--tz`: an IANA timezone name.
   - `--cap`: monthly cap in USD, a positive number. The default is 5.0.
   - The bot token is on the command line, so it lands in shell history. Clear it if
     that matters to you.

   `add` refuses (nothing is created) when: the email is one of your own Google
   accounts (exit 1); the profile already exists; the bot token is already used by
   another profile; or there is not enough free memory for another friend (it needs
   500 MB plus a 768 MB floor, both ESTIMATE). Bad arguments exit 2. If a step fails
   part-way, `add` rolls back the steps it completed and exits 1.

   The owner's account is refused because `remove` revokes the friend's Google
   refresh token on the shared OAuth client, and Google revokes by token, not by
   profile. Using your own address would let a later `remove` cut off your own
   sign-in.

   On success it prints:

   ```
   Created friend alice on port 8101; gateway hermes-gateway-alice started.
   Add this redirect URI to the Google Cloud OAuth Web client:
     https://auth.fibonadithya.me/alice/oauth2callback
   Tell alice to send /start to their bot.
   ```

   (The port is the first free one in 8101 to 8199.) The profile starts
   unactivated.

4. Google Cloud console, APIs and Services, Credentials: open the existing **Web
   application** OAuth client and add the printed URI under "Authorized redirect
   URIs". Save. Do this before the friend signs in, or Google shows a
   `redirect_uri_mismatch` error.
5. Tell the friend to send `/start` to their bot. A bot cannot message first. The
   bot replies with a sign-in link.
6. The friend opens the link, signs in with the address given to `--email`, clicks
   past Google's "unverified app" warning, and approves. They see a success page.
7. Run `friends activate alice`. If the credentials file
   `google-credentials/<email>.json` is not there yet it prints
   `error: alice has not signed in to Google yet (...)` and exits 1. On success it
   rewrites the profile config without the sign-in tool, restarts the gateway, and
   prints `Activated alice.`
   If a cap or runaway hold is set it writes the config but does not restart, and
   prints ``alice activated; gateway stays stopped (cap/runaway hold) — use `friends cap` or `friends resume`.``

Google Workspace caveat: if the friend's address is a Google Workspace account, the
Workspace admin may block unverified apps or restrict the Gmail and Calendar scopes.
The friend then sees a block page at step 6 that you cannot fix from this side; they
need to ask their admin, or use a personal Google account.

## What the friend does

This can be copied to them.

> 1. Open your bot in Telegram and send `/start`.
> 2. The bot sends you a link. Open it and sign in with your Google account.
> 3. Google warns that the app is not verified. Choose Advanced, then continue to the
>    app. Approve the access it asks for.
> 4. When you see the success page, tell the owner. After they switch you on, the
>    bot starts working.
>
> The assistant has a monthly allowance. It messages you at 80% and when it is used
> up, and starts again on the 1st.

## Day to day

| Command | What it does |
|---|---|
| `friends list` | One line per friend: `alice  active  signed-in yes  $1.20 of $5.00 ESTIMATE`. `active` is the gateway unit's state; `signed-in` is whether the Google credentials file exists. Prints `no friends` when there are none. A friend whose files cannot be read shows `error: ...` and the exit code is 1. |
| `friends cap alice 8` | Sets the monthly cap in USD (0 or more; negative or non-finite exits 2). If the gateway was stopped for the cap and the new cap is above this month's spend, it clears the cap stop and starts the gateway. If a runaway hold is also set it stays paused and prints `alice stays paused: runaway hold; use resume.` Setting 0 makes the next check stop the gateway. |
| `friends resume alice` | Clears both the cap stop and the runaway hold and the runaway baseline (the saved spend samples, so old samples cannot trigger an immediate runaway stop), then enables and starts the gateway. Prints `Resumed alice.` Exits 2 with an error if the friend was never activated. If the friend is still over the cap, the next check stops them again and sends the cap messages again. |
| `friends remove alice` | See below. |
| `friends activate alice` | See "Adding a person". |

`friends spend-check` is what the timer runs; you can run it by hand. It exits 1 if
any friend's check failed, and sends you a Telegram message
`friends: <name> spend-check failed: <reason>` for each one.

### Removing a person

```
friends remove alice
```

It runs these steps in order and carries on past any step that fails, printing
`warning: <step> failed: <reason>` to stderr: revoke the friend's Google refresh
token (done first, because deleting the profile destroys it); stop and disable
`hermes-gateway-alice`; `hermes gateway uninstall`; `systemctl --user reset-failed`
on the unit; force-remove any Docker container that mounts something under the profile
(the sandbox container outlives `profile delete` and keeps its network); reclaim root-owned files in the profile; `hermes profile delete -y`;
remove the profile directory if anything is left; remove
`/etc/caddy/friends.d/alice.caddy`; reload Caddy.

- **No archive is kept.** The profile, sessions, memory, and Google credentials are
  deleted. Mail-derived data should not outlive the friend's access.
- The sandbox runs as root in Docker and leaves root-owned files that would make the
  delete fail. `remove` reclaims them itself (a `chown` through the sandbox image),
  so you do not need `sudo`.
- It always ends by printing `delete the bot in BotFather (@BotFather → /deletebot)`.
  Do that; `remove` does not touch the bot.
- Exit code 0: every step succeeded. Exit 1: at least one step warned, or the profile
  directory is still there (`remove finished with warnings; check the lines above`).
  Read the warnings, fix the cause, and run `friends remove alice` again; it works on
  a half-removed profile as long as `friend.json` is still there. An unknown name
  exits 1 (`no such friend: alice`).
- Revoking the token does not remove the redirect URI from the Google console, and
  does not remove the friend's grant from their own Google account page. Delete the
  redirect URI from the OAuth client if you want it gone.

## Spend limits

The monthly total per friend is the sum of `estimated_cost_usd` over sessions
started since the 1st of the month, Europe/London time, in that friend's
`state.db`. These are Hermes's own estimates, not Nous invoices. Treat every dollar
figure here as ESTIMATE.

`friends-spend.timer` checks every 15 minutes (`OnCalendar=*:0/15`, `Persistent=true`,
so a missed run happens at the next boot). A session that is still open keeps
accruing between checks, so spend can pass the cap by up to one interval's worth plus
the estimate error.

| Event | When | What happens |
|---|---|---|
| Warning | total reaches 80% of the cap, once a month (not sent if the same check also reaches the cap, and never sent after a cap stop that month, including after `friends cap` raises the cap mid-month) | Friend: `You've used 80% of this month's assistant allowance ($4.02 of $5.00, estimate). It resets on the 1st.` Owner: `friends: alice passed 80% — $4.02 of $5.00 this month (ESTIMATE).` |
| Cap stop | total reaches the cap | Friend: `You've reached this month's assistant allowance ($5.00, estimate). I'll be back on the 1st.` Gateway is stopped and disabled (so it does not return on reboot, and the friend's cron jobs stop with it). If the friend message cannot be sent, the stop still happens, the friend is not marked notified, and you get `friends: alice could not be messaged about the stop`; it is not retried. Owner: `friends: alice reached cap; gateway stopped and disabled — $5.03 of $5.00 this month (ESTIMATE).` |
| Runaway stop | spend over the last hour exceeds $1.00, measured against the sample from at least an hour earlier | Friend: `I've paused myself because I was using far more than usual. The owner has been told.` Gateway stopped and disabled. Owner: `friends: alice spent >$1.00 in an hour; gateway stopped and disabled — ...`. Only `friends resume alice` restarts it. |
| Unpriced sessions | a session has tokens but a missing or zero cost | Owner only, once per session: `friends: alice has unpriced sessions <ids>; counted at fallback rate — ...`. These are counted at $1.89 per million tokens over all tokens (ESTIMATE, the Nous v4-pro output list price, so it over-counts for a cheaper model). |
| New month | first check after the month changes | Counters reset. A gateway stopped for the cap is enabled and started, and you get `friends: alice new month; gateway re-enabled.` It stays stopped if a runaway hold is set, and if the friend is already over the cap in the new month. A gateway you stopped by hand is not restarted. |

The runaway check needs a sample at least an hour old, so a new friend is not
runaway-checked in their first hour. Samples are kept for two hours.

The owner's own profile is never capped by this timer. The account-wide backstop is
the Nous org-level monthly cap that you set on the Nous portal. Nothing in this
build sets or reads it, and reaching it stops your own assistant too.

## Where state lives

Per friend, under `~/.hermes/profiles/<name>/` (mode 700; files 600, except the
Caddy file):

| Path | Contents |
|---|---|
| `friend.json` | name, Telegram ID, email, timezone, port, cap, `activated` |
| `friend-spend.json` | month, warned/stopped/notified flags, unpriced session IDs already reported, spend samples for the last two hours |
| `google-credentials/` | the friend's Google token `<email>.json` and `oauth_states.json` |
| `state.db` | Hermes's session history; the spend figures come from here |
| `.env`, `config.yaml`, `SOUL.md`, `skills/`, `memories/MEMORY.md` | rendered from the templates only when the profile is created, with one exception: `friends activate` rewrites `config.yaml` and removes only the sign-in line from `MEMORY.md` (anything else in the file is kept) |

Elsewhere:

| Path | Contents |
|---|---|
| `/etc/caddy/friends.d/<name>.caddy` | the friend's route: `/<name>/oauth2callback` to `127.0.0.1:<port>`; every other path returns 404 (mode 644 so Caddy can read it at boot) |
| unit `hermes-gateway-<name>` | the friend's gateway (`systemctl --user ... hermes-gateway-<name>`) |
| `~/.config/systemd/user/friends-spend.{service,timer}` | the spend check |
| `~/.local/share/friends-synced.sha` | the master commit the friends code was last synced to |
| `~/.hermes/broker/broker.json` | holds the approvals bot token; friends do not use it |

## Deploys

Merging to master reaches the droplet through `harness-pull` (every 15 minutes: it
resets `~/hermes-harness` to `origin/master`). Friends has its own stamp,
`friends-synced.sha`, and its own step (`uv sync` in `friends/`). If the friends
sync fails, `harness-pull` prints `friends sync failed` and exits 1, but your own
deploy stamp (`harness-deployed.sha`) was already written, so the approvals restart
does not repeat on every tick. The friends step is retried on the next tick.

Code changes to `friends` apply at once. Template changes (SOUL, skills, config,
memory) apply only to profiles created afterwards; existing profiles keep the files
they were created with. To change an existing friend, edit their profile files by
hand and restart `hermes-gateway-<name>`.

## Moving friends to their own box

This is not built; these are the steps.

1. New box with Hermes, Docker (and the `hermes-sandbox:latest` image), Caddy, and
   this repo's `friends` tool. Run the bootstrap and install steps there.
2. For each friend: `hermes profile export <name>` here, copy the archive over a
   verified transfer (check `sha256sum` on both ends), `hermes profile import` there.
3. On the new box, regenerate the gateway unit and Caddy route (`hermes gateway
   install --start-now --start-on-login` with `HERMES_HOME` set to the profile, and
   the `.caddy` file for the friend). Ports may need reallocating.
4. Move the `auth.fibonadithya.me` DNS record to the new box. The redirect URIs do
   not change.
5. Run `friends remove <name>` on the old box for each moved friend. This revokes the
   Google token, so the friend signs in again on the new box; if you want to keep the
   token, delete the profile by hand instead of using `remove`.
6. Delete or ignore the old Caddy files and units.

## Accepted risks

- **Shared host.** Friends run as the same Linux user as you, on the same machine as
  your Google token, the broker's box key and your Telegram token. An agent that
  escaped its sandbox would land as `hermes`, which has Docker and sudo. The
  mitigation is moving friends off this box (above), not a boundary here. A friend
  profile's environment was checked to contain none of your `.env` keys (spike,
  2026-10-25).
- **Web extract.** The friend's terminal sandbox has no network
  (`TERMINAL_DOCKER_NETWORK=false` in the friend's `.env`, with image
  `hermes-sandbox:latest`). Friends also get Hermes's `web` toolset, and page
  extraction still fetches URLs from the host, outside that sandbox. An injected email could try to
  make the agent fetch an attacker's URL with mailbox content in the query string.
  The only mitigation is the standing rule in the friend's SOUL: report instructions
  found in email, never act on them. It is a soft rule. Private and internal
  addresses are refused (a fetch of `http://127.0.0.1:8101/` returned "Blocked: URL
  targets a private or internal network address").

Known limit: a session still open across the 1st counts in the month it started, so
its later spend is not counted in the new month.

## Troubleshooting

- **Gateway logs:** `journalctl --user -u hermes-gateway-alice -n 100 --no-pager`.
- **Spend check logs:** `journalctl --user -u friends-spend -n 100 --no-pager`.
  Run `friends spend-check` by hand to see the errors on stderr.
- **Bot does not answer `/start`:** check `friends list` shows `active`, then the
  gateway log. Check the Telegram ID passed to `--telegram-id`: only that ID is
  allowed.
- **`redirect_uri_mismatch` at sign-in:** the URI printed by `add` is not yet saved
  on the Web client in the Google console.
- **Sign-in link stops working after a week:** the OAuth app is in *Testing*; move it
  to *In production*.
- **Persona not loading:** if a profile's `SOUL.md` contains phrases the injection
  scanner matches (for example a quoted "ignore previous instructions"), Hermes
  skips it without telling the friend. Look for the scanner block message in the
  gateway log.
- **A `prompt-backend-probe` container with bridge networking:** Hermes's own prompt
  builder starts it and does not pass the network setting. Agent commands do not run
  in it; they run in the sandbox container, which has no network.
- **Unit shows "not-found failed" after a removal:** `systemctl --user reset-failed
  hermes-gateway-alice`. `remove` already does this.
- **A friend is paused and you do not know why:** `cat
  ~/.hermes/profiles/alice/friend-spend.json`. `stopped_by_cap` means the cap,
  `stopped_by_runaway` means the hourly check. Raise the cap with `friends cap`
  (clears a cap stop) or use `friends resume` (clears both).
- **Caddy cannot get a certificate:** `journalctl -u caddy -n 50` (as root), confirm
  the DNS record resolves to this droplet, and that ports 80 and 443 are open in
  both ufw and any DigitalOcean cloud firewall.
