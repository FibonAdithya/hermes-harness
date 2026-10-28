"""Build, validate and publish inbox notes."""
import base64
import os
import random
import re
import secrets
import subprocess
import time
from datetime import date
from pathlib import Path

from .config import WikiConfig
from .frontmatter import dump
from .schema import load_tags, validate_note


_CONTROL = re.compile("[\\x00-\\x1f\\x7f\\x85\\u2028\\u2029]")
_RAND = re.compile(r"[0-9a-f]{8}")


class NoteRejected(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class PublishError(Exception):
    pass


def note_path(source: str, kind: str, created: date, rand: str) -> str:
    return f"inbox/{source}/{created:%Y-%m-%d}-{kind}-{rand}.md"


PUBLISH_ATTEMPTS = 5
# GitHub answers 409 when main moved while the PUT was in flight (another agent
# published at the same moment). No commit was made, so the same PUT is retried.
_BRANCH_MOVED = re.compile(r"\(HTTP 409\)")


def publish(repo: str, path: str, content: str, run=subprocess.run, gh: str = "gh",
            dry_dir: Path | None = None, sleep=time.sleep) -> None:
    if dry_dir is not None and os.environ.get("HERMES_WIKI_DRY_PUBLISH") == "1":
        target = Path(dry_dir) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    argv = [
        gh, "api", "-X", "PUT", f"repos/{repo}/contents/{path}",
        "-f", f"message=note: {path}",
        "-f", f"content={encoded}",
        "-f", "branch=main",
    ]
    for attempt in range(PUBLISH_ATTEMPTS):
        try:
            proc = run(argv, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            # The PUT may have landed; retrying could fail as "already exists". Report it.
            raise PublishError("timeout") from None
        except OSError as e:
            raise PublishError(f"could not run {gh}: {e}") from None
        if proc.returncode == 0:
            return
        conflict = bool(_BRANCH_MOVED.search(proc.stderr or ""))
        if not conflict or attempt == PUBLISH_ATTEMPTS - 1:
            raise PublishError(proc.stderr or f"gh exited {proc.returncode}")
        sleep(0.5 * 2 ** attempt + random.uniform(0, 0.5))


def add_note(
    cfg: WikiConfig,
    *,
    kind: str,
    body: str,
    tags: list[str],
    repos: list[str] = (),
    verified: str = "claim",
    suggest_tags: list[str] = (),
    review_by: str | None = None,
    session: str = "",
    today: date | None = None,
    rand: str | None = None,
    publisher=publish,
) -> str:
    created = today or date.today()
    rand = rand or secrets.token_hex(4)
    meta = {
        "kind": kind,
        "tags": list(tags),
        "repos": list(repos),
        "source": cfg.source,
        "session": session,
        "created": f"{created:%Y-%m-%d}",
        "verified": verified,
        "suggest_tags": list(suggest_tags),
    }
    if review_by is not None:
        meta["review_by"] = review_by
    errors = validate_note(meta, body, load_tags(cfg.root))
    for key in ("session", "verified", "review_by"):
        if key not in meta:
            continue
        v = meta[key]
        if not isinstance(v, str):
            errors.append(f"{key}: must be a string")
        elif len(v.splitlines()) > 1 or _CONTROL.search(v):
            errors.append(f"{key}: must be a single line without control characters")
    if not _RAND.fullmatch(rand):
        errors.append("rand: must be 8 lowercase hex characters")
    if errors:
        raise NoteRejected(errors)
    path = note_path(cfg.source, kind, created, rand)
    content = dump(meta, body)
    if publisher is publish:
        publisher(cfg.repo, path, content, gh=cfg.gh, dry_dir=cfg.state_dir / "dry")
    else:
        publisher(cfg.repo, path, content)
    return path
