"""Back-off from repeated identical outcomes, and the drafting history (Task 10)."""
from __future__ import annotations

import hashlib
import json

import pytest

from hermes_tuner.backoff import backoff, copy_fields, draft_history, input_hash, reason_class
from hermes_tuner.ledger import FixRecord, Ledger

DAY = 86400.0
KEY = "seq:a>b"
COPY = "contains text copied from untrusted tool results"
h = input_hash({"prompt": "Triage the inbox."}, None)


def rec(kind, state, reason, t, ih=h, key=KEY, rid=None):
    return FixRecord(rid or f"r{t}-{kind}-{state}", kind, "job", key, state, t,
                     reason=reason, input_hash=ih)


def rej(kind, reason, t, ih=h, key=KEY):
    return rec(kind, "rejected", reason, t, ih, key)


# --- reason_class -----------------------------------------------------------------------

@pytest.mark.parametrize("kind, state, reason, want", [
    ("composed_tool", "rejected", f"payload.program: {COPY}", "copy"),
    # copy wins over a replay-looking or other reason joined with it
    ("job_prompt", "rejected", f"fix targets a job other than the candidate's; "
                               f"payload.prompt: {COPY}", "copy"),
    ("composed_tool", "rejected", f"replay: limit hit in 1 of 6 sessions; payload.x: {COPY}",
     "copy"),
    ("composed_tool", "rejected", "replay: limit hit in 3 of 6 sessions", "replay_limit"),
    ("composed_tool", "rejected", "replay: program error in 2 of 6 sessions", "replay"),
    ("composed_tool", "rejected", "replay: too few sessions (1 < 3)", "replay"),
    ("composed_tool", "rejected", "replay: saving below floor", "replay"),
    ("composed_tool", "rejected", "replay: no time left", "replay"),
    ("composed_tool", "rejected", "replay: internal error ValueError", "replay"),
    ("bridge_error", "rejected", "drafting call failed: oneshot timed out", "timeout"),
    ("unparseable", "rejected", "unparseable draft", "unparseable"),
    ("error", "rejected", "internal error: KeyError", "limits"),
    ("reducer", "rejected", "reducer search: no_runs", "limits"),
    ("config", "rejected", "config key 'x' is not on the allowlist", "limits"),
    ("job_prompt", "shadow_failed", "shadow decisions differ", "shadow"),
    ("job_prompt", "rolled_back", "cost went up", "trial"),
    ("job_prompt", "kept", "", ""),
    ("job_prompt", "trial", "", ""),
    ("job_prompt", "shadow_pending", "", ""),
    ("config", "applied", "", ""),
    ("new_reach", "proposed", "", ""),
    ("config", "rollback_pending", "", ""),
    # a copy reason on a record that is not rejected is not a copy rejection
    ("job_prompt", "shadow_failed", f"payload.prompt: {COPY}", "shadow"),
])
def test_reason_class(kind, state, reason, want):
    assert reason_class(rec(kind, state, reason, 1.0)) == want


def test_copy_fields_lists_paths_without_payload_prefix():
    r = rej("reducer", f"payload.steps[0].keep_hosts[1]: {COPY}; payload.program: scanner "
                       f"finding x; payload.program: {COPY}; payload.steps[0].keep_hosts[1]: "
                       f"{COPY}", 1.0)
    assert copy_fields(r) == ["steps[0].keep_hosts[1]", "program"]


def test_copy_fields_empty_for_other_rejections():
    assert copy_fields(rej("composed_tool", "replay: no time left", 1.0)) == []
    # a copy text on a non-rejected record names no fields
    assert copy_fields(rec("job_prompt", "shadow_failed", f"payload.prompt: {COPY}", 1.0)) == []


def test_copy_fields_drop_model_written_keys():
    """A path segment is a payload key the model chose (parameters.properties.<k>), so
    it could carry copied text. Only identifier-shaped segments are sent."""
    r = rej("composed_tool", f"payload.parameters.properties.Bank of X <a@b.c>: {COPY}; "
                             f"payload.parameters.properties.ids.<key>: {COPY}", 1.0)
    assert copy_fields(r) == ["?", "parameters.properties.ids.<key>"]


def test_input_hash_is_sha256_of_prompt_and_skill_text():
    job = {"prompt": "P", "name": "n"}
    assert input_hash(job, "S") == hashlib.sha256(json.dumps(["P", "S"]).encode()).hexdigest()
    assert input_hash(None, None) == hashlib.sha256(json.dumps([None, None]).encode()).hexdigest()
    assert input_hash({"prompt": "P", "name": "other"}, "S") == input_hash(job, "S")
    assert input_hash({"prompt": "Q"}, "S") != input_hash(job, "S")
    assert input_hash(job, "T") != input_hash(job, "S")


# --- backoff ----------------------------------------------------------------------------

def test_three_copy_rejections_park():      # acceptance check 5
    recs = [rej("composed_tool", f"payload.program: {COPY}", t, h)
            for t in (DAY * 1, DAY * 2, DAY * 3)]
    assert backoff(recs, KEY, h, DAY * 4) == "parked"
    assert backoff(recs, KEY, "other-hash", DAY * 4) == "skip"   # inputs changed: newest 1 day old
    assert backoff(recs, KEY, "other-hash", DAY * 11) == "ok"    # and 8 days later it may draft
    assert backoff(recs, KEY, h, DAY * 30) == "parked"           # parking has no end date


