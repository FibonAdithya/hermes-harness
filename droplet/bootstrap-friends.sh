#!/usr/bin/env bash
# One-time droplet setup for hermes-friends. Run as root: sudo droplet/bootstrap-friends.sh
# Installs Caddy, writes the Caddyfile, creates /etc/caddy/friends.d, adds a
# 2 GB swapfile and opens ports 80/443. Idempotent. Leaves the Caddy admin API
# on its default localhost:2019 and does not enable or disable ufw.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "bootstrap-friends.sh must run as root (use sudo)" >&2
  exit 1
fi

changes=()

# 1. 2 GB swapfile
# Recreate a leftover file from an aborted run (inactive and not formatted as swap).
if [ -e /swapfile ] && ! swapon --show=NAME --noheadings | grep -qx /swapfile \
   && ! blkid -t TYPE=swap /swapfile >/dev/null 2>&1; then
  echo "/swapfile exists but is not valid swap; recreating"
  rm -f /swapfile
fi
if [ -e /swapfile ]; then
  echo "/swapfile already exists"
else
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  changes+=("created 2G /swapfile")
fi
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  swapon /swapfile
  changes+=("enabled /swapfile")
fi
if ! grep -qE '^/swapfile[[:space:]]' /etc/fstab; then
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  changes+=("added /swapfile to /etc/fstab")
fi

# 2. Firewall (ports open before Caddy's first ACME attempt)
ufw allow 80/tcp
ufw allow 443/tcp
changes+=("ufw allow 80/tcp, 443/tcp")

# 3. Caddy from the official apt repo
if command -v caddy >/dev/null 2>&1; then
  echo "caddy already installed"
else
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
  changes+=("installed caddy")
fi

# 4. Per-friend snippet directory, writable by hermes
install -d -o hermes -g hermes -m 755 /etc/caddy/friends.d

# 5. Caddyfile
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<'CADDY'
auth.fibonadithya.me {
	import /etc/caddy/friends.d/*.caddy
	handle {
		respond 404
	}
}
CADDY
caddyfile=/etc/caddy/Caddyfile
if [ -f "$caddyfile" ] && cmp -s "$tmp" "$caddyfile"; then
  echo "Caddyfile already up to date"
else
  # Back up any existing Caddyfile that differs from the target.
  if [ -f "$caddyfile" ]; then
    bak="$caddyfile.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "$caddyfile" "$bak"
    echo "backed up existing Caddyfile to $bak"
    changes+=("backed up Caddyfile to $bak")
  fi
  install -m 644 "$tmp" "$caddyfile"
  changes+=("wrote $caddyfile")
fi
caddy validate --config "$caddyfile"
if systemctl is-active --quiet caddy; then
  systemctl reload caddy
else
  systemctl restart caddy
fi

echo
echo "Summary of changes:"
if [ "${#changes[@]}" -eq 0 ]; then
  echo "  (none)"
else
  printf '  - %s\n' "${changes[@]}"
fi
echo
echo "Next steps, as the hermes user:"
echo "  droplet/install-friends.sh"
echo "  systemctl --user enable --now friends-spend.timer"
