#!/usr/bin/env bash
# Install the Hermes tuner on the droplet, as the `hermes` user. Safe to re-run.
# Does NOT enable the timer, and does NOT touch Hermes config (the owner adds
# hermes-tuner to plugins.enabled by hand; see runbooks/tuner.md).
set -euo pipefail

repo="$HOME/hermes-harness"
units="$HOME/.config/systemd/user"

(cd "$repo/tuner" && /usr/local/bin/uv sync --no-dev -q)
echo "synced $repo/tuner/.venv"

mkdir -p "$HOME/.hermes/plugins"
ln -sfn "$repo/tuner/plugin/hermes-tuner" "$HOME/.hermes/plugins/hermes-tuner"
echo "linked ~/.hermes/plugins/hermes-tuner -> $repo/tuner/plugin/hermes-tuner"

mkdir -p -m 700 "$HOME/.hermes/tuner"
echo "ensured ~/.hermes/tuner (mode 700)"

mkdir -p "$units"
for f in hermes-tuner.service hermes-tuner.timer; do
  cp "$repo/droplet/units/$f" "$units/$f"
  echo "installed $units/$f"
done
systemctl --user daemon-reload
echo "daemon-reload done; hermes-tuner.timer is NOT enabled (the owner enables it)"