def test_parking_reads_the_newest_records_hash():
    """Only the newest's input_hash counts: older ones were drafted on older inputs."""
    recs = [rej("composed_tool", f"payload.program: {COPY}", DAY * d, ih)
            for d, ih in ((1, "old"), (2, "old"), (3, h))]
    assert backoff(recs, KEY, h, DAY * 20) == "parked"
    recs[-1].input_hash = "old"
    assert backoff(recs, KEY, h, DAY * 20) == "ok"


def test_two_same_skip_then_ok_after_7_days():
    recs = [rej("unparseable", "unparseable draft", DAY * d) for d in (1, 3)]
    assert backoff(recs, KEY, h, DAY * 3 + 1) == "skip"
    assert backoff(recs, KEY, h, DAY * 10 - 1) == "skip"   # 7 days less a second after the newest
    assert backoff(recs, KEY, h, DAY * 10) == "ok"         # 7 days after the newest
    # measured from the newest: 7 days after the OLDEST (day 8) is still inside the skip
    assert backoff(recs, KEY, h, DAY * 8 + 1) == "skip"


def test_one_rejection_is_ok():
    assert backoff([rej("unparseable", "unparseable draft", DAY)], KEY, h, DAY + 1) == "ok"
    assert backoff([], KEY, h, DAY) == "ok"


def test_different_kinds_do_not_back_off():
    recs = [rej("composed_tool", f"payload.program: {COPY}", DAY * 1),
            rej("job_prompt", f"payload.prompt: {COPY}", DAY * 2)]
    assert backoff(recs, KEY, h, DAY * 3) == "ok"


def test_different_classes_do_not_back_off():
    recs = [rej("composed_tool", "replay: limit hit in 3 of 6 sessions", DAY * 1),
            rej("composed_tool", "replay: no time left", DAY * 2)]
    assert backoff(recs, KEY, h, DAY * 3) == "ok"


def test_only_the_newest_records_count():
    """Two old identical outcomes followed by a different one: the newest two differ."""
    recs = [rej("composed_tool", f"payload.program: {COPY}", DAY * d) for d in (1, 2)]
    recs.append(rej("job_prompt", "unparseable draft", DAY * 3))
    assert backoff(recs, KEY, h, DAY * 4) == "ok"


def test_records_order_by_time_not_list_position():
    recs = [rej("job_prompt", "unparseable draft", DAY * 3),
            rej("composed_tool", f"payload.program: {COPY}", DAY * 1),
            rej("composed_tool", f"payload.program: {COPY}", DAY * 2)]
    # newest two are the job_prompt one (day 3) and a composed one (day 2): they differ
    assert backoff(recs, KEY, h, DAY * 4) == "ok"


def test_kept_and_active_records_are_ignored():
    recs = [rej("unparseable", "unparseable draft", DAY * 1),
            rec("job_prompt", "kept", "", DAY * 2),
            rej("unparseable", "unparseable draft", DAY * 3)]
    assert backoff(recs, KEY, h, DAY * 4) == "skip"


def test_other_candidates_records_are_ignored():
    recs = [rej("unparseable", "unparseable draft", DAY * d, key="seq:other") for d in (1, 2)]
    assert backoff(recs, KEY, h, DAY * 3) == "ok"


def test_placeholders_count_under_their_tag():
    recs = [rej("bridge_error", "drafting call failed: timed out", DAY * d) for d in (1, 2)]
    assert backoff(recs, KEY, h, DAY * 3) == "skip"


def test_old_record_without_input_hash_is_not_parked():
    recs = [rej("unparseable", "unparseable draft", DAY * d, ih=None) for d in (1, 2, 3)]
    assert backoff(recs, KEY, h, DAY * 20) == "ok"


def test_old_ledger_without_input_hash_loads(tmp_path):
    (tmp_path / "ledger.json").write_text(json.dumps({"records": [{
        "id": "x1", "kind": "job_prompt", "target": "j", "candidate_key": KEY,
        "state": "rejected", "created_at": 1.0, "reason": "unparseable draft"}]}))
    (r,) = Ledger.load(tmp_path).records
    assert r.input_hash is None


# --- drafting history -------------------------------------------------------------------

def test_draft_history_newest_three_with_class_and_fields_only():
    recs = [rej("composed_tool", f"payload.program: {COPY}", DAY * 1),
            rej("composed_tool", "replay: limit hit in 3 of 6 sessions", DAY * 2),
            rec("job_prompt", "kept", "", DAY * 3),
            rej("job_prompt", f"payload.prompt: {COPY}", DAY * 4),
            rej("unparseable", "unparseable draft", DAY * 5),
            rej("unparseable", "unparseable draft", DAY * 6, key="seq:other")]
    assert draft_history(recs, KEY) == [
        {"kind": "unparseable", "state": "rejected", "reason_class": "unparseable",
         "fields": []},
        {"kind": "job_prompt", "state": "rejected", "reason_class": "copy",
         "fields": ["prompt"]},
        {"kind": "job_prompt", "state": "kept", "reason_class": "", "fields": []},
    ]
