#!/usr/bin/env bash
# Install the Hermes friends CLI and spend-check units on the droplet, as the
# `hermes` user. Safe to re-run. Does NOT enable the timer. Run
# droplet/bootstrap-friends.sh (as root) once first for Caddy, swap and ufw.
set -euo pipefail

repo="$HOME/hermes-harness"
units="$HOME/.config/systemd/user"

(cd "$repo/friends" && /usr/local/bin/uv sync --no-dev -q)
echo "synced $repo/friends/.venv"

mkdir -p "$HOME/.local/bin"
ln -sfn "$repo/friends/.venv/bin/friends" "$HOME/.local/bin/friends"
echo "linked ~/.local/bin/friends -> $repo/friends/.venv/bin/friends"

mkdir -p "$units"
for f in friends-spend.service friends-spend.timer; do
  cp "$repo/droplet/units/$f" "$units/$f"
  echo "installed $units/$f"
done
systemctl --user daemon-reload
echo "daemon-reload done; friends-spend.timer is NOT enabled."
echo "To enable: systemctl --user enable --now friends-spend.timer"
