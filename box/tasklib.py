"""The fleet ledger, as the box's verbs see it.

The only code on the box that writes fleet issues: file_task, approve_task,
close_task and the doctor all come through here. Every call takes the gh runner
as an argument so tests stand in for GitHub; production passes real_gh, which
uses the box's gh login.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Callable

import boxlib

Gh = Callable[[list[str], "str | None"], "tuple[int, str, str]"]

# fleet/src/fleet/labels.py::CLASSES. Extend by PR when fleet does.
CLASSES = ("patch", "spec", "investigation", "integration", "build", "bug")
TRIAGE, READY, AUTO_OK, HUMAN = "fleet:triage", "fleet:ready", "fleet:auto-ok", "fleet:human"
SOLO, DROPPED = "fleet:solo", "fleet:dropped"
SOURCES = ("source:hermes", "source:doctor")
MODES = {"fleet": (READY, AUTO_OK), "solo": (HUMAN, SOLO)}
# The owner's Telegram announcement shows at most this much (broker approvals.py
# BODY_CHARS), and approve() binds the hash of the whole text, so a longer body
# could be approved unread. The broker offers only `drop` for anything longer.
MAX_TITLE, MAX_BODY = 200, 3500
# The owner's answers to fleet:human escalations, one JSON list per issue, under home.
ANSWERS_DIR = Path(".local/state/fleet/answers")
MAX_ANSWER = 2000
ANSWER_HEADER = "Owner's answer (from Telegram):\n\n"


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


def require_role_prompts(repo: Path) -> None:
    """Refuse a repository fleet cannot run on.

    fleet stops at policy load when the roles directory named in fleet.toml holds
    no prompt (fleet.policy.known_roles). A night started there fails at once, and
    the doctor files that failure against fleet; an issue approved with `task`
    there waits for a night that cannot run. file_task, list_triage and `solo`
    need none of this, so such a repository's tasks are solo only."""
    repo = Path(repo)
    try:
        roles = tomllib.loads((repo / "fleet.toml").read_text())["policy"]["roles"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        roles = None
    if not isinstance(roles, str) or not any((repo / roles).glob("*.md")):
        raise TaskError(f"{repo.name} has no fleet role prompts, so fleet cannot run there; its tasks are solo only")


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


# --- fleet:human: the owner answers or takes an escalated issue -------------------------

def fleet_escalations(home: Path, comments: dict[int, list[str]]) -> dict[int, dict | None]:
    """Each issue's newest escalation, read by fleet's own parser (fleet.escalation.latest).

    One process for every issue; the comment bodies go on stdin as JSON. --no-sync: this
    is reached from the owner's Telegram reply, and must not sync (rewrite) the deployed
    fleet checkout's environment; deploy_fleet owns that. -P: the cwd must not shadow
    fleet's imports. Every failure raises TaskError, so a caller never mistakes a broken
    fleet checkout for an issue with no escalation."""
    code = ("import json, sys; from fleet.escalation import latest; "
            "d = json.load(sys.stdin); print(json.dumps({k: latest(v) for k, v in d.items()}))")
    want = {int(n): [str(b) for b in v] for n, v in comments.items()}
    try:
        p = subprocess.run(["uv", "run", "--no-sync", "--project", str(Path(home) / "TIG" / "fleet"),
                            "python", "-P", "-c", code],
                           input=json.dumps({str(n): v for n, v in want.items()}),
                           capture_output=True, text=True, check=False)
    except OSError as exc:
        raise TaskError(f"cannot run uv: {exc}") from exc
    if p.returncode != 0:
        raise TaskError(f"could not read escalations: {p.stderr.strip()[-300:]}")
    try:
        raw = json.loads(p.stdout.strip().splitlines()[-1])
        out = {int(k): v for k, v in raw.items()}
    except (IndexError, ValueError, AttributeError, TypeError) as exc:
        raise TaskError(f"fleet.escalation.latest printed no JSON object: {p.stdout.strip()[-300:]!r}") from exc
    for n in want:
        if n not in out:
            raise TaskError(f"fleet.escalation.latest gave nothing for #{n}")
        e = out[n]
        if e is not None and not (isinstance(e, dict) and type(e.get("index")) is int
                                  and isinstance(e.get("reason"), str) and isinstance(e.get("text"), str)):
            raise TaskError(f"fleet.escalation.latest gave a malformed escalation for #{n}: {e!r}"[:300])
    return {n: out[n] for n in want}


def _login(gh: Gh) -> str:
    login = _ok(gh, ["api", "user", "--jq", ".login"]).strip()
    if not login:
        raise TaskError("gh api user gave no login")
    return login


def _human_rows(issues: list[dict], home: Path, escalations) -> list[dict]:
    """The rows human() lists, and the freshness hash answer() and take() check against.

    The hash binds the title and the escalation the owner read (its index and text), so
    a newer escalation or a reworded one changes it. When fleet's helper fails, every
    issue is still listed, with a note and the fallback index -1 and text ""."""
    if not issues:
        return []
    comments = {int(r["number"]): [c.get("body", "") for c in r.get("comments") or []] for r in issues}
    note = None
    try:
        found = escalations(home, comments)
    except TaskError as exc:
        found, note = {}, f"escalation text unavailable: {str(exc)[:200]}"
    rows = []
    for r in issues:
        e = found.get(int(r["number"]))
        esc = {"index": e["index"], "reason": e["reason"], "text": e["text"]} if e else None
        index, text = (esc["index"], esc["text"]) if esc else (-1, "")
        rows.append({"number": int(r["number"]), "title": r["title"],
                     "labels": [x["name"] for x in r.get("labels", [])],
                     "escalation": esc, "note": note, "hash": issue_hash(r["title"], f"{index}\n{text}")})
    return rows


def human(gh: Gh, repo_slug: str, home: Path, escalations=fleet_escalations) -> list[dict]:
    """Open fleet:human issues waiting on the owner: not solo work (run_task's), and not
    already taken by the box's gh login."""
    login = _login(gh)
    issues = json.loads(_ok(gh, ["issue", "list", "--repo", repo_slug, "--state", "open", "--label", HUMAN,
                                 "--json", "number,title,labels,assignees,comments", "--limit", "200"]) or "[]")
    waiting = [r for r in issues
               if SOLO not in {x["name"] for x in r.get("labels", [])}
               and login not in {a.get("login") for a in r.get("assignees") or []}]
    return _human_rows(waiting, home, escalations)


def _fresh_human(gh: Gh, repo_slug: str, number: int, want_hash: str, home: Path, escalations) -> dict:
    """Refuse unless the issue still waits on the owner with the escalation they read."""
    row = json.loads(_ok(gh, ["issue", "view", str(int(number)), "--repo", repo_slug,
                              "--json", "state,title,labels,assignees,comments"]))
    names = {x["name"] for x in row.get("labels", [])}
    if str(row.get("state", "")).upper() != "OPEN":
        raise TaskError(f"#{number} is closed")
    if HUMAN not in names:
        raise TaskError(f"#{number} is no longer waiting on the owner")
    if SOLO in names:
        raise TaskError(f"#{number} is solo work; it is not answered from here")
    row["number"] = int(number)
    [fresh] = _human_rows([row], home, escalations)
    if fresh["hash"] != want_hash:
        raise TaskError(f"#{number} changed since it was announced; wait for the new announcement")
    return fresh


def answers_path(home: Path, repo_slug: str, number: int) -> Path:
    if not isinstance(repo_slug, str) or not boxlib.SLUG_RE.fullmatch(repo_slug):
        raise TaskError(f"repo must be owner/name: {repo_slug!r}")
    owner, name = repo_slug.split("/")
    return Path(home) / ANSWERS_DIR / f"{owner}__{name}" / f"{int(number)}.json"


def _read_answers(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TaskError(f"cannot read the answers file {path}: {exc}") from exc
    try:
        entries = json.loads(text)
    except ValueError as exc:
        raise TaskError(f"the answers file {path} is not JSON; fix it by hand, earlier answers are in it") from exc
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise TaskError(f"the answers file {path} is not a list of answers; fix it by hand")
    return entries


def _write_answers(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp creates it mode 600
            json.dump(entries, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def answer(gh: Gh, repo_slug: str, number: int, want_hash: str, text: str, home: Path, now: float,
           escalations=fleet_escalations) -> dict:
    """The owner's `answer` reply: records it for fleet and sends the issue back to ready."""
    text = text.strip() if isinstance(text, str) else ""
    if not 1 <= len(text) <= MAX_ANSWER:
        raise TaskError(f"an answer must be 1..{MAX_ANSWER} characters")
    _fresh_human(gh, repo_slug, number, want_hash, home, escalations)
    path = answers_path(home, repo_slug, number)
    entries = _read_answers(path)
    entries.append({"text": text, "at": float(now)})
    _write_answers(path, entries)
    comment(gh, repo_slug, number, ANSWER_HEADER + text)
    # fleet reads the highest fleet:answered-N; earlier ones stay on the issue.
    add = (f"fleet:answered-{len(entries)}", READY)
    for name in add:
        ensure_label(gh, repo_slug, name)
    # Add before remove, as approve() does.
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--add-label", ",".join(add)])
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--remove-label", HUMAN])
    return {"number": int(number), "answers": len(entries)}


def take(gh: Gh, repo_slug: str, number: int, want_hash: str, escalations=fleet_escalations,
         home: Path | None = None) -> dict:
    """The owner's `take` reply: assigns the box's gh login, so human() stops listing the
    issue. fleet:human stays on it; the owner works it by hand."""
    home = Path.home() if home is None else Path(home)
    fresh = _fresh_human(gh, repo_slug, number, want_hash, home, escalations)
    login = _login(gh)
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--add-assignee", login])
    return {"number": int(number), "title": fresh["title"], "assignee": login}
