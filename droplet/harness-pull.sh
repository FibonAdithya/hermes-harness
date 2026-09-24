#!/usr/bin/env bash
# Deploy hermes-harness master on the droplet. A broker change takes effect
# for the approvals bot at once and for the MCP server at the next gateway
# restart (the 04:00 reset, or `systemctl --user restart hermes-gateway`).
set -euo pipefail
cd "$HOME/hermes-harness"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)" ] && exit 0
git checkout -q master && git reset -q --hard origin/master
(cd broker && /usr/local/bin/uv sync --no-dev -q)
systemctl --user restart hermes-approvals
echo "deployed $(git rev-parse --short HEAD)"
