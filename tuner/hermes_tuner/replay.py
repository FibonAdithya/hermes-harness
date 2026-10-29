"""Replay of recorded runs: which result strings the model reused verbatim.

Text read here stays inside the tuner process; callers report counts and booleans.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from . import policy
from .detect import carried_usd
from .history import ReplayCall, ReplayRun
from .model import Pricing
from .reducers import _URL_RE, apply_reducer, text_view


def norm(text: str) -> str:
    """Lowercase; every whitespace run becomes one space; no leading/trailing space."""
    return " ".join(text.lower().split())


def _automaton(a: str) -> tuple[list[dict[str, int]], list[int], list[int]]:
    """Suffix automaton of `a`: transitions per state, plus link and len lists."""
    nxt: list[dict[str, int]] = [{}]
    link = [-1]
    length = [0]
    last = 0
    for ch in a:
        cur = len(nxt)
        nxt.append({})
        link.append(-1)
        length.append(length[last] + 1)
        p = last
        while p != -1 and ch not in nxt[p]:
            nxt[p][ch] = cur
            p = link[p]
        if p == -1:
            link[cur] = 0
        else:
            q = nxt[p][ch]
            if length[p] + 1 == length[q]:
                link[cur] = q
            else:
                clone = len(nxt)
                nxt.append(dict(nxt[q]))
                link.append(link[q])
                length.append(length[p] + 1)
                while p != -1 and nxt[p].get(ch) == q:
                    nxt[p][ch] = clone
                    p = link[p]
                link[q] = clone
                link[cur] = clone
        last = cur
    return nxt, link, length


def _maximal_matches(sam, b: str, min_len: int):
    """Substrings of `b` (len >= min_len) in the automaton's text, maximal within `b`."""
    nxt, link, length = sam
    state, cur, prev = 0, 0, 0
    for k, ch in enumerate(b):
        while state != 0 and ch not in nxt[state]:
            state = link[state]
            cur = length[state]
        if ch in nxt[state]:
            state = nxt[state][ch]
            cur += 1
        else:
            cur = 0
        # The match of length `prev` ending at k-1 was not extended at k.
        if cur != prev + 1 and prev >= min_len:
            yield b[k - prev:k]
        prev = cur
    if prev >= min_len:
        yield b[len(b) - prev:]


def used_strings(result: str, later: Sequence[str], exclude: Sequence[str]) -> frozenset[str]:
    """Strings of `result` the model reused verbatim in `later` messages.

    For each normalised later message, the maximal substrings that occur in the
    normalised text view of `result`, stripped of edge spaces, of length >=
    USED_MIN_CHARS after stripping. Strings found
    in any normalised `exclude` text (the prompt) are dropped. Only the first
    USED_MAX_CHARS normalised chars of the result and of each message are compared.
    """
    cap = policy.USED_MAX_CHARS
    sam = _automaton(norm(text_view(result))[:cap])
    found: set[str] = set()
    for msg in later:
        for m in _maximal_matches(sam, norm(msg)[:cap], policy.USED_MIN_CHARS):
            # A match can start or end with a space; the reduced text need not keep it.
            m = m.strip()
            if len(m) >= policy.USED_MIN_CHARS:
                found.add(m)
    excl = [norm(e) for e in exclude]
    return frozenset(s for s in found if not any(s in e for e in excl))


def survives(s: str, reduced: str) -> bool:
    """True when used string `s` is still in the reduced result's text view."""
    return s in norm(text_view(reduced))


def boilerplate(used: Iterable[str], results: Sequence[str]) -> frozenset[str]:
    """Strings of `used` present in more than BOILERPLATE_SHARE of a tool's results.

    Presence is a substring of the result's normalised text view. With fewer than
    BOILERPLATE_MIN_RESULTS results nothing is boilerplate.
    """
    if len(results) < policy.BOILERPLATE_MIN_RESULTS:
        return frozenset()
    views = [norm(text_view(r)) for r in results]
    limit = policy.BOILERPLATE_SHARE * len(results)
    return frozenset(s for s in set(used) if sum(s in v for v in views) > limit)


@dataclass(frozen=True)
class ReducerResult:
    spec: dict | None
    reason: str          # "ok" | "no_runs" | "too_few_sessions" | "cut_below_min"
    sessions: int        # runs with at least one result for the tool
    results: int
    bytes_before: int
    bytes_after: int
    used_total: int      # (result, used string) pairs that must survive
    used_kept: int
    est_saving_per_run: float   # carried_usd(before) - carried_usd(after), summed, / sessions


_SCHEMELESS_RE = re.compile(r"^((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]*[a-z][a-z0-9-]*)/")


