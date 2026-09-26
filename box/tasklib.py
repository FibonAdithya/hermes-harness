"""The fleet ledger, as the box's verbs see it.

The only code on the box that writes fleet issues: file_task, approve_task,
close_task and the doctor all come through here. Every call takes the gh runner
as an argument so tests stand in for GitHub; production passes real_gh, which
uses the box's gh login.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Callable

import boxlib

Gh = Callable[[list[str], "str | None"], "tuple[int, str, str]"]

# fleet/src/fleet/labels.py::CLASSES. Extend by PR when fleet does.
CLASSES = ("patch", "spec", "investigation", "integration")
TRIAGE, READY, AUTO_OK, HUMAN = "fleet:triage", "fleet:ready", "fleet:auto-ok", "fleet:human"
SOLO, DROPPED = "fleet:solo", "fleet:dropped"
SOURCES = ("source:hermes", "source:doctor")
MODES = {"fleet": (READY, AUTO_OK), "solo": (HUMAN, SOLO)}
# The owner's Telegram announcement shows at most this much (broker approvals.py
# BODY_CHARS), and approve() binds the hash of the whole text, so a longer body
# could be approved unread. The broker offers only `drop` for anything longer.
MAX_TITLE, MAX_BODY = 200, 3500


class TaskError(ValueError):
    pass


def real_gh(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise TaskError(f"cannot run gh: {exc}") from exc
    return p.returncode, p.stdout, p.stderr


def issue_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()


def fleet_repos(home: Path) -> list[Path]:
    root = Path(home) / "TIG"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "fleet.toml").is_file())


def repo_dir(home: Path, name: str) -> Path:
    d = Path(home) / "TIG" / boxlib._name(name)
    if not (d / "fleet.toml").is_file():
        raise TaskError(f"{name} has no fleet.toml")
    return d


def slug(repo: Path) -> str:
    try:
        s = tomllib.loads((Path(repo) / "fleet.toml").read_text())["repo"]["slug"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"{Path(repo).name}/fleet.toml has no [repo] slug: {exc}") from exc
    if not isinstance(s, str) or not boxlib.SLUG_RE.fullmatch(s):
        raise TaskError(f"{Path(repo).name}/fleet.toml slug is not owner/name: {s!r}")
    return s


def fleet_areas(repo: Path, home: Path) -> frozenset[str]:
    """The target's areas, read by fleet's own parser (fleet.policy.area_names).

    --no-sync: file_task reaches this from Hermes, and must not sync (rewrite) the
    deployed fleet checkout's environment; deploy_fleet owns that. -P: cwd is the
    target repo, which must not shadow fleet's imports."""
    code = ("import json; from fleet.config import load_config; from fleet.policy import area_names; "
            "print(json.dumps(sorted(area_names(load_config()))))")
    try:
        p = subprocess.run(["uv", "run", "--no-sync", "--project", str(Path(home) / "TIG" / "fleet"),
                            "python", "-P", "-c", code],
                           cwd=repo, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise TaskError(f"cannot run uv: {exc}") from exc
    if p.returncode != 0:
        raise TaskError(f"could not read {Path(repo).name}'s areas: {p.stderr.strip()[-300:]}")
    return frozenset(json.loads(p.stdout.strip().splitlines()[-1]))


def _ok(gh: Gh, args: list[str], stdin: str | None = None) -> str:
    rc, out, err = gh(args, stdin)
    if rc != 0:
        raise TaskError(f"gh {' '.join(args[:2])} failed: {err.strip()[:300]}")
    return out


def ensure_label(gh: Gh, repo_slug: str, name: str) -> None:
    rc, _, err = gh(["label", "create", name, "--repo", repo_slug, "--color", "ededed"], None)
    if rc != 0 and "already exists" not in err:
        raise TaskError(f"gh label create {name} failed: {err.strip()[:300]}")


def file_issue(gh: Gh, repo_slug: str, title: str, body: str, area: str, cls: str,
               areas: frozenset[str], source: str, extra_labels: tuple[str, ...] = ()) -> dict:
    title = (title or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise TaskError(f"title must be 1..{MAX_TITLE} characters")
    if not isinstance(body, str) or len(body) > MAX_BODY:
        raise TaskError(f"body must be at most {MAX_BODY} characters")
    if area not in areas:
        raise TaskError(f"area {area!r} is not in {repo_slug}'s ownership.md (have: {', '.join(sorted(areas))})")
    if cls not in CLASSES:
        raise TaskError(f"class must be one of {', '.join(CLASSES)}: {cls!r}")
    if source not in SOURCES:
        raise TaskError(f"source must be one of {', '.join(SOURCES)}: {source!r}")
    for name in (source, *extra_labels):
        ensure_label(gh, repo_slug, name)
    labels = (TRIAGE, f"area:{area}", f"class:{cls}", source, *extra_labels)
    args = ["issue", "create", "--repo", repo_slug, "--title", title, "--body-file", "-"]
    for name in labels:
        args += ["--label", name]
    url = _ok(gh, args, body).strip().splitlines()[-1]
    return {"number": int(url.rsplit("/", 1)[1]), "url": url}


def _view(gh: Gh, repo_slug: str, number: int) -> dict:
    return json.loads(_ok(gh, ["issue", "view", str(int(number)), "--repo", repo_slug,
                               "--json", "state,title,body,labels"]))


def approve(gh: Gh, repo_slug: str, number: int, want_hash: str, mode: str) -> dict:
    """The owner's `task` or `solo` reply. Refuses unless the issue is still the one they read."""
    if mode not in MODES:
        raise TaskError(f"mode must be fleet or solo: {mode!r}")
    row = _view(gh, repo_slug, number)
    names = {x["name"] for x in row.get("labels", [])}
    if str(row.get("state", "")).upper() != "OPEN":
        raise TaskError(f"#{number} is closed")
    if TRIAGE not in names:
        raise TaskError(f"#{number} is no longer in triage")
    if issue_hash(row["title"], row["body"]) != want_hash:
        raise TaskError(f"#{number} changed since it was announced; wait for the new announcement")
    add = MODES[mode]
    for name in add:
        ensure_label(gh, repo_slug, name)
    # Add before remove, as fleet's own CLI does: a failure between the two leaves
    # the issue in two states, which fleet's ledger refuses loudly, rather than in
    # none, which it reads as triage and forgets.
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--add-label", ",".join(add)])
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--remove-label", TRIAGE])
    return {"number": int(number), "title": row["title"], "body": row["body"]}


def close(gh: Gh, repo_slug: str, number: int) -> None:
    ensure_label(gh, repo_slug, DROPPED)
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--add-label", DROPPED])
    _ok(gh, ["issue", "close", str(int(number)), "--repo", repo_slug])


def open_issues_with_label(gh: Gh, repo_slug: str, label: str) -> list[dict]:
    return json.loads(_ok(gh, ["issue", "list", "--repo", repo_slug, "--state", "open", "--label", label,
                               "--json", "number,title,body,labels", "--limit", "200"]) or "[]")


def triage(gh: Gh, repo_slug: str) -> list[dict]:
    return [{"number": r["number"], "title": r["title"], "body": r["body"],
             "labels": [x["name"] for x in r.get("labels", [])], "hash": issue_hash(r["title"], r["body"])}
            for r in open_issues_with_label(gh, repo_slug, TRIAGE)]


def comment(gh: Gh, repo_slug: str, number: int, body: str) -> None:
    _ok(gh, ["issue", "comment", str(int(number)), "--repo", repo_slug, "--body", body])
