#!/usr/bin/env bash
# remind-owner WHEN NAME < message
# remind-owner --cancel JOB_ID
# remind-owner --prune
# Sends the owner a one-time Telegram message through a no-agent Hermes cron job
# (no model call). Hermes removes the job after it runs (repeat limit 1); the job's
# script removes its own files. Claude Code sessions use this for reminders: see
# the "Reminders for me" section of the owner's global CLAUDE.md.
# Installed as ~/.local/bin/remind-owner, a link that harness-pull.sh keeps.
#
# WHEN: "30m", "2h", or an ISO time with offset, e.g. 2026-11-26T09:00+00:00.
# --cancel removes a reminder job (refuses any other job) and then its script and
# message files. --prune deletes remind-*.sh/.txt pairs that no live job uses and
# whose .sh is more than 10 minutes old. Both exit 1 if jobs.json is unreadable.
# REMIND_DELIVER overrides the delivery target (default: the owner's Telegram chat).
set -euo pipefail
usage="usage: remind-owner WHEN NAME < message | --cancel JOB_ID | --prune"
case "${1:-}" in
  --cancel|--prune)
    if [ "$1" = "--cancel" ] && [ -z "${2:-}" ]; then
      echo "$usage" >&2
      exit 2
    fi
    exec python3 - "$@" <<'PY'
import json
import os
import re
import subprocess
import sys
import time

home = os.environ["HOME"]
scripts = os.path.join(home, ".hermes", "scripts")
pattern = re.compile(r"remind-[0-9]{14}-[0-9]+\.sh")


def fail(msg, code=1):
    print("remind-owner: " + msg, file=sys.stderr)
    sys.exit(code)


try:
    with open(os.path.join(home, ".hermes", "cron", "jobs.json")) as f:
        data = json.load(f)
except (OSError, ValueError) as e:
    fail("cannot read jobs.json: %s" % e)
if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
    fail("jobs.json has an unexpected shape")
jobs = data["jobs"]
if not all(isinstance(j, dict) for j in jobs):
    fail("jobs.json has an unexpected shape")


def base_of(job):
    s = job.get("script")
    return os.path.basename(s) if isinstance(s, str) else None


def remove_pair(base):
    removed = []
    stem = base[: -len(".sh")]
    for ext in (".sh", ".txt"):
        path = os.path.join(scripts, stem + ext)
        try:
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            pass
    return removed


if sys.argv[1] == "--cancel":
    job_id = sys.argv[2]
    matches = [j for j in jobs if j.get("id") == job_id]
    if not matches:
        fail("no job with id %s" % job_id)
    base = base_of(matches[0])
    if base is None or not pattern.fullmatch(base):
        fail("job %s is not a reminder job" % job_id)
    if subprocess.run(["hermes", "cron", "remove", job_id]).returncode != 0:
        fail("hermes cron remove failed; files kept")
    remove_pair(base)
    sys.exit(0)

live = {b for b in (base_of(j) for j in jobs) if b}
try:
    names = sorted(os.listdir(scripts))
except OSError as e:
    fail("cannot list scripts dir: %s" % e)
for name in names:
    if not pattern.fullmatch(name) or name in live:
        continue
    try:
        age = time.time() - os.stat(os.path.join(scripts, name)).st_mtime
    except FileNotFoundError:
        continue
    if age > 600:
        for path in remove_pair(name):
            print(path)
PY
    ;;
esac
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
