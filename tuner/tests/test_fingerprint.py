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


# --- trusted text (without) ----------------------------------------------------------

def _corpus_with_trusted():
    rng = random.Random(77)
    trusted, leak = rand(rng, 120), rand(rng, 200)
    return trusted, leak, CopyIndex.build([leak[:100] + " " + trusted + " " + leak[100:]])


def test_without_drops_text_that_is_also_trusted():
    """Trusted text echoed in an untrusted result (a job prompt in a session_search
    hit) is not a copy. Catches: without() returning the index unchanged."""
    trusted, _leak, idx = _corpus_with_trusted()
    assert idx.contains_copy(trusted)
    assert not idx.without([trusted]).contains_copy(trusted)


def test_without_keeps_every_untrusted_40_char_run():
    """Catches: without() emptying the index or dropping fingerprints outside the
    trusted text."""
    trusted, leak, idx = _corpus_with_trusted()
    sub = idx.without([trusted])
    for part in (leak[:100], leak[100:]):
        for i in range(0, len(part) - 40 + 1, 5):
            assert sub.contains_copy(part[i:i + 40]), i


def test_without_leaves_the_original_index_alone():
    """The cycle caches one index and checks fixes for different jobs against it.
    Catches: without() removing fingerprints in place."""
    trusted, _leak, idx = _corpus_with_trusted()
    idx.without([trusted])
    assert idx.contains_copy(trusted)


def test_without_matches_trusted_text_after_normalisation():
    """Trusted text and corpus are normalised the same way. Catches: without()
    hashing the raw trusted text (here its doubled spaces would not match)."""
    rng = random.Random(5)
    words = [rand(rng, 8) for _ in range(10)]
    idx = CopyIndex.build([" ".join(words)])
    assert not idx.without(["  ".join(words)]).contains_copy(" ".join(words))
