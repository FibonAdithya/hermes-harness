"""Every side effect of the friends tool. Thin on purpose; behaviour lives in cli.py."""
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

OWNER_ROOT = Path("/home/hermes/.hermes")
FALLBACK_HERMES = "/home/hermes/.local/bin/hermes"


class System:
    def __init__(self, root: Path = OWNER_ROOT):
        self.root = Path(root)

    def _hermes_bin(self) -> str:
        return shutil.which("hermes") or FALLBACK_HERMES

    def _home(self, name: str | None) -> Path:
        return self.root if name is None else self.root / "profiles" / name

    def _run(self, cmd: list[str], env: dict | None = None) -> str:
        r = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or "")[-500:]
            raise RuntimeError(f"{' '.join(cmd)} failed ({r.returncode}): {tail}")
        return r.stdout

    def hermes(self, name: str | None, *args: str) -> str:
        env = {**os.environ, "HERMES_HOME": str(self._home(name))}
        return self._run([self._hermes_bin(), *args], env=env)

    def systemctl(self, *args: str) -> None:
        self._run(["systemctl", "--user", *args])

    def mem_available_mb(self) -> int:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
        raise RuntimeError("MemAvailable not found in /proc/meminfo")

    def caddy_reload(self) -> None:
        self._run(["caddy", "reload", "--config", "/etc/caddy/Caddyfile"])

    def write_file(self, path: Path, data: str | bytes, mode: int) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = data.encode() if isinstance(data, str) else data
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.chmod(path, mode)

    def chmod(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)

    def remove_tree(self, path: Path) -> None:
        path = Path(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def revoke_google_token(self, refresh_token: str) -> None:
        body = urllib.parse.urlencode({"token": refresh_token}).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"token revoke returned {resp.status}")

    def send(self, name: str | None, chat_id: int, text: str) -> bool:
        try:
            env = {**os.environ, "HERMES_HOME": str(self._home(name))}
            r = subprocess.run(
                [self._hermes_bin(), "send", "-q", "-t", f"telegram:{chat_id}", text],
                env=env, check=False, capture_output=True, text=True, timeout=60)
            return r.returncode == 0
        except Exception:
            return False
