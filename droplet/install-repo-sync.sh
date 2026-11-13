#!/usr/bin/env bash
# Install the repo mirror sync on the droplet, as the `hermes` user. Safe to re-run.
# Does NOT enable the timer: the owner installs the token first and the first pass
# is run by hand (runbooks/dispatch.md, "The repo mirrors").
set -euo pipefail

repo="$HOME/hermes-harness"
units="$HOME/.config/systemd/user"

mkdir -p "$HOME/.local/bin"
ln -sfn "$repo/droplet/hermes-repo-sync.sh" "$HOME/.local/bin/hermes-repo-sync.sh"
echo "linked ~/.local/bin/hermes-repo-sync.sh -> $repo/droplet/hermes-repo-sync.sh"

mkdir -p "$units"
for f in hermes-repo-sync.service hermes-repo-sync.timer; do
  cp "$repo/droplet/units/$f" "$units/$f"
  echo "installed $units/$f"
done
systemctl --user daemon-reload
echo "daemon-reload done; hermes-repo-sync.timer is NOT enabled"
