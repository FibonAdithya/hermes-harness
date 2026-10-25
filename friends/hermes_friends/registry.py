import json
from pathlib import Path

from .model import Friend
from .owner import read_env
from .policy import PORTS


def profile_dir(root: Path, name: str) -> Path:
    return Path(root) / "profiles" / name


def list_friends(root: Path) -> list[Friend]:
    base = Path(root) / "profiles"
    if not base.is_dir():
        return []
    friends = [Friend.load(d) for d in base.iterdir() if (d / "friend.json").is_file()]
    return sorted(friends, key=lambda f: f.name)


def allocate_port(friends: list[Friend]) -> int:
    used = {f.port for f in friends}
    for p in PORTS:
        if p not in used:
            return p
    raise RuntimeError("no free port in range")


def bot_token_in_use(root: Path, token: str) -> bool:
    root = Path(root)
    envs = [root / ".env"]
    base = root / "profiles"
    if base.is_dir():
        envs += [d / ".env" for d in base.iterdir()]
    for e in envs:
        if e.is_file() and read_env(e).get("TELEGRAM_BOT_TOKEN") == token:
            return True
    broker = root / "broker" / "broker.json"
    if broker.is_file():
        if json.loads(broker.read_text()).get("approvals_bot_token") == token:
            return True
    return False
