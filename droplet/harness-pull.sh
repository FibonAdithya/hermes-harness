#!/usr/bin/env bash
# Deploy hermes-harness master on the droplet. A broker change takes effect
# for the approvals bot at once and for the MCP server at the next gateway
# restart (the 04:00 reset, or `systemctl --user restart hermes-gateway`).
# The friends sync has its own stamp, so a failing friends sync never
# repeats the owner's reset and approvals restart on every tick.
set -euo pipefail
cd "$HOME/hermes-harness"
git fetch -q origin
want=$(git rev-parse origin/master)
stamp="$HOME/.local/share/harness-deployed.sha"
friends_stamp="$HOME/.local/share/friends-synced.sha"
mkdir -p "$(dirname "$stamp")"

# Owner steps. The stamp is written only after every step succeeded, so a failed
# `uv sync` or restart is retried on the next tick instead of being forgotten.
if [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
  git checkout -q master && git reset -q --hard origin/master
  (cd broker && /usr/local/bin/uv sync --no-dev -q)
  systemctl --user restart hermes-approvals
  printf %s "$want" > "$stamp"
  echo "deployed $(git rev-parse --short HEAD)"
fi

# Friends steps, independent of the owner's stamp. The checkout is at `want`
# here only if the owner steps ran now or earlier; a stale checkout is fixed first.
if [ "$(cat "$friends_stamp" 2>/dev/null)" != "$want" ]; then
  git checkout -q master && git reset -q --hard origin/master
  if (cd friends && /usr/local/bin/uv sync --no-dev -q); then
    printf %s "$want" > "$friends_stamp"
    echo "friends synced $(git rev-parse --short HEAD)"
  else
    echo "friends sync failed" >&2
    exit 1
  fi
fi

# Wiki worklist script: Hermes only runs --script files inside ~/.hermes/scripts,
# so it is a copy, refreshed here when the repo version changes. Absent files
# on either side are not an error.
wl_src="$HOME/hermes-harness/droplet/wiki-worklist.py"
wl_dst="$HOME/.hermes/scripts/wiki-worklist.py"
if [ -f "$wl_dst" ] && [ -f "$wl_src" ] && ! cmp -s "$wl_src" "$wl_dst"; then
  install -m 644 "$wl_src" "$wl_dst"
  echo "wiki worklist updated"
fi

# Already-read scripts for the email jobs: one copy per job, named
# already-read-<job id>.py (the job id is in the name; see runbooks/email-jobs.md).
# Each installed copy is refreshed when the repo version changes; no copy is created.
ar_src="$HOME/hermes-harness/droplet/already-read.py"
for ar_dst in "$HOME"/.hermes/scripts/already-read-*.py; do
  [ -f "$ar_dst" ] && [ -f "$ar_src" ] || continue
  if ! cmp -s "$ar_src" "$ar_dst"; then
    install -m 644 "$ar_src" "$ar_dst"
    echo "updated $(basename "$ar_dst")"
  fi
done

# Owner reminder helper: ~/.local/bin/remind-owner is a link into the checkout, so it
# follows master without a copy step. Made once here; a link already in place is left alone.
ro_src="$HOME/hermes-harness/droplet/remind-owner.sh"
ro_dst="$HOME/.local/bin/remind-owner"
if [ -f "$ro_src" ] && [ "$(readlink "$ro_dst" 2>/dev/null || true)" != "$ro_src" ]; then
  mkdir -p "$(dirname "$ro_dst")"
  ln -sfn "$ro_src" "$ro_dst"
  echo "linked remind-owner"
fi