def _used_hosts(used: Iterable[str]) -> list[str]:
    """Lowercased hostnames of URLs in used strings, sorted.

    Also takes `host/path` tokens with no scheme when the host is a dotted name.
    """
    hosts: set[str] = set()
    for s in used:
        for m in _URL_RE.finditer(s):
            try:
                h = urlsplit(m.group(0)).hostname
            except ValueError:
                h = None
            if h:
                hosts.add(h.lower())
        for tok in s.split():
            if "://" in tok:
                continue
            m = _SCHEMELESS_RE.match(tok.lower())
            if m:
                hosts.add(m.group(1))
    return sorted(hosts)


def _top_keys(texts: Sequence[str]) -> list[str]:
    keys: set[str] = set()
    for t in texts:
        try:
            obj = json.loads(t)
        except ValueError:
            continue
        if isinstance(obj, dict):
            keys.update(obj)
    return sorted(keys)


def _with(steps: list[dict], cand: dict) -> list[dict]:
    """`steps` plus `cand`; a drop_fields candidate merges into an existing one."""
    if cand["op"] == "drop_fields":
        for i, st in enumerate(steps):
            if st["op"] == "drop_fields":
                merged = {"op": "drop_fields", "fields": sorted(set(st["fields"]) | set(cand["fields"]))}
                return steps[:i] + [merged] + steps[i + 1:]
    return steps + [cand]


def _evaluate(steps: list[dict], texts: Sequence[str], used: Sequence[frozenset[str]]):
    """Total reduced bytes and reduced texts, or None if any used string is lost."""
    spec = {"steps": steps}
    out: list[str] = []
    for text, need in zip(texts, used):
        red = apply_reducer(spec, text)
        if need:
            view = norm(text_view(red))      # survives(), with the view computed once
            if not all(s in view for s in need):
                return None
        out.append(red)
    return sum(len(t) for t in out), out


def search_reducer(runs: Sequence[ReplayRun], tool: str, job: str, exclude: Sequence[str],
                   pricing: Pricing) -> ReducerResult:
    """Greedy search for the reducer that cuts most bytes of `tool`'s results.

    Every used string (minus boilerplate) of every replayed result must survive.
    Reads text in-process only; the result holds a spec of op names, field names
    and hosts, plus counts.
    """
    calls: list[ReplayCall] = []
    used: list[frozenset[str]] = []
    sessions = 0
    for run in runs:
        mine = [c for c in run.calls if c.name == tool]
        if mine:
            sessions += 1
        for c in mine:
            calls.append(c)
            used.append(used_strings(c.text, [t for p, t in run.said if p > c.pos], exclude))
    texts = [c.text for c in calls]
    before = sum(len(t) for t in texts)
    if not calls:
        return ReducerResult(None, "no_runs", 0, 0, 0, 0, 0, 0, 0.0)

    bp = boilerplate(set().union(*used), texts)
    used = [u - bp for u in used]
    used_total = sum(len(u) for u in used)

    cands: list[dict] = [{"op": "drop_fields", "fields": [k]} for k in _top_keys(texts)]
    cands += [{"op": "html_to_text"},
              {"op": "shorten_urls", "keep_hosts": _used_hosts(set().union(*used))},
              {"op": "drop_quoted"}, {"op": "drop_footer"}, {"op": "collapse_ws"}]
    cands += [{"op": "cap", "chars": n} for n in policy.REDUCER_CAP_LADDER]

    steps: list[dict] = []
    total, reduced = before, list(texts)
    step_min = policy.REDUCER_STEP_MIN * before
    while True:
        best = None
        for cand in cands:
            got = _evaluate(_with(steps, cand), texts, used)
            if got is not None and (best is None or got[0] < best[1][0]):
                best = (cand, got)
        if best is None or total - best[1][0] < step_min:
            break
        cand, (total, reduced) = best
        steps = _with(steps, cand)
        if cand["op"] == "drop_fields":
            cands = [c for c in cands if c is not cand]
        else:
            cands = [c for c in cands if c["op"] != cand["op"]]

    saving = sum(carried_usd(c.chars, c.later_turns, pricing)
                 - carried_usd(len(r), c.later_turns, pricing) for c, r in zip(calls, reduced))
    if sessions < policy.REPLAY_MIN_SESSIONS:
        reason = "too_few_sessions"
    elif before == 0 or (before - total) < policy.REDUCER_MIN_CUT * before:
        reason = "cut_below_min"
    else:
        reason = "ok"
    spec = {"tool": tool, "job": job, "steps": steps} if reason == "ok" else None
    return ReducerResult(spec, reason, sessions, len(calls), before, total,
                         used_total, used_total, saving / sessions)
