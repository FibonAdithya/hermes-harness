#!/usr/bin/env bash
# Mirror every repo the owner owns into the agent's sandbox at /root/repos,
# read-only, every 30 minutes (hermes-repo-sync.timer). Invoked through the
# ~/.local/bin symlink; the git logic lives in repo-sync.sh next to this file.
#
# The sandbox runs with TERMINAL_DOCKER_NETWORK=false, so it never fetches
# anything itself. Git runs in a throwaway container over the same bind mount,
# the way the wiki sync does: that container has the host's network, the
# agent's sandbox still has none. The GitHub token (fine-grained, all repos,
# Metadata + Contents read-only) is mounted read-only at /gh, outside the
# agent's /root, so the agent never sees it. Setup: runbooks/dispatch.md.
set -euo pipefail

here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SBX="$HOME/.hermes/sandboxes/docker/default/home"
GH_CONFIG="$HOME/.hermes/gh-config"
LOCK="$HOME/.hermes/repo-sync.lock"

[ -d "$SBX" ] || { echo "FATAL: sandbox home missing at $SBX" >&2; exit 1; }
[ -f "$GH_CONFIG/hosts.yml" ] || {
  echo "FATAL: no gh credential at $GH_CONFIG/hosts.yml (see runbooks/dispatch.md)" >&2
  exit 1
}
grep -q 'oauth_token:' "$GH_CONFIG/hosts.yml" || {
  echo "FATAL: $GH_CONFIG/hosts.yml has no oauth_token; the token went to a keyring the" \
       "container cannot read. Log in again with --insecure-storage (see runbooks/dispatch.md)" >&2
  exit 1
}

# A slow pass must not overlap the next timer tick: concurrent clones over one
# bind mount corrupt each other.
exec 9>"$LOCK"
flock -n 9 || { echo "previous run still going; skipping"; exit 0; }

docker run --rm \
  -v "$SBX:/root" \
  -v "$GH_CONFIG:/gh:ro" \
  -v "$here/repo-sync.sh:/opt/repo-sync.sh:ro" \
  -e HOME=/root \
  -e GH_CONFIG_DIR=/gh \
  hermes-sandbox:latest \
  bash /opt/repo-sync.sh /root/repos
