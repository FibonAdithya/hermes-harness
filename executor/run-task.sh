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

name="hermes-task-$(basename "$dir")"
# RuntimeMaxSec sends SIGTERM to this script; the container must not outlive it
# with the credentials still inside. `--init` gives the container a PID 1 that
# forwards signals, `timeout` bounds Claude Code itself, and the trap removes
# the container if either of those fails to.
term() { docker rm -f "$name" >/dev/null 2>&1; exit 143; }
trap term TERM INT
mkdir -p "${dir}/work"
docker run --rm --init \
  --name "$name" \
  --memory 6g --cpus 4 \
  -v "${dir}/work:/work" \
  -v "${claude_creds}:/home/runner/.claude/.credentials.json:ro" \
  -e "GH_TOKEN=$(cat "$token_file")" \
  -e "TASK_PROMPT_FILE=/work/prompt.txt" \
  -e "TASK_REPO=${repo}" \
  -e "TASK_ID=$(basename "$dir")" \
  -e "TASK_MINUTES=${minutes}" \
  hermes-exec:latest \
  bash -lc '
    set -euo pipefail
    # git over HTTPS needs a credential helper; gh wires one that reads GH_TOKEN.
    gh auth setup-git
    git clone --depth 50 "https://github.com/${TASK_REPO}.git" /work/repo
    cd /work/repo
    git checkout -b "hermes/${TASK_ID}"
    timeout "$((TASK_MINUTES * 60))" claude -p "$(cat "${TASK_PROMPT_FILE}")" \
      --dangerously-skip-permissions \
      --output-format json \
      --append-system-prompt "You are working unattended in a throwaway container on a scratch box; nobody can answer a question, so never stop to ask one. If you hit a choice, take the most reasonable option, say so in the pull request description, and keep going. Stay inside /work/repo. Make the change, run the tests, commit, push the branch hermes/${TASK_ID}, and open a pull request with gh. Always end by opening the pull request, even if a test could not run here. Do not push to master or main and do not merge anything."
  ' &
child=$!
wait "$child"
