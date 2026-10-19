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


def _disguise(text: str) -> str:
    """Full-width letters and digits, and a zero-width space every 10 characters."""
    wide = "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in text)
    return "​".join(wide[i:i + 10] for i in range(0, len(wide), 10))


def test_copy_with_zero_width_and_full_width_characters_is_detected():
    """M1: NFKC and format-character stripping on corpus and query."""
    from hermes_tuner.fingerprint import CopyIndex
    secret = "Invoice 4471 from Acme Ltd due on Friday"  # exactly 40 characters
    assert len(secret) == 40
    disguised = _disguise(secret)
    assert disguised != secret and "​" in disguised
    assert CopyIndex.build(["header " + secret + " footer"]).contains_copy(
        "draft: " + disguised + " end")
    assert CopyIndex.build(["header " + disguised + " footer"]).contains_copy(
        "draft: " + secret + " end")
    # control: unrelated text of the same shape is not a copy
    assert not CopyIndex.build(["header " + secret + " footer"]).contains_copy(
        _disguise("Quarterly report for the north-east team"))
