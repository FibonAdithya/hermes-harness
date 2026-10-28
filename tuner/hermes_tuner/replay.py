"""Replay of recorded runs: which result strings the model reused verbatim.

Text read here stays inside the tuner process; callers report counts and booleans.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from . import policy
from .reducers import text_view


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
