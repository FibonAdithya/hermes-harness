#!/usr/bin/env bash
# Install the wiki sync script, worklist script and delegate skill on the
# droplet, as the `hermes` user. Safe to re-run. File installs only: it does not
# touch Hermes config or cron. Manual steps are in runbooks/wiki.md.
set -euo pipefail

repo="$HOME/hermes-harness"
bin="$HOME/.local/bin"
scripts="$HOME/.hermes/scripts"
skills="$HOME/.hermes/skills/wiki"

for f in droplet/hermes-wiki-sync.sh droplet/wiki-git-sync.sh \
         droplet/wiki-worklist.py droplet/skills/delegate-with-context \
         droplet/todo-nightly.sh droplet/hermes-wiki-droplet.json \
         droplet/units/todo-nightly.service droplet/units/todo-nightly.timer; do
  if [ ! -e "$repo/$f" ]; then
    echo "FATAL: $repo/$f is missing; pull a hermes-harness checkout that includes it" >&2
    exit 1
  fi
done

mkdir -p "$bin" "$scripts" "$skills"

target="$bin/hermes-wiki-sync.sh"
if [ -f "$target" ] && [ ! -L "$target" ] && [ ! -e "$target.pre-wiki" ]; then
  mv "$target" "$target.pre-wiki"
  echo "backed up $target to $target.pre-wiki"
fi
ln -sfn "$repo/droplet/hermes-wiki-sync.sh" "$target"
echo "linked $target -> $repo/droplet/hermes-wiki-sync.sh"

# A copy, not a symlink: Hermes refuses --script paths outside ~/.hermes/scripts.
install -m 644 "$repo/droplet/wiki-worklist.py" "$scripts/wiki-worklist.py"
echo "installed $scripts/wiki-worklist.py"

rm -rf "${skills:?}/delegate-with-context"
cp -r "$repo/droplet/skills/delegate-with-context" "$skills/delegate-with-context"
echo "installed $skills/delegate-with-context"

ln -sfn "$repo/droplet/todo-nightly.sh" "$bin/todo-nightly.sh"
echo "linked $bin/todo-nightly.sh -> $repo/droplet/todo-nightly.sh"

mkdir -p "$HOME/.config/hermes-wiki" "$HOME/.hermes/state"
cfg="$HOME/.config/hermes-wiki/config.json"
if [ -e "$cfg" ]; then
  echo "kept existing $cfg"
else
  cp "$repo/droplet/hermes-wiki-droplet.json" "$cfg"
  echo "installed $cfg from droplet/hermes-wiki-droplet.json"
fi

units="$HOME/.config/systemd/user"
mkdir -p "$units"
for f in todo-nightly.service todo-nightly.timer; do
  cp "$repo/droplet/units/$f" "$units/$f"
  echo "installed $units/$f"
done
systemctl --user daemon-reload
echo "daemon-reload done; todo-nightly.timer is NOT enabled"

echo "Remaining manual steps: see $repo/runbooks/wiki.md"
