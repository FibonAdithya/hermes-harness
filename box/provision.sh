#!/usr/bin/env bash
# Provision the TIG box. Run once as root over SSH; safe to re-run.
# Everything after the user exists runs as `adi` via `run_as_adi`.
set -euo pipefail

ADI_HOME=/home/adi
HERDR_VERSION="${HERDR_VERSION:-0.7.5}"

log() { printf '== %s\n' "$*"; }

# cd ~ first: uv and npm look for config in the current directory and its
# parents, and root's cwd (/root) is unreadable to adi.
run_as_adi() { sudo -u adi -H bash -lc "cd ~ && $*"; }

# --- packages (root) --------------------------------------------------------
log "apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential ca-certificates curl git gh jq \
  docker.io ripgrep unzip unattended-upgrades tmux python3 python3-venv
systemctl enable --now docker >/dev/null

# --- node 24 from NodeSource (root) -----------------------------------------
if ! command -v node >/dev/null || ! node --version | grep -q '^v24\.'; then
  log "node 24"
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash - >/dev/null
  apt-get install -y -qq nodejs
fi

# --- user (root) -------------------------------------------------------------
if ! id adi >/dev/null 2>&1; then
  log "user adi"
  adduser --disabled-password --gecos "" adi
fi
usermod -aG docker,sudo adi
install -d -o adi -g adi -m 0700 "$ADI_HOME/.ssh"
# The owner's own key (from the laptop) for herdr --remote and ssh.
if [ -n "${OWNER_PUBKEY:-}" ]; then
  grep -qxF "$OWNER_PUBKEY" "$ADI_HOME/.ssh/authorized_keys" 2>/dev/null \
    || echo "$OWNER_PUBKEY" >> "$ADI_HOME/.ssh/authorized_keys"
  chown adi:adi "$ADI_HOME/.ssh/authorized_keys"; chmod 0600 "$ADI_HOME/.ssh/authorized_keys"
fi
loginctl enable-linger adi
install -d -o adi -g adi -m 0700 /etc/hermes-exec

# --- per-user tooling (adi) --------------------------------------------------
log "directories"
run_as_adi 'mkdir -p ~/nights ~/TIG ~/.local/bin ~/.config/systemd/user'

log "uv"
run_as_adi 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'
run_as_adi 'uv python install 3.10 3.11 >/dev/null'

log "herdr"
# Pinned to the laptop's version from the GitHub release, not herdr.dev/install.sh,
# which always installs the latest manifest. Client and server must match.
HERDR_URL="https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-$(uname -m)"
run_as_adi "command -v herdr >/dev/null && herdr --version | grep -qF '$HERDR_VERSION' \
  || { curl -fsSL '$HERDR_URL' -o ~/.local/bin/herdr.tmp && chmod +x ~/.local/bin/herdr.tmp \
       && ~/.local/bin/herdr.tmp --version | grep -qF '$HERDR_VERSION' && mv ~/.local/bin/herdr.tmp ~/.local/bin/herdr; }"

# npm -g as a non-root user needs a user prefix; NodeSource's node lives in /usr/lib.
run_as_adi 'npm config get prefix | grep -q "$HOME/.npm-global" || { mkdir -p ~/.npm-global && npm config set prefix ~/.npm-global; }'
run_as_adi 'grep -q npm-global ~/.profile || echo "export PATH=\$HOME/.npm-global/bin:\$HOME/.local/bin:\$PATH" >> ~/.profile'

log "claude and codex"
run_as_adi 'command -v claude >/dev/null || npm install -g @anthropic-ai/claude-code >/dev/null'
run_as_adi 'command -v codex  >/dev/null || npm install -g @openai/codex >/dev/null'

log "done"
cat <<'EOF'
Provisioning complete. Interactive steps still needed, each over `ssh -t tig-adi`:
  claude          # then /login
  codex login
  gh auth login
Then copy the Claude guards:  scp -r ~/.claude/{CLAUDE.md,settings.json,hooks} tig-adi:~/.claude/
EOF
