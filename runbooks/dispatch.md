# Dispatch runbook

Everything below runs on the droplet as the `hermes` user. If you reached the
box through the DigitalOcean console you are `root` — run `sudo -iu hermes`
first, or every `systemctl --user` command will look at root's manager and find
nothing. No command here needs `sudo`.

## Granting and revoking

The assistant asks in the main Telegram chat and prints a 4-digit code. Reply in
the **approvals** chat — a different bot — with `approve 7391`. Codes expire in
two minutes and are single-use. `revoke` in that chat locks every box at once.

Forwarded messages are ignored by design. If you forward yourself a code it will
not work; type it.

## Inspecting state

```bash
cat ~/.hermes/broker/state/grants.json          # pending requests and live grants
journalctl --user -u hermes-approvals -n 50     # what the approvals bot did
journalctl --user -u hermes-gateway -n 50       # what the assistant did
```

A grant is live if `grants.<box>.expires_at` is in the future. Times are epoch
seconds — `date -d @1754400000` to read one.

## A task went wrong

```bash
ssh tig-server 'cat /srv/hermes-tasks/<task-id>/status'
ssh tig-server 'tail -100 /srv/hermes-tasks/<task-id>/log'
ssh tig-server 'docker ps --filter name=hermes-task'
```

Kill a stuck one with `docker rm -f hermes-task-<task-id>` on tig-server. The
container is `--rm`, so there is nothing else to clean up.

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
- **Claude Code credential** — re-run the token step from the plan and rewrite
  `/etc/hermes-exec/claude-credentials.json` the same way.
- **Approvals bot token** — BotFather `/revoke`, then edit
  `~/.hermes/broker/broker.json` and `systemctl --user restart hermes-approvals`.

## Things that are supposed to fail

- Any dispatch tool without a grant → `LOCKED: no active grant for <box>`.
- A cron job asking for access → it gets a code nobody approves, then `LOCKED`.
- `curl` from inside the agent's sandbox → no network, by design.
- A push to `main` from the executor → rejected by branch protection.

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
- **Rotating the executor's GitHub token.** `ssh -t tig-server 'nano /etc/hermes-exec/github-token'`.
