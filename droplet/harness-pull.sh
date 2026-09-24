#!/usr/bin/env bash
# Deploy hermes-harness master on the droplet. A broker change takes effect
# for the approvals bot at once and for the MCP server at the next gateway
# restart (the 04:00 reset, or `systemctl --user restart hermes-gateway`).
set -euo pipefail
cd "$HOME/hermes-harness"
git fetch -q origin
want=$(git rev-parse origin/master)
stamp="$HOME/.local/share/harness-deployed.sha"
# The stamp is written only after every deploy step succeeded, so a failed
# `uv sync` or restart is retried on the next tick instead of being forgotten.
[ "$(cat "$stamp" 2>/dev/null)" = "$want" ] && exit 0
git checkout -q master && git reset -q --hard origin/master
(cd broker && /usr/local/bin/uv sync --no-dev -q)
systemctl --user restart hermes-approvals
mkdir -p "$(dirname "$stamp")" && printf %s "$want" > "$stamp"
echo "deployed $(git rev-parse --short HEAD)"
