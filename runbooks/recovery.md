# Recovery runbook — hermes-assistant

**Host:** DigitalOcean droplet `hermes-assistant`, Ubuntu 24.04 LTS,
2 vCPU / 4 GB / lon1. IPv4 **139.59.168.236**.
**Login:** `ssh hermes-vm` (alias in `~/.ssh/config`), user `hermes`,
key `~/.ssh/id_ed25519`. Root SSH login and all password SSH auth are
disabled.

## Before you need this

The `hermes` user must have a **password set** (`sudo passwd hermes`), stored
in the password manager. SSH does not accept passwords — this exists solely so
the DigitalOcean web console is usable. A droplet created from an SSH key has
no password by default, which makes the recovery console useless exactly when
you need it. Verify you can still log into the console after any rebuild.

## When the assistant stops responding on Telegram

1. **Is the gateway down, or the box?**

   The gateway runs under the **user** systemd manager as `hermes`, not the
   system manager. Every command needs `--user` and none of them need `sudo`.
   A system-scope `systemctl status hermes-gateway` answers "Unit
   hermes-gateway.service could not be found" on a perfectly healthy box —
   do not read that as a dead service.

   ```bash
   ssh hermes-vm "systemctl --user status hermes-gateway --no-pager"
   ```
   If reachable, restart it:
   ```bash
   ssh hermes-vm "systemctl --user restart hermes-gateway"
   ```
   Logs are in the user journal:
   ```bash
   ssh hermes-vm "journalctl --user -u hermes-gateway -n 100 --no-pager"
   ```

   The unit is `~/.config/systemd/user/hermes-gateway.service` with
   `Restart=always` and `WantedBy=default.target`. Boot survival depends on
   **lingering** — `loginctl show-user hermes | grep Linger` must report
   `Linger=yes`, or the gateway will not start until someone logs in. Re-enable
   with `sudo loginctl enable-linger hermes`.

2. **SSH unreachable?** Open the DigitalOcean control panel → droplet →
   **Access** → **Launch Droplet Console**. Log in as `hermes` with the
   password from the password manager. Diagnose from there — check
   `systemctl --user status hermes-gateway`,
   `journalctl --user -u hermes-gateway -n 100`, and `df -h` (a full disk is a
   common cause). If you logged into the console as `root` rather than
   `hermes`, `--user` targets root's own manager and finds nothing. Switch
   with `sudo -iu hermes`, then `export XDG_RUNTIME_DIR=/run/user/$(id -u)`.
   The export is required: without it every `systemctl --user` fails with
   "Failed to connect to bus: No medium found", and `su - hermes` fails the
   same way. `machinectl` is not installed on the droplet.

3. **Console shows an unresponsive box?** Control panel → **Power** →
   **Power Cycle**. This is a power-pull; verified safe against this
   configuration on 2026-07-28 (firewall, SSH hardening, and filesystem all
   survived).

4. **Box unrecoverable?** Control panel → **Snapshots** → restore the most
   recent snapshot (baseline: `hermes-assistant-baseline`). Then re-run the
   implementation plan from Task 2. Secrets must be re-entered by hand; the
   wiki is safe on GitHub and gets re-cloned in Task 6.

## What is and isn't backed up

- **Safe on GitHub:** the `hermes-wiki` knowledge graph (pushed every 15 min
  by `hermes-wiki-sync.timer`).
- **Only on the droplet:** `~/.hermes/.env` (DeepInfra key, Telegram token,
  Google OAuth client secret), the sandbox SSH key, and the Google OAuth token
  cache. None of these are recoverable from a lost droplet without a snapshot —
  they are re-created by re-running the plan.
- **Snapshots** are point-in-time and manual. Take a fresh one after any
  significant configuration change.
