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
   ```bash
   ssh hermes-vm "systemctl status hermes-gateway --no-pager"
   ```
   If reachable, restart it:
   ```bash
   ssh hermes-vm "sudo systemctl restart hermes-gateway"
   ```

2. **SSH unreachable?** Open the DigitalOcean control panel → droplet →
   **Access** → **Launch Droplet Console**. Log in as `hermes` with the
   password from the password manager. Diagnose from there — check
   `systemctl status hermes-gateway`, `journalctl -u hermes-gateway -n 100`,
   and `df -h` (a full disk is a common cause).

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
- **Only on the droplet:** `~/.hermes/.env` (OpenRouter key, Telegram token,
  Google OAuth client secret), the sandbox SSH key, and the Google OAuth token
  cache. None of these are recoverable from a lost droplet without a snapshot —
  they are re-created by re-running the plan.
- **Snapshots** are point-in-time and manual. Take a fresh one after any
  significant configuration change.
