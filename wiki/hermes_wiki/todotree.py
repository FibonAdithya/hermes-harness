"""One-commit multi-file writes to the wiki repo through the git data API."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from hermes_wiki.todos import Conflict, StoreError, WriteUncertain, _first_line

PATHS = ("agent-todos.jsonl", "agent-todos.md", "todos.md")


class GhTree:
    def __init__(self, repo: str, gh: str, run):
        self.repo = repo
        self.gh = gh
        self.run = run

    def _call(self, method: str, endpoint: str, body: dict | None = None,
              *, uncertain: bool = False):
        argv = [self.gh, "api", "--method", method, f"repos/{self.repo}/{endpoint}"]
        kw = {"capture_output": True, "text": True, "timeout": 30}
        if body is not None:
            argv += ["--input", "-"]
            kw["input"] = json.dumps(body)
        try:
            r = self.run(argv, **kw)
        except subprocess.TimeoutExpired:
            if uncertain:
                raise WriteUncertain() from None
            raise StoreError(f"timed out: {method} {endpoint.split('?')[0]}") from None
        except OSError as e:
            raise StoreError(str(e)) from None
        return r

    def _json(self, r) -> dict:
        if r.returncode != 0:
            raise StoreError(_first_line(r.stderr) or f"gh exited {r.returncode}")
        try:
            return json.loads(r.stdout)
        except ValueError as e:
            raise StoreError(f"unexpected response: {type(e).__name__}") from None

    def head(self, paths: tuple[str, ...] = PATHS) -> tuple[str, dict[str, str | None]]:
        data = self._json(self._call("GET", "git/ref/heads/main"))
        try:
            sha = data["object"]["sha"]
        except (KeyError, TypeError) as e:
            raise StoreError(f"unexpected response: {type(e).__name__}") from None
        files: dict[str, str | None] = {}
        for p in paths:
            r = self._call("GET", f"contents/{p}?ref={sha}")
            if r.returncode != 0 and "(HTTP 404)" in (r.stderr or ""):
                files[p] = None
                continue
            try:
                data = self._json(r)
                if data.get("encoding") != "base64":
                    raise StoreError(f"{p} is too large for the contents API")
                files[p] = base64.b64decode(data["content"]).decode("utf-8")
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise StoreError(f"unexpected response: {type(e).__name__}") from None
        return sha, files

    def commit(self, base: str, files: dict[str, str], message: str) -> None:
        try:
            base_tree = self._json(self._call("GET", f"git/commits/{base}"))["tree"]["sha"]
            entries = [{"path": p, "mode": "100644", "type": "blob", "content": c}
                       for p, c in files.items()]
            tree = self._json(self._call(
                "POST", "git/trees", {"base_tree": base_tree, "tree": entries}))["sha"]
            new = self._json(self._call(
                "POST", "git/commits",
                {"message": message, "tree": tree, "parents": [base]}))["sha"]
        except (KeyError, TypeError) as e:
            raise StoreError(f"unexpected response: {type(e).__name__}") from None
        r = self._call("PATCH", "git/refs/heads/main",
                       {"sha": new, "force": False}, uncertain=True)
        if r.returncode != 0:
            err = r.stderr or ""
            if "(HTTP 422)" in err and "fast forward" in err.lower():
                raise Conflict()
            raise StoreError(_first_line(err) or f"gh exited {r.returncode}")


class DryTree:
    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self._paths = PATHS

    def head(self, paths: tuple[str, ...] = PATHS) -> tuple[str, dict[str, str | None]]:
        self._paths = tuple(paths)
        h = hashlib.sha256()
        files: dict[str, str | None] = {}
        for p in self._paths:
            f = self.dir / p
            data = f.read_bytes() if f.exists() else None
            h.update(p.encode() + b"\0")
            h.update(b"-" if data is None else b"+" + data)
            h.update(b"\0")
            files[p] = None if data is None else data.decode("utf-8")
        return h.hexdigest(), files

    def commit(self, base: str, files: dict[str, str], message: str) -> None:
        current = self.head(self._paths)[0]
        if current != base:
            raise Conflict()
        self.dir.mkdir(parents=True, exist_ok=True)
        for p, c in files.items():
            (self.dir / p).write_text(c, encoding="utf-8")
