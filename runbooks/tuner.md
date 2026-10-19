# Tuner runbook

Everything below runs on the droplet as the `hermes` user. From the DigitalOcean
console you are `root`: run `sudo -iu hermes`, then
`export XDG_RUNTIME_DIR=/run/user/$(id -u)`. Without the switch,
`systemctl --user` looks at root's manager; without the export it fails with
"Failed to connect to bus: No medium found".

## What it does

Once a day (03:30 London, `hermes-tuner.timer`) the tuner reads Hermes's session
history, looks for repeated wasted work (the same tool sequence every run, the
same message read again in consecutive runs, repeated `tool_describe` lookups),
and drafts a fix with one model call. A fix is a job prompt change, a
`tools.tool_search.*` or `compression.*` config value, a skill edit, or a composed
tool. It checks each draft against fixed rules, replays it as a shadow
(dry-run) session, applies it as a trial, and keeps or rolls it back by
measured cost. Anything that widens what Hermes can reach (a `new_reach` fix)
is only proposed to you; it never applies without your reply. Once a week it
writes one summary line, which the approvals bot sends.

## Where state lives

All under `~/.hermes/tuner/` (mode 700, files 600):

| Path | Contents |
|---|---|
| `ledger.json` | every fix the tuner has handled, one record each |
| `cycle_state.json` | pending restart flag, restart times (28 days), last weekly line written, error log (28 days) |
| `versions/<id>/fix.json` | the full fix as drafted |
| `versions/<id>/before.json` | live values before the fix was applied (the rollback source) |
| `versions/<id>/after.json` | the values the fix wrote |
| `tools/<name>/` | composed tools (`tool.json`, `program.py`), read by the plugin. A `tool.json` with `"shadow_only": true` is waiting for its shadow check: the plugin runs it only in shadow sessions until go-live rewrites it without the flag |
| `plugin_heartbeat.json` | written by the plugin when the gateway loads it (`pid`, `registered_at`). The tuner schedules shadows and applies composed tools only when `plugins.enabled` lists `hermes-tuner` and this is newer than the last restart it recorded; otherwise it logs `plugin not loaded; shadow not scheduled` |
| `shadow_jobs.json` | shadow runs scheduled and not yet evaluated |
| `shadow/<session>.jsonl` | what a shadow session tried to do (one intent per line) |
| `proposals/<id>.json` | `new_reach` proposals waiting for your reply |
| `outbox/weekly-<year>-W<week>.txt` | the weekly line waiting to be sent (renamed to `.sent` after) |
| `.lock` | held while a cycle runs; a second run prints `already running` and exits |

## Read the ledger

```bash
python3 -m json.tool ~/.hermes/tuner/ledger.json | less
```

Each record has `id`, `kind` (`job_prompt`, `config`, `skill`, `composed_tool`,
`new_reach`), `target`, `state`, `reason`, and times (epoch seconds; read one
with `date -d @1754400000`). States:

| State | Meaning |
|---|---|
| `shadow_pending` | drafted; waiting for its shadow run to be compared |
| `shadow_failed` | the shadow run did not match or did not run; nothing was applied |
| `rejected` | the draft broke a rule; see `reason` |
| `proposed` | `new_reach` change waiting for your `tool` or `drop` reply |
| `approved` | you approved it; the next run applies it |
| `dropped` | you dropped the proposal |
| `trial` | applied; its cost is being measured against the baseline |
| `applied` | applied by an approved proposal |
| `kept` | the trial saved enough; it stays |
| `rolled_back` | undone, and `before.json` was restored exactly |
| `owner_changed` | the tuner wanted to undo it but you had changed the value since; it left your value alone (`reason` names which part) |
| `bridge_error` | a Hermes call failed; nothing was left half-applied (`reason` has the error) |

`trial`, `applied` and `shadow_pending` count as active; the tuner starts no
second fix on a target that has one.

## Roll back a fix by hand

Find the id in the ledger, then read `~/.hermes/tuner/versions/<id>/before.json`
and `after.json`. Only restore a value if it still equals `after.json`;
otherwise you changed it yourself since.

**job_prompt.** `before.json` holds `prompt` and `enabled_toolsets`. Restore the
prompt with the cron CLI:

```bash
hermes cron edit <job_id> --prompt "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["prompt"])' ~/.hermes/tuner/versions/<id>/before.json)"
```

