"""`hermes-wiki repos`: refresh repos.md and repos-private.md in the wiki.

Every shell-out goes through the injected `run`; every page write goes through the injected
store. Any privacy doubt aborts with exit 1 before the first write.
"""
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

from . import todos
from .config import WikiConfig
from .reposcan import (HOSTS, Checkout, GitHubDown, Unreachable, load_overrides, one_repo,
                       owner_repos, page_index, readme_line, scan_host)
from .repospage import build_rows, private_names, private_names_in, render, same_page
from .secretscan import find_secrets

PUBLIC, PRIVATE = "repos.md", "repos-private.md"
STATE = "repos-state.json"
MESSAGE = "repos: refresh"
PULL_TIMEOUT = 20
_CHECKOUT_FIELDS = {f.name for f in fields(Checkout)}


def _load_state(path: Path) -> dict:
    """Saved host entries keyed by host name; anything unreadable counts as no state."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hosts = data["hosts"]
        if not isinstance(hosts, dict):
            return {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    good = {}
    for name, entry in hosts.items():
        try:
            day = entry["date"]
            raw = entry["checkouts"]
            if not isinstance(day, str) or not isinstance(raw, list):
                continue
            if not all(isinstance(c, dict) and set(c) == _CHECKOUT_FIELDS for c in raw):
                continue
            good[name] = {"date": day, "checkouts": [Checkout(**c) for c in raw]}
        except (KeyError, TypeError):
            continue
    return good


def _save_state(path: Path, hosts: dict) -> None:
    data = {"hosts": {name: {"date": e["date"], "checkouts": [asdict(c) for c in e["checkouts"]]}
                      for name, e in hosts.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_slug(key: str) -> bool:
    if key.lower().startswith("local:"):
        return False
    owner, sep, name = key.partition("/")
    return bool(sep and owner and name and "/" not in name)


def _row_text(row) -> str:
    parts = [row.key, row.slug or "", row.purpose, row.band, row.last_commit or "", *row.pages]
    for paths in row.paths.values():
        parts += paths
    return "\n".join(parts)


def run_repos(cfg: WikiConfig, *, run, store, now: datetime, overrides_path: Path, dry_run: bool,
              out: TextIO, err: TextIO, sleep=time.sleep) -> int:
    def abort(msg: str, code: int = 1) -> int:
        print(f"hermes-wiki repos: {msg}", file=err)
        return code

    # 1. fresh wiki copy. Always pull (no throttle stamp): an unpulled wiki could miss a page
    # that newly marks a repo private.
    if cfg.pull:
        try:
            r = run(["git", "-C", str(cfg.root), "pull", "--ff-only", "-q"],
                    capture_output=True, text=True, timeout=PULL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return abort("wiki pull timed out")
        except OSError as e:
            return abort(f"wiki pull failed: {e}")
        if r.returncode != 0:
            reason = (r.stderr or "").strip()[:300] or f"git exited {r.returncode}"
            return abort(f"wiki pull failed: {reason}")

    # 2. overrides and wiki index. The file is required: it names the private repos that have
    # no wiki page, so running without it could put one on the public page.
    if not Path(overrides_path).exists():
        return abort(f"overrides file missing: {overrides_path} (it lists private repos with no "
                     "wiki page; see runbooks/wiki.md, Repo registry)", 2)
    try:
        ov = load_overrides(overrides_path)
    except ValueError as e:
        return abort(f"bad overrides: {e}", 2)
    except OSError as e:
        return abort(f"cannot read overrides {overrides_path}: {e}", 2)
    if not ov.owners:
        return abort(f"bad overrides: {overrides_path}: owners must not be empty", 2)
    try:
        index = page_index(cfg.root)
    except OSError as e:
        return abort(f"cannot read the wiki: {e}")

    # 3. GitHub owner listings
    since = now - timedelta(days=30)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_date = since.date().isoformat()
    today = now.date().isoformat()
    owned = []
    try:
        for owner in ov.owners:
            owned += owner_repos(owner, run, since_iso, gh=cfg.gh)
    except GitHubDown as e:
        return abort(str(e))

    # 4. host scans, with saved paths for unreachable hosts
    state_path = cfg.state_dir / STATE
    saved = _load_state(state_path)
    checkouts: list[Checkout] = []
    notes: list[str] = []
    for host in HOSTS:
        try:
            got = scan_host(host, run, since_date)
        except Unreachable:
            entry = saved.get(host.name)
            if entry is None:
                notes.append(f"{host.name} paths unknown (unreachable today)")
            else:
                notes.append(f"{host.name} paths as of {entry['date']} (unreachable today)")
                checkouts += entry["checkouts"]
            continue
        saved[host.name] = {"date": today, "checkouts": got}
        checkouts += got

    # 5. lookups for unowned clone-owner checkouts and includes
    owned_keys = {r.slug.lower() for r in owned}
    clone_owners = {o.lower() for o in ov.clone_owners}
    wanted: dict[str, str] = {}
    candidates = [c.slug for c in checkouts
                  if c.slug is not None and c.slug.partition("/")[0].lower() in clone_owners]
    candidates += sorted(ov.include)
    for slug in candidates:
        if _is_slug(slug) and slug.lower() not in owned_keys:
            wanted.setdefault(slug.lower(), slug)
    extra = {slug: one_repo(slug, run, since_iso, gh=cfg.gh) for slug in wanted.values()}

    # 6. rows and pages
    rows = build_rows(checkouts, owned, extra, index, ov, readme=readme_line)
    public_rows = [r for r in rows if not r.private]
    pages = {
        PUBLIC: render(public_rows, private=False, today=today, host_notes=notes),
        PRIVATE: render([r for r in rows if r.private], private=True, today=today, host_notes=notes),
    }

    # 7. fail closed on secrets and private names in the public page
    for path, text in pages.items():
        if find_secrets(text):
            return abort(f"{path} would contain a secret pattern; nothing written")
    hidden = private_names(rows, index, ov)
    hits = private_names_in(pages[PUBLIC], hidden)
    if hits:
        where = []
        for name in hits:
            keys = [r.key for r in public_rows if private_names_in(_row_text(r), [name])]
            where.append(f"{name} (in {', '.join(keys) if keys else 'page text'})")
        return abort(f"{PUBLIC} would name private repos: {'; '.join(where)}; nothing written. "
                     "Add an override (exclude, private or purpose) to repos.toml.")

    # 8. dry run
    if dry_run:
        for path, text in pages.items():
            print(f"===== {path}", file=out)
            out.write(text)
        return 0

    # 9. write, then save the state
    wrote = 0
    for path, text in pages.items():
        def edit(current: str, new=text):
            return None if same_page(current, new) else (new, MESSAGE)
        try:
            wrote += todos.update(store, edit, sleep=sleep, path=path, create=True)
        except todos.TodoError as e:
            return abort(f"{e} ({wrote} page(s) written before the failure)")
    try:
        _save_state(state_path, saved)
    except OSError as e:
        return abort(f"pages written; state not saved: {e}")
    print(f"repos: wrote {wrote} page(s)" if wrote else "repos: unchanged", file=out)
    return 0
