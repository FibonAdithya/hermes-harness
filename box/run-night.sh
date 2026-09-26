#!/usr/bin/env bash
# run-night.sh <night_dir> <command...>
# Writes status, log and exit_code around one command.
#
# SIGINT (the pause_talos verb) is forwarded to the command as SIGINT, its clean
# stop, and the night ends `paused`. SIGTERM (what RuntimeMaxSec sends) ends it
# `killed`: forwarded as SIGINT when NIGHT_STOP_SIGNAL=INT (Talos nights, whose
# SIGINT cancels the bench job in flight), otherwise as SIGTERM at once.
set -uo pipefail
dir="$1"; shift
mkdir -p "$dir"
echo running > "$dir/status"
stopped_as=""
term() { echo killed > "$dir/status"; echo 143 > "$dir/exit_code"; kill -TERM "$child" 2>/dev/null; wait "$child"; exit 143; }
on_int() { stopped_as=${stopped_as:-paused}; kill -INT "$child" 2>/dev/null; }
on_term() {
  if [ "${NIGHT_STOP_SIGNAL:-}" = INT ]; then stopped_as=killed; kill -INT "$child" 2>/dev/null; else term; fi
}
trap on_int INT
trap on_term TERM
"$@" >> "$dir/log" 2>&1 &
child=$!
# A trapped signal interrupts `wait` with a status above 128 while the child
# still runs; wait again until the child itself has exited.
while :; do
  wait "$child"; rc=$?
  kill -0 "$child" 2>/dev/null || break
done
echo "$rc" > "$dir/exit_code"
if [ -n "$stopped_as" ]; then echo "$stopped_as" > "$dir/status"
elif [ "$rc" -eq 0 ]; then echo "done" > "$dir/status"; else echo "failed" > "$dir/status"; fi
exit "$rc"
