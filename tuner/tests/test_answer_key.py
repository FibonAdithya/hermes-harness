"""Answer key (plan Task 11, Step 2): on the scrubbed 2026-09/10 history, with the
window anchored at 2026-10-04 (before the credit outage), the top 5 candidates
include the three known wastes. The fixture is produced on the droplet (`scrub --since 2026-09-01`) and committed;
the droplet resolves the Nous models-API list, so that is the primary price list, and
the DeepSeek-page list is checked too. The helper logic is tested unskipped against
a small synthetic fixture."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory import NOUS_V4_PRO, V4_PRO  # judged at v4-pro list prices, both lists
from hermes_tuner import scrub
from hermes_tuner.detect import Candidate, detect, saved_turns
from tests.test_detect import DAY, GET, briefing_runs, call, schema_sessions, sess

FIXTURE = Path(__file__).parent / "fixtures" / "history-2026-10.json.gz"
ANCHOR = datetime(2026, 10, 4, tzinfo=timezone.utc).timestamp()
MAX_STRING = 64
BRIEFING, CALENDAR = "3bc4e11d29b1", "cc68e24f21cd"

# The 2026-10-28 fixture (scrub v3, plan Task 12): `scrub --since 2026-09-15 --until 2026-10-29`.
FIXTURE_1028 = Path(__file__).parent / "fixtures" / "history-2026-10-28.json.gz"
NOW_1028 = datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp()


def needs(path: Path):
    return pytest.mark.skipif(not path.exists(), reason=f"{path.name} not produced yet")


needs_fixture = needs(FIXTURE)
needs_fixture_1028 = needs(FIXTURE_1028)


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
    cands = detect(scrub.load(FIXTURE), now=ANCHOR, pricing=NOUS_V4_PRO)
    assert missing_from_top5(cands) == [], [(c.kind, c.key, c.job_id) for c in cands[:5]]


@needs_fixture
def test_answer_key_in_top_five_at_deepseek_page_prices():
    cands = detect(scrub.load(FIXTURE), now=ANCHOR, pricing=V4_PRO)
    assert missing_from_top5(cands) == [], [(c.kind, c.key, c.job_id) for c in cands[:5]]


@pytest.mark.parametrize("path", [pytest.param(FIXTURE, marks=needs_fixture, id=FIXTURE.name),
                                  pytest.param(FIXTURE_1028, marks=needs_fixture_1028,
                                               id=FIXTURE_1028.name)])
def test_fixture_has_no_long_strings(path):
    assert long_strings(path) == []


def top_bytes(cands: list[Candidate]) -> dict[str | None, str]:
    """Each job's highest-ranked `bytes` candidate key (`cands` is ranked)."""
    top: dict[str | None, str] = {}
    for c in cands:
        if c.kind == "bytes":
            top.setdefault(c.job_id, c.key)
    return top


@needs_fixture_1028
def test_fixture_bytes_ranking():
    """Acceptance check 1: the top bytes candidate per job, at Nous v4-pro prices."""
    top = top_bytes(detect(scrub.load(FIXTURE_1028), now=NOW_1028, pricing=NOUS_V4_PRO))
    assert top.get(CALENDAR) == "bytes:mcp__google__search_gmail_messages", top
    assert top.get(BRIEFING) == "bytes:" + GET, top


@needs_fixture_1028
def test_fixture_parallel_sequence_zero():
    """Acceptance check 2: the 2026-10-24 calendar run fetched its mails in parallel,
    so three consecutive fetches save no turn."""
    runs = [s for s in scrub.load(FIXTURE_1028)
            if s.id.startswith(f"cron_{CALENDAR}_20261024_")]
    assert len(runs) == 1, [s.id for s in runs]
    gram = (GET,) * 3
    names = [c.name for c in runs[0].calls]
    # Precondition: the gram occurs, so 0 is not the empty-match answer.
    assert any(tuple(names[i:i + 3]) == gram for i in range(len(names) - 2)), names
    assert saved_turns(runs[0], gram) == 0


def test_top_bytes_takes_each_jobs_first_bytes_candidate():
    cands = [Candidate("sequence", "seq:x", "j1", 5, 5, 1, 9.0),
             Candidate("bytes", "bytes:a", "j1", 5, 5, 1, 3.0),
             Candidate("bytes", "bytes:b", "j2", 5, 5, 1, 2.0),
             Candidate("bytes", "bytes:c", "j1", 5, 5, 1, 1.0)]
    assert top_bytes(cands) == {"j1": "bytes:a", "j2": "bytes:b"}


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
