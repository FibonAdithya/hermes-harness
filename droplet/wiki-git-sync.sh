#!/usr/bin/env bash
# Snapshot the wiki working tree, rebase onto origin/main, push. Runs inside the
# hermes-sandbox container (git credentials live in its home). Argument: repo dir.
# Never force-pushes; on a rebase conflict it aborts and leaves the snapshot
# commit local so the next run can retry once the conflict is resolved.
set -euo pipefail

cd "${1:?usage: wiki-git-sync.sh <repo dir>}"

git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "wiki: auto-snapshot $(date -Iseconds)"
fi

if ! git pull -q --rebase --autostash origin main; then
  git rebase --abort 2>/dev/null || true
  echo "FATAL: rebase conflict, not pushed" >&2
  exit 1
fi

if [ -n "$(git rev-list origin/main..HEAD)" ]; then
  git push -q origin HEAD:main
  echo "pushed"
else
  echo "no changes"
fi
