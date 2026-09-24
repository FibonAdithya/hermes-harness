#!/usr/bin/env bash
# Install or refresh the box side of hermes-harness. Idempotent. Runs as adi.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin"; units="$HOME/.config/systemd/user"
mkdir -p "$bin" "$units" "$HOME/nights"
ln -sfn "$here/dispatch" "$bin/dispatch"
ln -sfn "$here/run-night.sh" "$bin/run-night.sh"
ln -sfn "$here/../executor/run-task.sh" "$bin/run-task.sh"
chmod +x "$here/dispatch" "$here/run-night.sh" "$here/../executor/run-task.sh"
cp "$here"/units/*.service "$here"/units/*.timer "$units/"
[ -f "$HOME/nights/config.toml" ] || cp "$here/night-config.example.toml" "$HOME/nights/config.toml"
systemctl --user daemon-reload
# The executor image follows the Dockerfile: rebuild when its content changed
# since the last install, so a merged Dockerfile change deploys like a verb.
stamp="$HOME/.local/share/hermes-exec.dockerfile.sha"
want="$(sha256sum "$here/../executor/Dockerfile" | cut -d" " -f1)"
if [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
  if docker build -q -t hermes-exec:latest -f "$here/../executor/Dockerfile" "$here/../executor" >/dev/null; then
    mkdir -p "$(dirname "$stamp")"; printf '%s' "$want" > "$stamp"; echo "rebuilt hermes-exec:latest"
  else
    echo "docker build of hermes-exec failed" >&2; exit 1
  fi
fi
# Timers are installed but never enabled here. Enabling is the owner's act after the dry night.
echo "installed verbs: $(cd "$here/verbs" && printf "%s " *)"
