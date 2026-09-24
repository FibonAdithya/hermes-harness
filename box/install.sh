#!/usr/bin/env bash
# Install or refresh the box side of hermes-harness. Idempotent. Runs as adi.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin"; units="$HOME/.config/systemd/user"
mkdir -p "$bin" "$units" "$HOME/nights"
ln -sfn "$here/dispatch" "$bin/dispatch"
ln -sfn "$here/run-night.sh" "$bin/run-night.sh"
ln -sfn "$here/../executor/run-task.sh" "$bin/run-task.sh"
chmod +x "$here/dispatch" "$here/run-night.sh" "$here"/verbs/* "$here/../executor/run-task.sh"
cp "$here"/units/*.service "$here"/units/*.timer "$units/"
[ -f "$HOME/nights/config.toml" ] || cp "$here/night-config.example.toml" "$HOME/nights/config.toml"
systemctl --user daemon-reload
# Timers are installed but never enabled here. Enabling is the owner's act after the dry night.
echo "installed verbs: $(ls "$here/verbs" | tr '\n' ' ')"
