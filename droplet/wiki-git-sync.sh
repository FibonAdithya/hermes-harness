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

# Generated files: only the todo commands and the nightly job write them, through GitHub.
# A local edit (including a deletion) would be snapshotted and pushed over the generated
# copy, so restore them from HEAD first. A local copy that HEAD lacks (untracked or newly
# staged) would be pushed as if generated, so remove it.
for p in agent-todos.jsonl agent-todos.md; do
  if ! git cat-file -e "HEAD:$p" 2>/dev/null; then
    if [ -e "$p" ] || [ -L "$p" ] || git ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
      git rm -q --cached --ignore-unmatch -- "$p"
      rm -f -- "$p"
      echo "removed local $p; only the todo commands write it" >&2
    fi
    continue
  fi
  if ! git diff --quiet HEAD -- "$p"; then
    git restore --staged --worktree --source=HEAD -- "$p"
    echo "discarded local edit to $p" >&2
  fi
done

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
