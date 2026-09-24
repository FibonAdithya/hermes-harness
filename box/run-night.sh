#!/usr/bin/env bash
# run-night.sh <night_dir> <command...>
# Writes status, log and exit_code around one command. SIGTERM (what
# RuntimeMaxSec sends) becomes status=killed, not a silent "running" forever.
set -uo pipefail
dir="$1"; shift
mkdir -p "$dir"
echo running > "$dir/status"
term() { echo killed > "$dir/status"; echo 143 > "$dir/exit_code"; kill -TERM "$child" 2>/dev/null; wait "$child"; exit 143; }
trap term TERM INT
"$@" >> "$dir/log" 2>&1 &
child=$!
wait "$child"; rc=$?
echo "$rc" > "$dir/exit_code"
if [ "$rc" -eq 0 ]; then echo done > "$dir/status"; else echo failed > "$dir/status"; fi
exit "$rc"
