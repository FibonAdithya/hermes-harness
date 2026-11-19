"""Nightly agent-todos job: close items whose check passed, apply and expire suggestions.

GitHub is read once, before the transaction, because `transact` re-runs its
change on every conflict retry; the change itself only reads the fetched results.
After the transaction an evidence file is written for the curator's worklist.
"""
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from hermes_wiki.notes import _CONTROL
from hermes_wiki.secretscan import find_secrets
from hermes_wiki.todorecords import Record, loads
from hermes_wiki.todos import MAX_DETAIL, StoreError, TodoError, _first_line
from hermes_wiki.todostore import JSONL, Change, NotMigrated, transact

MESSAGE = "todo: nightly"
EXPIRE_DAYS = 7
FAILURE_ALARM = 3
_REF = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(pull|issues)/(\d+)")
_DONE_WHEN = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
_LIVE = ("open", "suggested")


def _key(kind: str, repo: str, num: int) -> str:
    return f"{kind}:{repo}#{num}"


def _split(key: str) -> tuple[str, str, int]:
    kind, rest = key.split(":", 1)
    repo, num = rest.rsplit("#", 1)
    return kind, repo, int(num)


def _url(key: str) -> str:
    kind, repo, num = _split(key)
    return f"https://github.com/{repo}/{'pull' if kind == 'pr' else 'issues'}/{num}"


def _check_keys(r: Record) -> list[tuple[str, dict]]:
    out = []
    for c in r.done_when:
        for kind in ("pr", "issue"):
            m = _DONE_WHEN.fullmatch(str(c.get(kind, "")))
            if m:
                out.append((_key(kind, m.group(1), int(m.group(2))), c))
    return out


def _text_keys(r: Record) -> list[str]:
    keys = []
    for m in _REF.finditer("\n".join([r.title, *r.details])):
        num = int(m.group(3))
        k = _key("pr" if m.group(2) == "pull" else "issue", m.group(1), num)
        if num > 0 and k not in keys:
            keys.append(k)
    return keys


def _gh_error(r) -> str:
    text = _CONTROL.sub("", _first_line(r.stderr))[:MAX_DETAIL]
    if not text or find_secrets(text):
        return f"gh failed (rc {r.returncode})"
    return text


def _day(value) -> str | None:
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _fetch(cfg, run, key: str) -> dict:
    kind, repo, num = _split(key)
    endpoint = f"repos/{repo}/{'pulls' if kind == 'pr' else 'issues'}/{num}"
    try:
        r = run([cfg.gh, "api", endpoint], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"error": type(e).__name__}
    if r.returncode != 0:
        return {"error": _gh_error(r)}
    try:
        data = json.loads(r.stdout)
    except ValueError as e:
        return {"error": type(e).__name__}
    if not isinstance(data, dict):
        return {"error": "unexpected response"}
    out = {k: data.get(k) for k in ("state", "merged_at", "closed_at")}
    if not isinstance(out["state"], str) or not all(
            v is None or isinstance(v, str) for v in (out["merged_at"], out["closed_at"])):
        return {"error": "unexpected response"}
    return out


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_failures(path: Path, log) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log(f"no check failure state at {path}; starting empty")
        return {}
    except (OSError, ValueError) as e:
        log(f"check failure state {path} unreadable ({type(e).__name__}); starting empty")
        return {}
    failures = data.get("failures") if isinstance(data, dict) else None
    if not isinstance(failures, dict) or not all(
            isinstance(v, list) and all(isinstance(d, str) for d in v) for v in failures.values()):
        log(f"check failure state {path} malformed; starting empty")
        return {}
    return failures


def _check_text(name: str, value, why: list[str]) -> str | None:
    if not isinstance(value, str):
        why.append(f"{name} must be a string")
        return None
    value = value.strip()
    if not value:
        why.append(f"{name} empty")
    elif len(value) > MAX_DETAIL:
        why.append(f"{name} longer than {MAX_DETAIL} characters")
    elif _CONTROL.search(value):
        why.append(f"{name} must be one line without control characters")
    elif find_secrets(value):
        why.append(f"{name} matches a secret pattern")
    return None if why else value


def _parse_suggestions(text: str) -> tuple[list[tuple[int, str, str, str]], list[str]]:
    """(line number, id, reason, evidence) for well-formed lines, and drop messages."""
    good, drops = [], []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            drops.append(f"dropped suggestion line {n}: invalid JSON")
            continue
        if not isinstance(obj, dict):
            drops.append(f"dropped suggestion line {n}: not an object")
            continue
        why: list[str] = []
        if not isinstance(obj.get("id"), str):
            why.append("id must be a string")
        reason = _check_text("reason", obj.get("reason"), why)
        evidence = _check_text("evidence", obj.get("evidence"), why)
        if why:
            drops.append(f"dropped suggestion line {n}: {why[0]}")
            continue
        good.append((n, obj["id"], reason, evidence))
    return good, drops


def _refused(s: dict) -> bool:
    return bool(s.get("rejected")) or bool(s.get("expired"))


def _blocked_list(s: dict) -> list[str]:
    b = s.get("blocked_evidence")
    return [x for x in b if isinstance(x, str)] if isinstance(b, list) else []


def _blocked(r: Record, evidence: str) -> bool:
    s = r.suggestion or {}
    return evidence in _blocked_list(s) or (_refused(s) and s.get("evidence") == evidence)


