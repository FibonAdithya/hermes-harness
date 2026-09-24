#!/usr/bin/env bash
# Run one Claude Code task in a throwaway container, in the foreground.
# The transient systemd unit that calls this is what detaches it.
#
# Usage: run-task.sh <night_dir> <owner/repo> <minutes>
set -euo pipefail
dir="$1"; repo="$2"; minutes="$3"

token_file="/etc/hermes-exec/github-token"
claude_creds="$HOME/.claude/.credentials.json"
[ -s "$token_file" ] || { echo "no github token at $token_file" >&2; exit 78; }
[ -s "$claude_creds" ] || { echo "no claude login at $claude_creds" >&2; exit 78; }

exec docker run --rm \
  --name "hermes-task-$(basename "$dir")" \
  --memory 6g --cpus 4 \
  -v "${dir}:/work" \
  -v "${claude_creds}:/home/runner/.claude/.credentials.json:ro" \
  -e "GH_TOKEN=$(cat "$token_file")" \
  -e "TASK_PROMPT_FILE=/work/prompt.txt" \
  -e "TASK_REPO=${repo}" \
  -e "TASK_ID=$(basename "$dir")" \
  -e "TASK_MINUTES=${minutes}" \
  hermes-exec:latest \
  bash -lc '
    set -euo pipefail
    git clone --depth 50 "https://github.com/${TASK_REPO}.git" /work/repo
    cd /work/repo
    git checkout -b "hermes/${TASK_ID}"
    claude -p "$(cat "${TASK_PROMPT_FILE}")" \
      --dangerously-skip-permissions \
      --output-format json \
      --append-system-prompt "You are working in a throwaway container on a scratch box. Stay inside /work/repo. Make the change, run the tests, commit, push the branch hermes/${TASK_ID}, and open a pull request with gh. Do not push to master or main and do not merge anything."
  '
