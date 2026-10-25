"""Every side effect of the friends tool. Thin on purpose; behaviour lives in cli.py."""
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

OWNER_ROOT = Path("/home/hermes/.hermes")
FALLBACK_HERMES = "/home/hermes/.local/bin/hermes"


def containers_under(mounts: dict[str, list[str]], profile: Path) -> list[str]:
    """Container ids with a mount source equal to or under profile."""
    profile = Path(profile)
    return [cid for cid, srcs in mounts.items()
            if any(Path(s).is_relative_to(profile) for s in srcs)]


class System:
    def __init__(self, root: Path = OWNER_ROOT):
        self.root = Path(root)

    def _hermes_bin(self) -> str:
        return shutil.which("hermes") or FALLBACK_HERMES

    def _home(self, name: str | None) -> Path:
        return self.root if name is None else self.root / "profiles" / name

    def _run(self, cmd: list[str], env: dict | None = None) -> str:
        try:
            r = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True,
                               timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{' '.join(cmd)} timed out after 300s") from None
        if r.returncode != 0:
            tail = (r.stderr or "")[-500:]
            raise RuntimeError(f"{' '.join(cmd)} failed ({r.returncode}): {tail}")
        return r.stdout

    def hermes(self, name: str | None, *args: str) -> str:
        env = {**os.environ, "HERMES_HOME": str(self._home(name))}
        return self._run([self._hermes_bin(), *args], env=env)

    def systemctl(self, *args: str) -> None:
        self._run(["systemctl", "--user", *args])

    def reclaim(self, path: Path) -> None:
        """Take back ownership of files the root-run Docker sandbox left under path."""
        self._run(["docker", "run", "--rm", "--network=none", "--user", "0",
                   "-v", f"{Path(path)}:/p", "hermes-sandbox:latest",
                   "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/p"])

    def remove_containers(self, profile: Path) -> int:
        """Force-remove every container that mounts something under profile."""
        ids = self._run(["docker", "ps", "-aq"]).split()
        mounts = {}
        for cid in ids:
            try:
                out = self._run(["docker", "inspect", "-f",
                                 "{{range .Mounts}}{{println .Source}}{{end}}", cid])
            except RuntimeError:
                continue  # removed between ps and inspect
            mounts[cid] = [ln for ln in out.splitlines() if ln]
        doomed = containers_under(mounts, Path(profile))
        for cid in doomed:
            self._run(["docker", "rm", "-f", cid])
        return len(doomed)

    def unit_active(self, unit: str) -> bool:
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                               check=False, capture_output=True, timeout=30)
            return r.returncode == 0
        except Exception:
            return False

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
