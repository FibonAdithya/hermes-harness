from dataclasses import dataclass
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
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
