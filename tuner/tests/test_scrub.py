from __future__ import annotations

import gzip
import json

import pytest

from hermes_tuner import scrub
from hermes_tuner.detect import detect
from hermes_tuner.model import Session, ToolCall
from tests.test_detect import NOW, briefing_runs, web_chats

SALT = b"salt-1"
RAW_REF = "RAWMESSAGEID" + "x" * 80


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)
    else:
        yield o


def _strings(path):
    data = json.loads(gzip.decompress(path.read_bytes()))
    return [x for x in _walk(data) if isinstance(x, str)]


def _sessions():
    return briefing_runs() + web_chats()


def test_scrub_round_trip_keeps_detection_identical(tmp_path):
    ss = _sessions()
    scrub.dump(ss, tmp_path / "f.json.gz", SALT)
    back = scrub.load(tmp_path / "f.json.gz")
    assert len(back) == len(ss)
    assert detect(back, NOW) == detect(ss, NOW)
    assert detect(back, NOW)  # non-empty, so the equality means something


def test_round_trip_reread_still_found(tmp_path):
    runs = [Session(f"c{k}", "cron", "cc68e24f21cd", NOW - (10 - k) * 86400.0, 0.1, 5, 1,
                    tuple(ToolCall(f"x{i}", "mcp__google__get_gmail_message_content", ("m",),
                                   f"r{r}", 1000) for i, r in enumerate(range(k, k + 7))))
            for k in range(1, 8)]
    scrub.dump(runs, tmp_path / "f.json.gz", SALT)
    got = [c for c in detect(scrub.load(tmp_path / "f.json.gz"), NOW) if c.kind == "reread"]
    assert got[0].occurrences == 36


def test_scrub_output_has_no_long_strings(tmp_path):
    ss = [Session("cron_3bc4e11d29b1_20261001_060000", "cron", "3bc4e11d29b1", NOW - 86400.0, 0.1, 7, 1,
                  (ToolCall("c0", "mcp__google__get_gmail_message_content", ("message_id",), RAW_REF, 123),
                   ToolCall("c1", "x", (), None, 5)))]
    scrub.dump(ss, tmp_path / "f.json.gz", SALT)
    strings = _strings(tmp_path / "f.json.gz")
    assert all(len(s) <= 64 for s in strings)
    assert RAW_REF not in strings
    assert not any(RAW_REF in s for s in strings)


def test_ref_is_salted_hash_prefix(tmp_path):
    import hashlib
    ss = [Session("s", "cron", None, 1.0, 0.0, 1, 1, (ToolCall("c", "t", ("a",), "abc", 1),))]
    scrub.dump(ss, tmp_path / "f.json.gz", SALT)
    back = scrub.load(tmp_path / "f.json.gz")
    assert back[0].calls[0].ref == hashlib.sha256(SALT + b"abc").hexdigest()[:16]
    scrub.dump(ss, tmp_path / "g.json.gz", b"other")
    assert scrub.load(tmp_path / "g.json.gz")[0].calls[0].ref != back[0].calls[0].ref


def test_load_rebuilds_all_fields_and_none_ref(tmp_path):
    ss = [Session("s1", "telegram", None, 12.5, 0.25, 77, 9,
                  (ToolCall("c", "t", ("a", "b"), None, 4),))]
    scrub.dump(ss, tmp_path / "f.json.gz", SALT)
    b = scrub.load(tmp_path / "f.json.gz")[0]
    assert (b.id, b.source, b.job_id, b.started_at, b.est_cost, b.prompt_tokens, b.output_tokens) == \
        ("s1", "telegram", None, 12.5, 0.25, 77, 9)
    assert b.calls[0].name == "t" and b.calls[0].arg_keys == ("a", "b")
    assert b.calls[0].ref is None and b.calls[0].result_chars == 4


def test_dump_is_deterministic(tmp_path):
    scrub.dump(_sessions(), tmp_path / "a.gz", SALT)
    scrub.dump(_sessions(), tmp_path / "b.gz", SALT)
    assert (tmp_path / "a.gz").read_bytes() == (tmp_path / "b.gz").read_bytes()


def test_scrub_round_trips_breakdown(tmp_path):
    ss = [Session("s1", "cron", "j", 1.0, 0.0, 111, 9, (), input_tokens=1,
                  cache_read_tokens=100, cache_write_tokens=10)]
    scrub.dump(ss, tmp_path / "f.json.gz", SALT)
    (b,) = scrub.load(tmp_path / "f.json.gz")
    assert (b.input_tokens, b.cache_read_tokens, b.cache_write_tokens, b.output_tokens,
            b.prompt_tokens) == (1, 100, 10, 9, 111)


@pytest.mark.parametrize("field", ["input_tokens", "cache_read_tokens", "cache_write_tokens"])
def test_scrub_load_refuses_a_dump_without_the_breakdown(tmp_path, field):
    """Loading zeros would price every run at its output tokens only."""
    scrub.dump(_sessions()[:1], tmp_path / "f.json.gz", SALT)
    doc = json.loads(gzip.decompress((tmp_path / "f.json.gz").read_bytes()))
    del doc["sessions"][0][field]
    (tmp_path / "old.json.gz").write_bytes(gzip.compress(json.dumps(doc).encode()))
    with pytest.raises(ValueError, match="fixture predates token breakdown"):
        scrub.load(tmp_path / "old.json.gz")