`hermes cron edit` has no option for `enabled_toolsets`. If `after.json` changed
it, edit `enabled_toolsets` for that job in `~/.hermes/cron/jobs.json` while the
gateway is stopped (`systemctl --user stop hermes-gateway`, edit, start it).

**config.** `before.json` is `{"key", "value"}`:

```bash
hermes config set <key> <value>
```

**skill.** `before.json` has `file`, `b64` (the original bytes) and `mode`:

```bash
python3 - <<'PY'
import base64, json, os
b = json.load(open(os.path.expanduser("~/.hermes/tuner/versions/<id>/before.json")))
with open(b["file"], "wb") as f:
    f.write(base64.b64decode(b["b64"]))
os.chmod(b["file"], b["mode"])
PY
```

**composed_tool.** Restore the job first if `before.json` has a `job` entry (as
for `job_prompt`, using `before.json`'s `job.prompt`), then remove the tool:

```bash
rm -r ~/.hermes/tuner/tools/<tool name>
```

The tool disappears from Hermes at the next gateway restart. After a manual
rollback, set the record's `state` to `rolled_back` in `ledger.json` (stop the
timer first, so no cycle writes it at the same time), or leave it and let the
next run report `owner_changed`.

## Pause, and run once

Pause:

```bash
systemctl --user disable --now hermes-tuner.timer
```

Run one cycle now, for real:

```bash
systemctl --user start hermes-tuner.service
journalctl --user -u hermes-tuner -n 50
```

Dry run (no fix is applied and no Hermes call mutates anything; it prints the
report, including what it would do under `planned`):

```bash
cd ~/hermes-harness/tuner && .venv/bin/python -m hermes_tuner run --dry-run
```

## Replies in the approvals chat

A `new_reach` fix arrives in the **approvals** chat as a tool proposal with a
4-digit code and the exact change. Read the change, then type the reply (not
forwarded, not copied from elsewhere):

- `tool 7391` approves it. The tuner applies it on its next run.
- `drop 7391` discards it.

Codes are single-use and expire (7-day TTL). If a code has expired, wait for the
tuner's re-announcement and use the new code. Editing a proposal after approval voids the approval.

## One-time owner steps

These are yours. The tuner never does them and its policy forbids changing
`plugins.*` or `platform_toolsets`.

1. Install: `bash ~/hermes-harness/droplet/install-tuner.sh` (after a deploy has
   put the code on the box). It syncs the venv, links the plugin, installs the
   units, and leaves the timer off.
2. Enable the plugin. Plugins in `~/.hermes/plugins/` load only when named in
   `plugins.enabled` (the key is the plugin's directory name, `hermes-tuner`):

   ```bash
   hermes plugins enable hermes-tuner
   systemctl --user restart hermes-gateway   # in the quiet window
   ```
3. If Telegram or cron jobs do not see composed tools (names starting `tuned_`),
   add the `tuner` toolset by hand. Check the current value first with
   `hermes config get platform_toolsets.telegram`, then keep what is there and
   add `tuner`, for example:

   ```bash
   hermes config set platform_toolsets.telegram '["hermes-telegram","tuner"]'
   ```

   Do the same for `platform_toolsets.cron` so jobs without an explicit
   `enabled_toolsets` see them.
4. Review the dry-cycle report (the `--dry-run` command above). If it looks
   right, enable the timer yourself:

   ```bash
   systemctl --user enable --now hermes-tuner.timer
   ```

   Enabling the timer is your decision; the install script never does it.

## Weekly line

One line a week, sent by the approvals bot from `outbox/`:

```
Hermes tuner: saved ~$X.XX this week (ESTIMATE), A applied, K kept, R rolled back; tuner spent $Y.YY
```

with `; errors: N (see ~/.hermes/tuner/cycle_state.json and ledger.json)`
appended when the tuner logged errors that week. The messages are in the
`errors` list of `cycle_state.json` (last 28 days); records that failed have the
reason in `ledger.json`.

Every dollar figure is an ESTIMATE. Hermes's `estimated_cost_usd` changed regime
when billing moved to the subscription (it reads $0), so the tuner never uses it:
every decision and figure prices a run from its tokens at one fixed rate,
`policy.USD_PER_MTOK` (0.42 $/Mtok, from the September median). Figures are
comparable week to week but are not an invoice.
