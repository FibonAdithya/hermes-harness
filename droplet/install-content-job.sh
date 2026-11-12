#!/usr/bin/env bash
# Install the content-job skill on the droplet, as the `hermes` user. Safe to
# re-run. It copies files only and does not touch Hermes config or cron.
# Manual steps are in runbooks/content.md.
set -euo pipefail

repo="$HOME/hermes-harness"
skills="$HOME/.hermes/skills/content"

if [ ! -d "$repo/droplet/skills/content-job" ]; then
  echo "FATAL: $repo/droplet/skills/content-job is missing; pull a hermes-harness checkout that includes it" >&2
  exit 1
fi

mkdir -p "$skills"
rm -rf "${skills:?}/content-job"
cp -r "$repo/droplet/skills/content-job" "$skills/content-job"
echo "installed $skills/content-job"

echo "Remaining manual steps: see $repo/runbooks/content.md"
