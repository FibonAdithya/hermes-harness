import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Friend:
    name: str
    telegram_id: int
    email: str
    tz: str
    port: int
    cap_usd: float
    activated: bool

    @classmethod
    def load(cls, profile_dir: Path) -> "Friend":
        return cls(**json.loads((Path(profile_dir) / "friend.json").read_text()))

    def save(self, profile_dir: Path) -> None:
        path = Path(profile_dir) / "friend.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(asdict(self), indent=2) + "\n")
        os.chmod(path, 0o600)
