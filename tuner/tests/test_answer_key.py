"""Answer key (plan Task 11, Step 2): on the scrubbed 2026-09/10 history, with the
window anchored at 2026-10-04 (before the credit outage), the top 5 candidates
include the three known wastes. The fixture is produced on the droplet; until it is
committed the real-fixture tests skip. The helper logic is tested unskipped against
a small synthetic fixture."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory import V4_PRO  # the answer key is judged at the v4-pro list prices
from hermes_tuner import scrub
from hermes_tuner.detect import Candidate, detect
from tests.test_detect import DAY, GET, briefing_runs, call, schema_sessions, sess

FIXTURE = Path(__file__).parent / "fixtures" / "history-2026-10.json.gz"
ANCHOR = datetime(2026, 10, 4, tzinfo=timezone.utc).timestamp()
MAX_STRING = 64
BRIEFING, CALENDAR = "3bc4e11d29b1", "cc68e24f21cd"

needs_fixture = pytest.mark.skipif(not FIXTURE.exists(), reason=f"{FIXTURE.name} not produced yet")


def missing_from_top5(cands: list[Candidate]) -> list[str]:
    """Answer-key items absent from the first five candidates."""
    top = cands[:5]
    missing = []
    if not any(c.kind == "sequence" and c.job_id in (BRIEFING, CALENDAR) and GET in c.key
               for c in top):
        missing.append("sequence containing " + GET)
    if not any(c.key == f"reread:{CALENDAR}" and c.kind == "reread" for c in top):
        missing.append(f"reread:{CALENDAR}")
    if not any(c.kind == "schema_lookup" and c.key == "schema_lookup" for c in top):
        missing.append("schema_lookup")
    return missing


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


def long_strings(path: Path) -> list[str]:
    """Every string, key or value, in the decompressed JSON longer than MAX_STRING."""
    data = json.loads(gzip.decompress(Path(path).read_bytes()))
    return [x for x in _walk(data) if isinstance(x, str) and len(x) > MAX_STRING]


# --- the real fixture ---------------------------------------------------------------

@needs_fixture
def test_answer_key_in_top_five():
    cands = detect(scrub.load(FIXTURE), now=ANCHOR, pricing=V4_PRO)
    assert missing_from_top5(cands) == [], [(c.kind, c.key, c.job_id) for c in cands[:5]]


@needs_fixture
def test_fixture_has_no_long_strings():
    assert long_strings(FIXTURE) == []


# --- the helpers, against a synthetic fixture ---------------------------------------

def _history() -> list:
    runs = briefing_runs(12, job=BRIEFING)
    cal = []
    for k in range(1, 8):
        cs = [call(i, GET, 1000, ref=f"r{r}") for i, r in enumerate(range(k, k + 7))]
        cal.append(sess(f"c{k}", cs, job=CALENDAR, age_days=10 - k, cost=0.15))
    return runs + cal + schema_sessions(25)


def test_synthetic_history_satisfies_answer_key(tmp_path):
    out = tmp_path / "h.json.gz"
    scrub.dump(_history(), out, b"salt")
    now = max(s.started_at for s in scrub.load(out)) + DAY
    assert missing_from_top5(detect(scrub.load(out), now=now, pricing=V4_PRO)) == []
    assert long_strings(out) == []


def test_missing_items_are_reported_by_name(tmp_path):
    # Without the calendar job there is no reread; without schema sessions, no lookup.
    out = tmp_path / "h.json.gz"
    scrub.dump(briefing_runs(12, job=BRIEFING), out, b"salt")
    now = max(s.started_at for s in scrub.load(out)) + DAY
    assert missing_from_top5(detect(scrub.load(out), now=now, pricing=V4_PRO)) == [
        f"reread:{CALENDAR}", "schema_lookup"]


def test_sequence_for_another_job_does_not_count(tmp_path):
    out = tmp_path / "h.json.gz"
    scrub.dump(briefing_runs(12, job="ffffffffffff") + _history()[12:], out, b"salt")
    now = max(s.started_at for s in scrub.load(out)) + DAY
    miss = missing_from_top5(detect(scrub.load(out), now=now, pricing=V4_PRO))
    assert miss == ["sequence containing " + GET]


def test_top_five_cutoff_is_enforced():
    filler = [Candidate("sequence", f"seq:f{i}", None, 10, 10, 1, 1000.0 - i) for i in range(5)]
    wanted = [Candidate("schema_lookup", "schema_lookup", None, 20, 40, 1, 1.0)]
    assert missing_from_top5(filler + wanted) == [
        "sequence containing " + GET, f"reread:{CALENDAR}", "schema_lookup"]


def test_long_string_in_key_or_value_is_found(tmp_path):
    for doc in ({"k" * 65: 1}, {"k": ["v" * 65]}, {"k": "v" * 64}):
        p = tmp_path / "x.json.gz"
        p.write_bytes(gzip.compress(json.dumps(doc).encode()))
        assert bool(long_strings(p)) == (doc != {"k": "v" * 64})


def test_reread_for_another_job_does_not_count():
    other = Candidate("reread", "reread:ffffffffffff", "ffffffffffff", 6, 36, 1, 50.0)
    assert f"reread:{CALENDAR}" in missing_from_top5([other])
