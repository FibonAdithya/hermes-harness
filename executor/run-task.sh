#!/usr/bin/env bash
# Run one Claude Code task in a throwaway container. Detached: the caller gets
# a task id immediately, because a Telegram turn must not block for minutes.
#
# Usage: run-task.sh <task_id> <owner/repo> <prompt_file>
set -euo pipefail

task_id="$1"
repo="$2"
prompt_file="$3"

dir="/srv/hermes-tasks/${task_id}"
mkdir -p "${dir}"
cp "${prompt_file}" "${dir}/prompt.txt"
echo "running" > "${dir}/status"

# Credentials are injected for this run only; nothing is left on the host.
token_file="/etc/hermes-exec/github-token"
claude_creds="/etc/hermes-exec/claude-credentials.json"

(
  set +e
  docker run --rm \
    --name "hermes-task-${task_id}" \
    --memory 3g --cpus 2 \
    -v "${dir}:/work" \
    -v "${claude_creds}:/home/runner/.claude/.credentials.json:ro" \
    -e "GH_TOKEN=$(cat "${token_file}")" \
    -e "TASK_PROMPT_FILE=/work/prompt.txt" \
    -e "TASK_REPO=${repo}" \
    -e "TASK_ID=${task_id}" \
    hermes-exec:latest \
    bash -lc '
      set -euo pipefail
      git clone --depth 50 "https://github.com/${TASK_REPO}.git" /work/repo
      cd /work/repo
      git checkout -b "hermes/${TASK_ID}"
      claude -p "$(cat "${TASK_PROMPT_FILE}")" \
        --dangerously-skip-permissions \
        --output-format json \
        --append-system-prompt "You are working in a throwaway container on a scratch box. Stay inside /work/repo. Make the change, run the tests, commit, push the branch hermes/${TASK_ID}, and open a pull request with gh. Do not push to main and do not merge anything."
    ' >> "${dir}/log" 2>&1
  rc=$?
  echo "${rc}" > "${dir}/exit_code"
  if [ "${rc}" -eq 0 ]; then echo "done" > "${dir}/status"; else echo "failed" > "${dir}/status"; fi
) &

echo "${task_id}"
