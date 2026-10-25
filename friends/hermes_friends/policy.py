"""Constants and policy for friend profiles."""
from pathlib import Path

PUBLIC_HOST = "auth.fibonadithya.me"
PORTS = range(8101, 8200)
DEFAULT_CAP_USD = 5.0
WARN_FRACTION = 0.8
RUNAWAY_USD = 1.0
RUNAWAY_WINDOW_S = 3600
# ESTIMATE: Nous v4-pro *output* list price (tuner/tests/factory.py NOUS_V4_PRO),
# applied to every token. Friends run a cheaper model, so this over-counts.
# Not DeepSeek's direct $0.87: the droplet is billed through Nous.
FALLBACK_USD_PER_MTOK = 1.89
# Both ESTIMATE (design section 6).
MEM_PER_FRIEND_MB = 500
MEM_FLOOR_MB = 768
# Proposal; owner confirms in Task 8.
MAIN_MODEL = AUX_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_CADDY_DIR = Path("/etc/caddy/friends.d")
# Caddy runs as user `caddy` and must read these at boot; 600 would break it on reboot.
CADDY_FILE_MODE = 0o644


def gateway_unit(name: str) -> str:
    # Provisional until a droplet spike confirms the unit name.
    return f"hermes-gateway-{name}"