def _suggest(r: Record, day: str, reason: str, evidence: str) -> None:
    """Replace the suggestion, carrying forward every evidence the owner or expiry refused."""
    old = r.suggestion or {}
    blocked = set(_blocked_list(old))
    if _refused(old) and isinstance(old.get("evidence"), str):
        blocked.add(old["evidence"])
    r.status = "suggested"
    r.suggestion = {"at": day, "reason": reason, "evidence": evidence}
    if blocked:
        r.suggestion["blocked_evidence"] = sorted(blocked)


def _ref(key: str, results: dict[str, dict]) -> dict:
    res = results.get(key, {"error": "not fetched"})
    if "error" in res:
        return {"url": _url(key), "key": key, "error": res["error"]}
    return {"url": _url(key), "key": key, **res}


def _item(r: Record, results: dict[str, dict]) -> dict:
    refs = sorted((_ref(k, results) for k in _text_keys(r)), key=lambda x: x["url"])
    digest = hashlib.sha256(json.dumps([r.title, r.details, r.project, r.status, refs],
                                       sort_keys=True).encode("utf-8")).hexdigest()
    return {"id": r.id, "title": r.title, "details": r.details, "project": r.project,
            "created": r.created, "status": r.status, "suggestion": r.suggestion,
            "refs": refs, "hash": digest}


def nightly(cfg, tree, *, suggestions_text: str, evidence_path: Path, state_path: Path,
            today: date, now: datetime, run, sleep, log: Callable[[str], None],
            new_id: Callable[[], str]) -> dict:
    try:
        _, files = tree.head()
    except StoreError as e:
        raise TodoError(f"GitHub: {e}") from None
    if files.get(JSONL) is None:
        raise NotMigrated()
    keys: list[str] = []
    for r in loads(files[JSONL]):
        if r.status in _LIVE:
            for k in [k for k, _ in _check_keys(r)] + _text_keys(r):
                if k not in keys:
                    keys.append(k)
    results = {k: _fetch(cfg, run, k) for k in keys}

    failures = _load_failures(state_path, log)
    day = today.isoformat()
    for k, res in results.items():
        if "error" in res:
            failures[k] = sorted(set(failures.get(k, [])) | {day})[-FAILURE_ALARM:]
        else:
            failures.pop(k, None)
    failures = {k: v for k, v in failures.items() if k in results}  # drop items no longer checked
    _write_json(state_path, {"failures": failures})

    lines, shape_drops = _parse_suggestions(suggestions_text)
    for m in shape_drops:
        log(m)
    final: dict = {}

    def fn(records: list[Record], today_: date) -> list[Record]:
        counts = {"closed": 0, "suggested_by_checks": 0, "suggestions_accepted": 0, "expired": 0}
        drops: list[str] = []
        by_id = {r.id: r for r in records}
        accepted: set[str] = set()
        for n, item_id, reason, evidence in lines:
            r = by_id.get(item_id)
            if item_id in accepted:
                why = "duplicate id"
            elif r is None:
                why = "unknown id"
            elif r.status != "open":
                why = f"item is {r.status}, not open"
            elif _blocked(r, evidence):
                why = "evidence matches a rejected or expired suggestion"
            else:
                _suggest(r, day, reason, evidence)
                accepted.add(item_id)
                counts["suggestions_accepted"] += 1
                continue
            drops.append(f"dropped suggestion line {n}: {why}")

        for r in records:
            if r.status not in _LIVE:
                continue
            checks = [(k, c, results[k]) for k, c in _check_keys(r)
                      if k in results and "error" not in results[k]]
            closed = None
            for k, c, res in checks:
                kind, _, num = _split(k)
                if kind == "pr" and res["merged_at"]:
                    closed = f"PR #{num} merged {_day(res['merged_at']) or ''}".rstrip()
                elif kind == "issue" and res["state"] == "closed":
                    closed = f"issue #{num} closed {_day(res['closed_at']) or ''}".rstrip()
                if closed:
                    break
            if closed:
                r.status = "done"
                r.closed = {"at": day, "by": "check", "reason": closed}
                counts["closed"] += 1
                continue
            if r.status != "open":
                continue
            for k, c, res in checks:
                kind, _, num = _split(k)
                if kind == "pr" and c.get("state") == "merged" and res["state"] == "closed":
                    evidence = f"{k} closed unmerged"
                    if _blocked(r, evidence):
                        continue
                    _suggest(r, day, f"PR #{num} was closed unmerged", evidence)
                    counts["suggested_by_checks"] += 1
                    break

        for r in records:
            if r.status != "suggested" or not r.suggestion:
                continue
            try:
                age = (today - date.fromisoformat(r.suggestion["at"])).days
            except (KeyError, TypeError, ValueError):
                continue
            if age >= EXPIRE_DAYS:
                r.status = "open"
                r.suggestion["expired"] = True
                counts["expired"] += 1

        final.clear()
        final.update(records=records, counts=counts, drops=drops)
        return records

    committed = transact(tree, Change(MESSAGE, fn), today=today, source=cfg.source,
                         new_id=new_id, sleep=sleep, log=log)
    for m in final["drops"]:
        log(m)

    items = [_item(r, results) for r in sorted(final["records"], key=lambda r: r.id)
             if r.status in _LIVE]
    _write_json(evidence_path, {
        "generated_at": now.isoformat(),
        "check_failures": sorted(k for k, v in failures.items() if len(v) >= FAILURE_ALARM),
        "items": items})
    return {**final["counts"], "suggestions_dropped": len(shape_drops) + len(final["drops"]),
            "fetch_failures": sum("error" in res for res in results.values()),
            "committed": committed}
