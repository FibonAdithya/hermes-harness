"""Install the SessionEnd hook into a Claude Code settings.json."""
import json
import os
import shutil
from pathlib import Path


def install_session_end_hook(settings: Path, command: str) -> bool:
    """Append the hook unless `command` is already registered. Returns True if the file changed."""
    settings = Path(settings)
    existed = settings.exists()
    data = json.loads(settings.read_text(encoding="utf-8")) if existed else {}
    if not isinstance(data, dict):
        raise ValueError(f"{settings}: top level must be a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{settings}: 'hooks' must be an object")
    entries = hooks.setdefault("SessionEnd", [])
    if not isinstance(entries, list):
        raise ValueError(f"{settings}: 'hooks.SessionEnd' must be a list")
    for entry in entries:
        for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
            if isinstance(h, dict) and h.get("command") == command:
                return False
    entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    if existed:
        backup = settings.with_name(settings.name + ".bak-wiki")
        if not backup.exists():
            shutil.copy2(settings, backup)
    settings.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.with_name(settings.name + ".tmp-wiki")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, settings)
    return True
