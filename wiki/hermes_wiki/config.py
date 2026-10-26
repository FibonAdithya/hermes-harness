"""Wiki configuration, loaded from a JSON file."""
import json
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_PATH = Path("~/.config/hermes-wiki/config.json").expanduser()
_DEFAULT_STATE_DIR = "~/.local/state/hermes-wiki"
_PATH_FIELDS = ("root", "state_dir")


@dataclass(frozen=True)
class WikiConfig:
    root: Path
    source: str
    repo: str = "FibonAdithya/hermes-wiki"
    public_only: bool = False
    pull: bool = False
    model: str = "sonnet"
    bin: str = "hermes-wiki"
    gh: str = "gh"
    claude: str = "claude"
    state_dir: Path = Path(_DEFAULT_STATE_DIR).expanduser()
    path: Path | None = None


def load(path: Path) -> WikiConfig:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(WikiConfig)} - {"path"}
    kwargs = {k: v for k, v in data.items() if k in known}
    for key in _PATH_FIELDS:
        if key in kwargs:
            kwargs[key] = Path(kwargs[key]).expanduser()
    return WikiConfig(**kwargs, path=path)
