#!/usr/bin/env bash
# remind-owner WHEN NAME < message
# Sends the owner a one-time Telegram message through a no-agent Hermes cron job
# (no model call). Hermes removes the job after it runs (repeat limit 1); the job's
# script removes its own files. Claude Code sessions use this for reminders: see
# the "Reminders for me" section of the owner's global CLAUDE.md.
# Installed as ~/.local/bin/remind-owner, a link that harness-pull.sh keeps.
#
# WHEN: "30m", "2h", or an ISO time with offset, e.g. 2026-11-26T09:00+00:00.
# REMIND_DELIVER overrides the delivery target (default: the owner's Telegram chat).
set -euo pipefail
usage="usage: remind-owner WHEN NAME < message"
when="${1:?$usage}"
name="${2:?$usage}"
deliver="${REMIND_DELIVER:-telegram:8887384436}"
msg=$(cat)
if [ -z "${msg//[[:space:]]/}" ]; then
  echo "remind-owner: empty message" >&2
  exit 2
fi
# Hermes only runs --script files from this directory.
dir="$HOME/.hermes/scripts"
id="remind-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
printf '%s\n' "$msg" > "$dir/$id.txt"
cat > "$dir/$id.sh" <<EOF
#!/bin/bash
cat "$dir/$id.txt"
rm -f "$dir/$id.txt" "$dir/$id.sh"
EOF
if ! hermes cron create --name "reminder: $name" --deliver "$deliver" \
    --repeat 1 --no-agent --script "$id.sh" "$when"; then
  rm -f "$dir/$id.txt" "$dir/$id.sh"
  echo "remind-owner: hermes cron create failed; no reminder set" >&2
  exit 1
fi
