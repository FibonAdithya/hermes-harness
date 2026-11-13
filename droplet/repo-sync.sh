#!/usr/bin/env bash
# Clone or refresh a mirror of every repo the owner owns. Runs inside a throwaway
# hermes-sandbox container started by hermes-repo-sync.sh, which has the host's
# network (the agent's sandbox has none). Argument: the mirror directory.
#
# gh lists the repos and also answers git's credential requests, both from the
# read-only token mounted at $GH_CONFIG_DIR. The sandbox home's own credential
# store is switched off for every git call here: it is the agent's, git would
# offer whatever it holds to GitHub, and on a rejection git erases the entry
# from the agent's home.
set -euo pipefail

mirrors="${1:?usage: repo-sync.sh <mirror dir>}"

export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1='!gh auth git-credential'
export GIT_TERMINAL_PROMPT=0 GH_NO_UPDATE_NOTIFIER=1

mkdir -p "$mirrors"

if ! repos=$(gh api --paginate "user/repos?per_page=100&affiliation=owner" -q ".[].full_name" 2>&1); then
  echo "FATAL: repo listing failed: ${repos}" >&2
  exit 1
fi
[ -n "${repos}" ] || { echo "FATAL: repo listing came back empty" >&2; exit 1; }

failed=0 count=0 seen=""
for full in ${repos}; do
  name="${full#*/}"
  dir="${mirrors}/${name}"
  seen="${seen} ${name}"
  count=$((count + 1))

  if [ ! -d "${dir}/.git" ]; then
    # Full clones only. A --depth or --filter clone would need the network the
    # moment the agent touched an unfetched object, and it has none.
    if git clone --quiet "https://github.com/${full}.git" "${dir}"; then
      echo "cloned ${full}"
    else
      echo "FAILED clone ${full}" >&2
      failed=$((failed + 1))
    fi
    continue
  fi

  if ! git -C "${dir}" fetch --prune --quiet origin; then
    echo "FAILED fetch ${full}" >&2
    failed=$((failed + 1))
    continue
  fi

  git -C "${dir}" remote set-head origin --auto >/dev/null 2>&1 || true
  head=$(git -C "${dir}" symbolic-ref --quiet --short refs/remotes/origin/HEAD || true)
  if [ -z "${head}" ]; then
    # An empty repo has no default branch yet. Not a failure.
    echo "empty ${full}"
    continue
  fi

  # Mirrors are replicas: anything the agent wrote here is discarded.
  if ! git -C "${dir}" reset --hard --quiet "${head}" || ! git -C "${dir}" clean -fdxq; then
    echo "FAILED reset ${full}" >&2
    failed=$((failed + 1))
  fi
done

# A renamed or deleted repo leaves a mirror behind. Report it, do not delete: a
# listing that silently loses a repo must not silently destroy the mirror the
# agent was reading.
for dir in "${mirrors}"/*/; do
  [ -d "${dir}" ] || continue
  n="$(basename "${dir}")"
  case " ${seen} " in
    *" ${n} "*) ;;
    *) echo "STALE mirror: ${n} (not in listing)" ;;
  esac
done

echo "listed ${count} repos, ${failed} failed"
[ "${failed}" -eq 0 ]
