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

export GH_CONFIG_DIR="$HOME/.hermes/gh-config"
export PYTHONPATH="$HARNESS/wiki"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

"$DOCKER" run --rm --network=none -v "$SBX_WIKI:/wiki:ro" hermes-sandbox:latest \
  sh -c 'if [ -e /wiki/.todo-suggestions.jsonl ]; then cat /wiki/.todo-suggestions.jsonl; fi' >"$tmp"

"$PYTHON3" -m hermes_wiki todo nightly \
  --config "$HOME/.config/hermes-wiki/config.json" \
  --suggestions "$tmp" \
  --evidence "$HOME/.hermes/state/todo-evidence.json" \
  --state "$HOME/.hermes/state/todo-nightly.json"

"$DOCKER" run --rm --network=none -v "$SBX_WIKI:/wiki" hermes-sandbox:latest \
  rm -f /wiki/.todo-suggestions.jsonl
