import re
from dataclasses import dataclass
from pathlib import Path


def _parse_value(v: str) -> str:
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end != -1:
            return v[1:end]
    m = re.search(r"\s#", v)
    if m:
        v = v[: m.start()]
    return v.strip()


def read_env(path: Path) -> dict[str, str]:
    """Parse a .env file using python-dotenv's basic rules."""
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = _parse_value(v)
    return out


@dataclass
class OwnerEnv:
    chat_id: int
    google_client_id: str
    google_client_secret: str


def read_owner_env(root: Path) -> OwnerEnv:
    env = read_env(Path(root) / ".env")
    raw = env.get("TELEGRAM_ALLOWED_USERS", "")
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise ValueError("TELEGRAM_ALLOWED_USERS must hold exactly one positive integer")
    cid = env.get("GOOGLE_OAUTH_CLIENT_ID", "")
    sec = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not cid or not sec:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET missing in owner .env")
    return OwnerEnv(int(raw), cid, sec)


def normalize_email(s: str) -> str:
    """Canonical form for the owner-account comparison only (Gmail ignores dots and +tags)."""
    s = s.strip().lower()
    local, _, domain = s.rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def owner_google_emails(root: Path, home: Path) -> set[str]:
    """Lowercased Google accounts the owner uses; a friend must never share one.

    Sources: mcp_servers.google.env.USER_GOOGLE_EMAIL in <root>/config.yaml and
    every <email>.json under <home>/.google_workspace_mcp/credentials/.
    """
    import yaml

    out: set[str] = set()
    cfg = Path(root) / "config.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
            email = data["mcp_servers"]["google"]["env"]["USER_GOOGLE_EMAIL"]
        except (yaml.YAMLError, KeyError, TypeError):
            email = None
        if isinstance(email, str) and email.strip():
            out.add(email.strip().lower())
    creds = Path(home) / ".google_workspace_mcp" / "credentials"
    if creds.is_dir():
        for p in creds.glob("*.json"):
            if "@" in p.stem and p.name != "oauth_states.json":
                out.add(p.stem.lower())
    return out
