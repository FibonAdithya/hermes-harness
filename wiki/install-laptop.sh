#!/usr/bin/env bash
# Install hermes-wiki on the laptop: CLI, wiki clone, config, MCP server, SessionEnd hook.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="$HOME/.local/bin"
wiki_dir="$HOME/.local/share/hermes-wiki"
conf_dir="$HOME/.config/hermes-wiki"

if command -v uv >/dev/null 2>&1; then
  uv="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  uv="$HOME/.local/bin/uv"
else
  echo "install-laptop: uv not found on PATH or in \$HOME/.local/bin" >&2
  exit 1
fi
gh_bin="$(command -v gh || true)"
claude_bin="$(command -v claude || true)"
if [ -z "$gh_bin" ]; then echo "install-laptop: gh not found on PATH (needed to clone and publish notes)" >&2; exit 1; fi
if [ -z "$claude_bin" ]; then echo "install-laptop: claude not found on PATH (needed for the MCP server and extractor)" >&2; exit 1; fi

(cd "$here" && "$uv" sync --no-dev -q)
mkdir -p "$bin_dir"
ln -sfn "$here/.venv/bin/hermes-wiki" "$bin_dir/hermes-wiki"

if [ ! -d "$wiki_dir" ]; then
  mkdir -p "$(dirname "$wiki_dir")"
  "$gh_bin" repo clone FibonAdithya/hermes-wiki "$wiki_dir"
fi

if [ ! -f "$conf_dir/config.json" ]; then
  mkdir -p "$conf_dir"
  HOME_DIR="$HOME" GH_BIN="$gh_bin" CLAUDE_BIN="$claude_bin" python3 - "$conf_dir/config.json" <<'PY'
import json, os, sys
home = os.environ["HOME_DIR"]
cfg = {
    "root": f"{home}/.local/share/hermes-wiki",
    "source": "claude-laptop",
    "pull": True,
    "public_only": False,
    "bin": f"{home}/.local/bin/hermes-wiki",
    "gh": os.environ["GH_BIN"],
    "claude": os.environ["CLAUDE_BIN"],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write(json.dumps(cfg, indent=2) + "\n")
PY
fi

"$claude_bin" mcp remove --scope user wiki >/dev/null 2>&1 || true
"$claude_bin" mcp add --scope user wiki -- "$bin_dir/hermes-wiki" mcp

"$bin_dir/hermes-wiki" install-hook --settings "$HOME/.claude/settings.json" \
  --command "$bin_dir/hermes-wiki session-end"

if [ ! -f "$conf_dir/repos.toml" ]; then
  mkdir -p "$conf_dir"
  cat > "$conf_dir/repos.toml" <<'TOML'
# Overrides for `hermes-wiki repos`; see the "Repo registry" section of runbooks/wiki.md.
# List private repos that no private wiki page names, e.g. private = ["local:<dir>"].
private = []
TOML
fi

unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
cp "$here/units/hermes-wiki-repos.service" "$here/units/hermes-wiki-repos.timer" "$unit_dir/"
systemctl --user daemon-reload || echo "install-laptop: daemon-reload failed; run it after login" >&2

echo "next: hermes-wiki import-claude-memory --projects ~/.claude/projects --dry-run"
echo "repo registry: dry run, then turn on the timer during rollout (runbooks/wiki.md, Repo registry)"
