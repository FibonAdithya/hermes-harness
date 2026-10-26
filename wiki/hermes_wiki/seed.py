"""One-time seed of TAGS.md and the new CONVENTIONS.md into the wiki repo through `gh api`.

TAGS.md is created only if absent. CONVENTIONS.md is replaced only if it still equals
the copy taken at planning time (seed/CONVENTIONS.before.md), so an edit Hermes made
since then is never overwritten without --force.
"""
import base64
import binascii
import json
import subprocess
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


class SeedError(Exception):
    pass


def _get(repo: str, path: str, run, gh: str) -> dict | None:
    """Return the contents API object, or None on 404."""
    argv = [gh, "api", f"repos/{repo}/contents/{path}"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise SeedError(f"GET {path}: timeout") from None
    if proc.returncode != 0:
        if "404" in (proc.stderr or "") or '"404"' in (proc.stdout or ""):
            return None
        raise SeedError(f"GET {path}: {(proc.stderr or proc.stdout or '').strip()}")
    try:
        obj = json.loads(proc.stdout)
    except ValueError as e:
        raise SeedError(f"GET {path}: not JSON: {e}") from None
    if not isinstance(obj, dict) or "sha" not in obj:
        raise SeedError(f"GET {path}: not a file")
    return obj


def _decode(obj: dict, path: str) -> str:
    try:
        return base64.b64decode(obj.get("content", "")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, TypeError, ValueError) as e:
        raise SeedError(f"GET {path}: content not decodable: {e}") from None


def _put(repo: str, path: str, text: str, message: str, run, gh: str, sha: str | None = None) -> None:
    argv = [gh, "api", "-X", "PUT", f"repos/{repo}/contents/{path}",
            "-f", f"message={message}",
            "-f", f"content={base64.b64encode(text.encode('utf-8')).decode('ascii')}",
            "-f", "branch=main"]
    if sha is not None:
        argv += ["-f", f"sha={sha}"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise SeedError(f"PUT {path}: timeout") from None
    if proc.returncode != 0:
        raise SeedError(f"PUT {path}: {(proc.stderr or proc.stdout or '').strip()}")


def seed(repo: str, force: bool = False, run=subprocess.run, gh: str = "gh",
         seed_dir: Path = SEED_DIR) -> list[str]:
    seed_dir = Path(seed_dir)
    tags = (seed_dir / "TAGS.md").read_text(encoding="utf-8")
    new_conv = (seed_dir / "CONVENTIONS.md").read_text(encoding="utf-8")
    old_conv = (seed_dir / "CONVENTIONS.before.md").read_text(encoding="utf-8")
    actions = []

    if _get(repo, "TAGS.md", run, gh) is None:
        _put(repo, "TAGS.md", tags, "seed: TAGS.md", run, gh)
        actions.append("created TAGS.md")
    else:
        actions.append("TAGS.md exists; left alone")

    cur = _get(repo, "CONVENTIONS.md", run, gh)
    text = _decode(cur, "CONVENTIONS.md") if cur is not None else None
    if text == new_conv:
        actions.append("CONVENTIONS.md already seeded")
    elif force or text == old_conv:
        _put(repo, "CONVENTIONS.md", new_conv, "seed: CONVENTIONS.md", run, gh,
             sha=cur["sha"] if cur is not None else None)
        actions.append("replaced CONVENTIONS.md" if cur is not None else "created CONVENTIONS.md")
    else:
        actions.append("CONVENTIONS.md changed since planning; not replaced")
    return actions
