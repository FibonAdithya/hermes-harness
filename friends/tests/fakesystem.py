import os
import shutil
from pathlib import Path


class FakeSystem:
    """Records mutating calls in order; performs real filesystem effects under root."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.calls: list[tuple] = []
        self.mem_mb = 4096
        self.fail_on: list[tuple] = []  # call-tuple prefixes that raise
        self.send_ok = True

    def _rec(self, *call):
        self.calls.append(call)
        for prefix in self.fail_on:
            if call[: len(prefix)] == prefix:
                raise RuntimeError(f"injected failure: {call}")

    def hermes(self, name, *args):
        self._rec("hermes", name, *args)
        if name is not None and args[:2] == ("profile", "create"):
            d = self.root / "profiles" / name
            d.mkdir(parents=True)
            os.chmod(d, 0o775)
            (d / ".env").write_text("SEEDED=1\n")
            os.chmod(d / ".env", 0o600)
            (d / "SOUL.md").write_text("default soul\n")
        elif args[:2] == ("profile", "delete"):
            shutil.rmtree(self.root / "profiles" / args[-1], ignore_errors=True)
        return ""

    def systemctl(self, *args):
        self._rec("systemctl", *args)

    def mem_available_mb(self):
        return self.mem_mb

    def caddy_reload(self):
        self._rec("caddy_reload")

    def write_file(self, path, data, mode):
        self._rec("write_file", Path(path), mode)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        os.chmod(path, mode)

    def chmod(self, path, mode):
        self._rec("chmod", Path(path), mode)
        os.chmod(path, mode)

    def remove_tree(self, path):
        self._rec("remove_tree", Path(path))
        if Path(path).is_dir():
            shutil.rmtree(path)
        else:
            Path(path).unlink()

    def revoke_google_token(self, refresh_token):
        self._rec("revoke_google_token", refresh_token)

    def send(self, name, chat_id, text):
        self._rec("send", name, chat_id, text)
        return self.send_ok

    # helpers for assertions
    def index(self, *prefix):
        for i, c in enumerate(self.calls):
            if c[: len(prefix)] == prefix:
                return i
        raise AssertionError(f"no call starting {prefix}: {self.calls}")
