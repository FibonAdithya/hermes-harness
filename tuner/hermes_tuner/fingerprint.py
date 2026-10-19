"""Winnowing copy detector (spec 5.3).

Any shared run of at least K + W - 1 = 40 characters (after whitespace
normalisation) is detected. Shorter runs may be detected; nothing is
guaranteed below 40.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left
from collections import deque
from collections.abc import Iterable

K = 24
W = 17
BASE = 257
MOD = (1 << 61) - 1
_TOP = pow(BASE, K - 1, MOD)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _winnow(text: str) -> set[int]:
    """Winnowed fingerprints of already-normalised text."""
    n = len(text) - K + 1
    if n <= 0:
        return set()
    codes = [ord(c) for c in text]
    h = 0
    for c in codes[:K]:
        h = (h * BASE + c) % MOD
    out: set[int] = set()
    win: deque[tuple[int, int]] = deque()  # (hash, index), hashes strictly increasing
    top, base, mod = _TOP, BASE, MOD
    for i in range(n):
        if i:
            h = ((h - codes[i - 1] * top) * base + codes[i + K - 1]) % mod
        while win and win[-1][0] >= h:  # ties: keep the rightmost
            win.pop()
        win.append((h, i))
        if win[0][1] <= i - W:
            win.popleft()
        if i >= W - 1:
            out.add(win[0][0])
    if n < W:  # fewer hashes than one window: use them all as one window
        out.add(win[0][0])
    return out


class CopyIndex:
    def __init__(self, fingerprints: array) -> None:
        self._fp = fingerprints

    @classmethod
    def build(cls, texts: Iterable[str]) -> "CopyIndex":
        fps: set[int] = set()
        for t in texts:
            fps |= _winnow(_normalise(t))
        return cls(array("Q", sorted(fps)))

    def contains_copy(self, text: str) -> bool:
        fp = self._fp
        if not len(fp):
            return False
        for f in _winnow(_normalise(text)):
            j = bisect_left(fp, f)
            if j < len(fp) and fp[j] == f:
                return True
        return False
