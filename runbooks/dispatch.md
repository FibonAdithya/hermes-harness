# Dispatch runbook

Everything below runs on the droplet as the `hermes` user. If you reached the
box through the DigitalOcean console you are `root` — run `sudo -iu hermes`
first, or every `systemctl --user` command will look at root's manager and find
nothing. No command here needs `sudo`.

## Granting and revoking

The assistant asks in the main Telegram chat and prints a 4-digit code. Reply in
the **approvals** chat — a different bot — with `approve 7391`. Codes expire in
two minutes and are single-use. `revoke` in that chat locks every box at once.

### Deploys

The assistant can ask to deploy a `hermes-harness` commit to the box
(`request_deploy`). It tells you a commit and the reply to type. Read that
commit on `master` first, then reply in the approvals chat with
`deploy <first 7+ characters of the commit>`, typed from what you read rather
than copied from the assistant's message. The approval covers that commit only,
for one deploy within 10 minutes. If `master` has moved on by the time it runs,
the box refuses and the assistant has to ask again. A `tig-server` grant does
not allow a deploy.

The droplet (the broker and this approvals bot) is never deployed by the
assistant: `systemctl --user start harness-pull.service` here, by hand.

Forwarded messages are ignored by design. If you forward yourself a code it will
not work; type it.

## Inspecting state

```bash
cat ~/.hermes/broker/state/grants.json          # pending requests and live grants
journalctl --user -u hermes-approvals -n 50     # what the approvals bot did
journalctl --user -u hermes-gateway -n 50       # what the assistant did
```

A grant is live if `grants.<box>.expires_at` is in the future. An approved
deploy is `grants.deploy` (with its `sha`); a requested one is `pending_deploy`. Times are epoch
seconds — `date -d @1754400000` to read one.

## A task went wrong

```bash
ssh tig-adi 'cat ~/nights/<task-id>/status'
ssh tig-adi 'tail -100 ~/nights/<task-id>/log'
ssh tig-adi 'docker ps --filter name=hermes-task'
```

Kill a stuck one with `systemctl --user stop night-<task-id>` on the box; the
wrapper removes the container. `docker rm -f hermes-task-<task-id>` is the
fallback. The container is `--rm`, so there is nothing else to clean up.

## tig-gpu was recreated

vast.ai gives it a new host and port. Update `HostName` and `Port` under
`Host tig-gpu` in `~/.ssh/config` on the droplet. Nothing else changes — the key
and the `gpuq` path are stable. Confirm with:

```bash
ssh tig-gpu /venv/main/bin/gpuq list
```

`gpuq` is not on the PATH for a non-interactive shell; always use the full path.

## Rotating credentials

- **GitHub PAT** — regenerate the fine-grained token, then
  `ssh -t tig-server 'nano /etc/hermes-exec/github-token'`. Never pass it as a
  shell argument.
- **Claude Code credential** — the executor mounts the box's own login,
  `~adi/.claude/.credentials.json`, read-only. Re-login with `ssh -t tig-adi claude`.
- **Approvals bot token** — BotFather `/revoke`, then edit
  `~/.hermes/broker/broker.json` and `systemctl --user restart hermes-approvals`.

## Things that are supposed to fail

- Any dispatch tool without a grant → `LOCKED: no active grant for <box>`.
- `deploy_harness` without an approved deploy, or a second time on one
  approval → `LOCKED: no approved deploy`.
- A cron job asking for access → it gets a code nobody approves, then `LOCKED`.
- `curl` from inside the agent's sandbox → no network, by design.
- A push to `master` from the executor → rejected by branch protection.
- The executor merging its own pull request → **possible, accepted** (its token
  is the owner's, by decision). `harness-pull.timer` stays disabled on both
  hosts, so nothing merged runs until you deploy it: on the box by approving
  `deploy <commit>` after reading it (or by hand with
  `systemctl --user start harness-pull.service`), and on the droplet only by
  hand. A bad merge is undone with a revert PR (force-push to `master` is
  blocked).

If any of those succeed, stop and investigate before using the system again.

## The TIG box

The box is `tig-adi` from the laptop (user `adi`). Hermes reaches it only as
the dispatch key, which can run `~/.local/bin/dispatch` and nothing else.

```bash
ssh tig-adi 'echo "{}" | SSH_ORIGINAL_COMMAND=status ~/.local/bin/dispatch'   # what the broker sees
ssh tig-adi 'ls ~/nights; cat ~/nights/<id>/status; tail -50 ~/nights/<id>/log'
ssh tig-adi 'systemctl --user list-timers --no-pager'                          # nights, wiki mirror, harness pull
ssh tig-adi 'systemctl --user stop night-<id>.service'                         # kill a night; status becomes killed
```

- **A verb is missing.** It is a file in `box/verbs` on `master`; `harness-pull`
  installs `master` every 15 minutes. `journalctl --user -u harness-pull -n 20`.
- **Nothing ran last night.** `systemctl --user list-timers` shows whether the
  timers are enabled; they ship disabled. `~/nights/config.toml` holds the
  inputs; an empty Talos queue is a `skipped` night, not an error.
- **The dispatcher says "Failed to connect to bus".** `XDG_RUNTIME_DIR` for
  `adi` is not `/run/user/<uid>`; read `loginctl show-user adi -p RuntimePath`.
- **herdr version.** The box runs 0.7.5, pinned in `box/provision.sh` from the
  GitHub release because `herdr.dev/install.sh` always installs the latest.
  Upgrade the laptop and the box together: set `HERDR_VERSION` and re-run
  `provision.sh`, then `herdr update` on the laptop.
- **Rotating the dispatch key.** New keypair on the laptop; replace the
  `command=...` line in `~adi/.ssh/authorized_keys` and the key in
  `~/.ssh/tig_server_dispatch` on the droplet.
- **Pausing a Talos night.** Hermes calls `pause_talos(<night id>)`; by hand,
  `ssh tig-adi 'systemctl --user kill --kill-whom=main --signal=SIGINT night-<id>.service'`.
  Do not use `systemctl stop` on a Talos night unless it is stuck: that also
  ends it cleanly (TERM becomes SIGINT for Talos), but reads `killed`. A night
  still reads `running` until Talos has cancelled its job, then `paused`.
  Continue with `run_talos(resume=<job id>, backend=…)`.
- **Setting up the c3 backend.** Once, on the box as `adi`: a checkout of the
  pinned Talos at `~/talos-c3` with its venv (as `~/talos-modal`), then
  `cd ~/talos-c3 && .venv/bin/talos setup` with backend `c3`, provider
  `claude-cli`, and a key from `c3 apikey create` on the laptop. Until
  `~/talos-c3/talos.config.json` exists, `run_talos(backend="c3")` refuses with
  "talos-c3 is not set up". Top up credit with `c3 topup`; each run is capped
  by its `compute_usd` (default $5, at most $90).
- **Rotating the executor's GitHub token.** `ssh -t tig-server 'nano /etc/hermes-exec/github-token'`.
