"""Winnowing copy detector (spec 5.3).

Any shared run of at least K + W - 1 = 40 characters (after normalisation:
NFKC, format characters such as zero-width spaces removed, whitespace collapsed)
is detected. Shorter runs may be detected; nothing is
guaranteed below 40.

`without(trusted)` drops every fingerprint whose K characters also occur in
trusted text. After that, a shared run is detected if one of its winnowed
fingerprints is not made of trusted text; a run made entirely of trusted text
is not a copy.
"""
from __future__ import annotations

import unicodedata
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
    """NFKC (full-width and compatibility forms fold to plain ones), drop every
    format (Cf) character (zero-width spaces, joiners, bidi marks), collapse
    whitespace. Applied to corpus and query alike."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return " ".join(text.split())


def _kgrams(text: str) -> set[int]:
    """The hash of every K-character window of already-normalised text."""
    n = len(text) - K + 1
    if n <= 0:
        return set()
    codes = [ord(c) for c in text]
    h = 0
    for c in codes[:K]:
        h = (h * BASE + c) % MOD
    out = {h}
    for i in range(1, n):
        h = ((h - codes[i - 1] * _TOP) * BASE + codes[i + K - 1]) % MOD
        out.add(h)
    return out


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

    def without(self, trusted: Iterable[str]) -> "CopyIndex":
        """A new index lacking every fingerprint that also occurs in trusted text."""
        drop: set[int] = set()
        for t in trusted:
            drop |= _kgrams(_normalise(t))
        if not drop:
            return self
        return CopyIndex(array("Q", (f for f in self._fp if f not in drop)))

    def contains_copy(self, text: str) -> bool:
        fp = self._fp
        if not len(fp):
            return False
        for f in _winnow(_normalise(text)):
            j = bisect_left(fp, f)
            if j < len(fp) and fp[j] == f:
                return True
        return False
