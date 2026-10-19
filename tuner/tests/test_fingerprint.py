from __future__ import annotations

import random
import string

from hermes_tuner.fingerprint import CopyIndex

LETTERS = string.ascii_letters


def rand(rng, n):
    return "".join(rng.choice(LETTERS) for _ in range(n))


def test_every_40_char_copy_detected():
    rng = random.Random(1234)
    for _ in range(50):
        corpus = rand(rng, 2000)
        start = rng.randrange(0, len(corpus) - 40)
        piece = corpus[start:start + 40]
        fresh = rand(rng, 300)
        at = rng.randrange(0, len(fresh) + 1)
        text = fresh[:at] + piece + fresh[at:]
        assert CopyIndex.build([corpus]).contains_copy(text)


def test_no_shared_kgram_not_detected():
    rng = random.Random(1234)
    for _ in range(50):
        corpus = rand(rng, 2000)
        start = rng.randrange(0, len(corpus) - 23)
        piece = corpus[start:start + 23]
        fresh = rand(rng, 300)
        at = rng.randrange(0, len(fresh) + 1)
        text = fresh[:at] + piece + fresh[at:]
        assert not CopyIndex.build([corpus]).contains_copy(text)


def test_whitespace_normalised():
    idx = CopyIndex.build(["a  b\n c" * 20])
    assert idx.contains_copy("a b c" * 20)


def test_short_text_and_empty_index():
    idx = CopyIndex.build(["x" * 100])
    assert not idx.contains_copy("x" * 10)
    assert not CopyIndex.build([]).contains_copy("anything at all " * 10)
