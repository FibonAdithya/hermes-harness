#!/usr/bin/env bash
# Nightly agent-todos job, run on the droplet host as the `hermes` user at 00:45,
# before the 01:00 wiki curator. Reads the curator's suggestions out of the
# root-owned sandbox checkout, runs `hermes-wiki todo nightly`, and deletes the
# suggestions file only after python exits 0. A failed read aborts (set -e) before
# python runs and before any delete.
set -euo pipefail

SBX_WIKI="${SBX_WIKI:-$HOME/.hermes/sandboxes/docker/default/home/hermes-wiki}"
DOCKER="${DOCKER:-docker}"
PYTHON3="${PYTHON3:-python3}"
HARNESS="${HARNESS:-$HOME/hermes-harness}"

# Two tokens: gh-config (all repos, read-only, shared with the repo mirrors) reads PR and
# issue state; gh-wiki (hermes-wiki only, contents write) makes the one wiki commit.
export GH_CONFIG_DIR="$HOME/.hermes/gh-config"
export HERMES_WIKI_TREE_GH_CONFIG_DIR="$HOME/.hermes/gh-wiki"
export PYTHONPATH="$HARNESS/wiki"

grep -q 'oauth_token:' "$HERMES_WIKI_TREE_GH_CONFIG_DIR/hosts.yml" 2>/dev/null || {
  echo "FATAL: no wiki write token in $HERMES_WIKI_TREE_GH_CONFIG_DIR/hosts.yml; log in there" \
       "with --insecure-storage (runbooks/wiki.md, Droplet tokens)" >&2
  exit 1
}

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

"$DOCKER" run --rm --network=none -v "$SBX_WIKI:/wiki:ro" hermes-sandbox:latest \
  sh -c 'if [ -e /wiki/.todo-suggestions.jsonl ]; then cat /wiki/.todo-suggestions.jsonl; fi' >"$tmp"

want="$(sha256sum "$tmp" | cut -d' ' -f1)"

"$PYTHON3" -m hermes_wiki todo nightly \
  --config "$HOME/.config/hermes-wiki/config.json" \
  --suggestions "$tmp" \
  --evidence "$HOME/.hermes/state/todo-evidence.json" \
  --state "$HOME/.hermes/state/todo-nightly.json"

# Delete only the file that was read: the curator may have written a new one since.
"$DOCKER" run --rm --network=none -v "$SBX_WIKI:/wiki" hermes-sandbox:latest sh -c 'f=/wiki/.todo-suggestions.jsonl; if [ -e "$f" ] && [ "$(sha256sum <"$f" | cut -d" " -f1)" = "$1" ]; then rm -f "$f"; elif [ -e "$f" ]; then echo "suggestions changed since read; kept" >&2; fi' sh "$want"
