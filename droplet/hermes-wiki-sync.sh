#!/usr/bin/env bash
# Pull, commit and push the Hermes wiki every 15 minutes (hermes-wiki-sync.timer).
# Invoked through the ~/.local/bin symlink; the git logic lives in
# wiki-git-sync.sh next to this file and runs inside the sandbox container.
set -euo pipefail

here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SBX="$HOME/.hermes/sandboxes/docker/default/home"

if [ ! -d "$SBX/hermes-wiki/.git" ]; then
  echo "FATAL: wiki repo missing at $SBX/hermes-wiki" >&2
  exit 1
fi

docker run --rm \
  -v "$SBX:/root" \
  -v "$here/wiki-git-sync.sh:/opt/wiki-git-sync.sh:ro" \
  -e HOME=/root \
  hermes-sandbox:latest \
  bash /opt/wiki-git-sync.sh /root/hermes-wiki
