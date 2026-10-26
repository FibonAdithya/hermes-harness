#!/usr/bin/env bash
# Snapshot the wiki working tree, rebase onto origin/main, push. Runs inside the
# hermes-sandbox container (git credentials live in its home). Argument: repo dir.
# Never force-pushes; on a rebase conflict it aborts and leaves the snapshot
# commit local so the next run can retry once the conflict is resolved.
set -euo pipefail

cd "${1:?usage: wiki-git-sync.sh <repo dir>}"

in_progress() {
  local p
  for p in rebase-merge rebase-apply MERGE_HEAD; do
    [ -e "$(git rev-parse --git-path "$p")" ] && return 0
  done
  return 1
}

# Someone (the owner, or a killed run) is mid-rebase or mid-merge: committing now would
# snapshot conflict markers, and aborting would throw the snapshot away. Leave it alone.
if in_progress; then
  echo "FATAL: rebase or merge in progress; finish or abort it by hand, not pushed" >&2
  exit 1
fi

git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "wiki: auto-snapshot $(date -Iseconds)"
fi

if ! git pull -q --rebase --autostash origin main; then
  if in_progress; then
    git rebase --abort 2>/dev/null || true
    echo "FATAL: rebase conflict, not pushed" >&2
  else
    echo "FATAL: pull failed (see git's error above), not pushed" >&2
  fi
  exit 1
fi

if [ -n "$(git rev-list origin/main..HEAD)" ]; then
  git push -q origin HEAD:main
  echo "pushed"
else
  echo "no changes"
fi
