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
