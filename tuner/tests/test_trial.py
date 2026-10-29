from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_tuner import policy
from hermes_tuner.bridge import FakeBridge
from hermes_tuner.ledger import FixRecord
from hermes_tuner.measure import JobStats
from hermes_tuner.model import Fix, Session, ToolCall
from hermes_tuner.model import Pricing
from hermes_tuner.trial import (
    BASELINE_MAX_RUNS, Verdict, baseline_runs, calendar_events, cleanup_shadows,
    compare_calendar, compare_triage, read_intents, schedule_shadow, trial_stats,
    triage_decisions, trial_verdict)

LIVE = "3bc4e11d29b1"
GET = "mcp__google__get_gmail_message_content"
MOD = "mcp__google__modify_gmail_message_labels"
EVT = "mcp__google__manage_event"
LIVE_RUN = datetime(2026, 10, 20, 8, 0, tzinfo=timezone(timedelta(hours=1)))  # 07:00 UTC
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp()


def ids(n, prefix="m"):
    return [f"{prefix}{i}" for i in range(n)]


# --- triage -------------------------------------------------------------------

def test_triage_decisions_kinds():
    calls = [(GET, {"message_id": "a"}), (GET, {"message_id": "b"}),
             (GET, {"message_id": "c"}), (GET, {"message_id": "d"}),
             (MOD, {"message_id": "b", "add_label_ids": ["TRASH"]}),
             (MOD, {"message_id": "c", "remove_label_ids": ["INBOX"]}),
             (MOD, {"message_id": "d", "add_label_ids": ["STARRED"]})]
    assert triage_decisions(calls, []) == {"a": "KEEP", "b": "TRASH", "c": "ARCHIVE",
                                           "d": "KEEP"}


def test_triage_trash_wins_over_archive_and_intents_count():
    calls = [(MOD, {"message_id": "a", "remove_label_ids": ["INBOX"]})]
    intents = [{"tool": MOD, "args": {"message_id": "a", "add_label_ids": ["TRASH"]}},
               {"tool": MOD, "args": {"message_id": "z", "remove_label_ids": ["INBOX"]}},
               {"tool": "web_search", "args": {"message_id": "q"}}]
    assert triage_decisions(calls, intents) == {"a": "TRASH", "z": "ARCHIVE"}


def test_triage_ignores_non_string_ids_and_bad_args():
    calls = [(GET, {"message_id": 5}), (GET, {}), (MOD, {"message_id": None,
             "add_label_ids": ["TRASH"]}), (MOD, {"message_id": "a", "add_label_ids": "TRASH"})]
    assert triage_decisions(calls, [{"tool": MOD, "args": "x"}, "junk"]) == {"a": "KEEP"}


def verdicts(old_n_agree, flips=0, other_diff=0):
    old = {f"m{i}": "KEEP" for i in range(old_n_agree + flips + other_diff)}
    new = dict(old)
    for i in range(flips):
        new[f"m{i}"] = "TRASH"
    for i in range(flips, flips + other_diff):
        old[f"m{i}"] = "TRASH"
        new[f"m{i}"] = "ARCHIVE"
    return old, new


def test_keep_to_trash_is_a_flip_even_at_high_agreement():
    old, new = verdicts(99, flips=1)
    v = compare_triage(old, new)
    assert v.agreement == 0.99 and v.flips == ["m0"] and v.ok is False


def test_keep_to_archive_is_a_flip():
    v = compare_triage({"a": "KEEP", "b": "KEEP"}, {"a": "ARCHIVE", "b": "KEEP"})
    assert v.flips == ["a"] and not v.ok


def test_trash_to_keep_is_not_a_flip():
    v = compare_triage({"a": "TRASH"}, {"a": "KEEP"})
    assert v.flips == [] and v.agreement == 0.0 and not v.ok


def test_agreement_boundary():
    old, new = verdicts(9, other_diff=1)
    v = compare_triage(old, new)
    assert v.agreement == 0.9 and v.ok is True
    old, new = verdicts(8, other_diff=2)
    v = compare_triage(old, new)
    assert v.agreement == 0.8 and v.ok is False


def test_messages_only_new_version_saw_are_ignored():
    old = {"a": "KEEP", "b": "KEEP", "c": "TRASH"}
    new = {"a": "KEEP", "b": "KEEP"}  # new skipped c (already checked)
    assert compare_triage(old, new) == Verdict(True, 1.0, [])
    # an id only the new version saw counts only if it trashes or archives it: the
    # shadow runs before the live run, so the old version had that mail and kept it
    assert compare_triage({"a": "KEEP"}, {"a": "KEEP", "x": "KEEP"}).ok
    assert compare_triage({"a": "KEEP"}, {"a": "KEEP", "x": "TRASH"}).flips == ["x"]


def test_no_common_messages_after_old_decided_is_inconclusive():
    """F5: the old run decided something and the shadow saw none of it: not a pass."""
    v = compare_triage({"a": "KEEP"}, {"b": "KEEP"})
    assert v.ok is False and v.reason == "no common messages" and v.flips == []
    assert compare_triage({"a": "KEEP"}, {}).ok is False


def test_no_decisions_by_the_old_run_is_ok():
    assert compare_triage({}, {}) == Verdict(True, 1.0, [])
    assert compare_triage({}, {"b": "KEEP"}) == Verdict(True, 1.0, [])


# --- calendar -----------------------------------------------------------------

def create(summary, start="2026-10-21T09:00:00"):
    return (EVT, {"action": "create", "summary": summary, "start_time": start})


def test_calendar_events_only_creates():
    calls = [create("Dentist"), (EVT, {"action": "delete", "summary": "X",
             "start_time": "2026-10-21"}), (EVT, {"action": "create", "summary": 3,
             "start_time": "2026-10-21"}), (EVT, {"action": "create", "summary": "S"})]
    intents = [{"tool": EVT, "args": {"action": "create", "summary": "Lunch",
                "start_time": "2026-10-22T12:00:00"}}]
    assert calendar_events(calls, intents) == {("dentist", "2026-10-21"),
                                               ("lunch", "2026-10-22")}


def test_calendar_missing_event_fails():
    old = calendar_events([create("A"), create("B", "2026-10-22T10:00")], [])
    new = calendar_events([create("A")], [])
    v = compare_calendar(old, new)
    assert v.ok is False and v.flips == ["b|2026-10-22"] and v.agreement == 0.5


def test_calendar_extra_event_in_new_is_fine_and_empty_old_is_one():
    assert compare_calendar({("a", "d")}, {("a", "d"), ("b", "d")}) == Verdict(True, 1.0, [])
    assert compare_calendar(set(), {("b", "d")}) == Verdict(True, 1.0, [])


def test_calendar_title_case_and_space_insensitive():
    old = calendar_events([create("Dentist  ")], [])
    new = calendar_events([create("  dentist", "2026-10-21T15:30")], [])
    assert compare_calendar(old, new).ok is True


# --- trial verdict --------------------------------------------------------------

# $1 per million for every token kind: a run of N fresh input tokens costs N / 1e6.
UNIT = Pricing(model="unit", input_per_m=1.0, output_per_m=1.0, cache_read_per_m=None,
               cache_write_per_m=None, source="test", source_url=None, version="unit",
               fetched_at=0.0)


def runs_at(usd, n=10):
    """`n` baseline breakdowns [input, cache read, cache write, output] costing `usd` at UNIT."""
    return [[round(usd * 1e6), 0, 0, 0]] * n


def rec(**kw):
    base = dict(id="f1", kind="job_prompt", target=LIVE, candidate_key="k", state="trial",
                created_at=1.0, applied_at=1000.0, baseline_runs=runs_at(0.10))
    base.update(kw)
    return FixRecord(**base)


def stats(runs=5, cost=0.05, tokens=1000.0):
    return JobStats(runs, cost, tokens, 3.0, 100.0)


def test_trial_continue_until_five_runs_then_judges():
    assert trial_verdict(rec(), None, UNIT) == "continue"
    assert trial_verdict(rec(), stats(runs=4, cost=0.01), UNIT) == "continue"
    assert trial_verdict(rec(), stats(runs=5, cost=0.09), UNIT) == "keep"
    assert trial_verdict(rec(), stats(runs=5, cost=0.10), UNIT) == "rollback"
    assert trial_verdict(rec(), stats(runs=5, cost=0.11), UNIT) == "rollback"


def test_baseline_is_the_median_of_stored_runs_at_the_given_prices():
    r = rec(baseline_runs=[[100_000, 0, 0, 0], [80_000, 0, 0, 0], [900_000, 0, 0, 0]])
    assert trial_verdict(r, stats(cost=0.099), UNIT) == "keep"      # median 0.10, not mean
    assert trial_verdict(r, stats(cost=0.101), UNIT) == "rollback"
    double = Pricing(**{**UNIT.to_dict(), "input_per_m": 2.0})
    assert trial_verdict(r, stats(cost=0.199), double) == "keep"     # priced at `pricing`


def test_record_without_baseline_runs_continues():
    """Older records (no baseline_runs) are not judged on cost: the deadline decides."""
    assert trial_verdict(rec(baseline_runs=None), stats(cost=0.0001), UNIT) == "continue"


def test_zero_priced_medians_fall_back_to_tokens():
    free = Pricing(**{**UNIT.to_dict(), "input_per_m": 0.0, "output_per_m": 0.0})
    r = rec(baseline_runs=[[1000, 0, 0, 0]] * 3)
    assert trial_verdict(r, stats(cost=0.0, tokens=600.0), free) == "keep"
    assert trial_verdict(r, stats(cost=0.0, tokens=1400.0), free) == "rollback"
    assert trial_verdict(r, stats(cost=0.0, tokens=1000.0), free) == "rollback"


def test_prompt_fallback_only_when_both_priced_medians_are_zero():
    # no baseline runs (priced 0) but trial runs priced above 0: dearer, whatever the tokens
    assert trial_verdict(rec(baseline_runs=[]), stats(cost=0.01, tokens=1.0), UNIT) \
        == "rollback"


def sess(i, t, cost=0.1, job=LIVE, ncalls=2, tokens=None, raw=0.0):
    """`cost` sets the priced cost at UNIT (fresh input; output 0); `raw` is the billing figure."""
    prompt = round(cost * 1e6) if tokens is None else tokens
    return Session(f"s{i}", "cron", job, t, raw, prompt, 0,
                   tuple(ToolCall(f"c{j}", "web_search", (), None, 5) for j in range(ncalls)),
                   input_tokens=prompt)


def test_runs_before_applied_at_not_counted():
    applied = 1000.0
    old_runs = [sess(i, applied - 10 - i, cost=1.0) for i in range(5)]
    new_runs = [sess(10 + i, applied + 10 + i, cost=0.01) for i in range(4)]
    s = trial_stats(old_runs + new_runs, LIVE, applied, UNIT)
    assert s.runs == 4 and s.median_priced_cost == pytest.approx(0.01)
    assert trial_verdict(rec(applied_at=applied), s, UNIT) == "continue"
    boundary = sess(99, applied)  # started at the apply instant: not the new version's
    assert trial_stats([boundary], LIVE, applied, UNIT) is None


def test_baseline_runs_window_is_28_days_before_applied_at():
    applied = 100 * 86400.0
    inside = sess(1, applied - 28 * 86400, cost=0.2)       # window start is inclusive
    too_old = sess(2, applied - 28 * 86400 - 1, cost=9.0)
    at_apply = sess(3, applied, cost=9.0)                   # window end is exclusive
    no_calls = sess(5, applied - 9, cost=9.0, ncalls=0)     # an outage, not a run
    other = sess(4, applied - 5, cost=9.0, job="other")
    got = baseline_runs([inside, too_old, at_apply, no_calls, other], applied,
                        lambda s: s.job_id == LIVE)
    assert got == [[200_000, 0, 0, 0]]
    assert baseline_runs([], applied, lambda s: True) == []


def test_baseline_runs_keeps_the_60_most_recent():
    assert BASELINE_MAX_RUNS == 60
    applied = 100 * 86400.0
    ss = [sess(i, applied - 1000 - i, tokens=i) for i in range(61)]  # s0 is the newest
    got = baseline_runs(list(reversed(ss)), applied, lambda s: True)
    assert len(got) == 60 and sorted(b[0] for b in got) == list(range(60))


def test_billing_change_does_not_keep_a_dearer_fix():
    """Reviewer scenario (F1): baseline runs billed $0.12 raw, trial runs billed $0
    (subscription) with 50% more tokens. Priced on tokens the fix is dearer: rollback."""
    applied = 100 * 86400.0
    base_runs = [sess(i, applied - (i + 1) * 3600, tokens=100_000, raw=0.12) for i in range(10)]
    trial_runs = [sess(20 + i, applied + (i + 1) * 3600, tokens=150_000, raw=0.0)
                  for i in range(5)]
    sessions = base_runs + trial_runs
    r = rec(applied_at=applied, live_from=applied,
            baseline_runs=baseline_runs(sessions, applied, lambda s: s.job_id == LIVE))
    assert trial_verdict(r, trial_stats(sessions, LIVE, applied, UNIT), UNIT) == "rollback"


# --- fake bridge ids ------------------------------------------------------------

def test_fake_bridge_ids_stay_distinct_after_remove():
    b = FakeBridge()
    first = b.create_job("p1", "30m", "n1", "local", None)
    second = b.create_job("p2", "30m", "n2", "local", None)
    assert b.remove_job(first["id"]) is True
    third = b.create_job("p3", "30m", "n3", "local", None)
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert b.jobs[second["id"]]["prompt"] == "p2"
    assert b.jobs[third["id"]]["prompt"] == "p3"


# --- schedule_shadow --------------------------------------------------------------

def live_job(toolsets=("web",), next_run=LIVE_RUN.isoformat()):
    return {"id": LIVE, "name": "triage", "prompt": "old", "next_run_at": next_run,
            "enabled_toolsets": None if toolsets is None else list(toolsets)}


def job_fix(kind="job_prompt", add=()):
    if kind == "job_prompt":
        return Fix("f1", kind, LIVE, {"prompt": "NEW PROMPT", "add_toolsets": list(add)}, "k")
    return Fix("f1", kind, LIVE, {"name": "tuned_x", "program": "p", "calls": [],
                                  "job_prompt": "NEW PROMPT"}, "k")


def test_shadow_scheduled_before_live_run_with_local_delivery(tmp_path):
    bridge = FakeBridge(jobs={LIVE: live_job()})
    sid = schedule_shadow(job_fix("composed_tool"), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    job = bridge.jobs[sid]
    assert job["deliver"] == "local" and job["name"] == policy.SHADOW_PREFIX + "f1"
    assert job["prompt"] == "NEW PROMPT"
    assert job["enabled_toolsets"] == ["web", "tuner"]
    when = datetime.fromisoformat(job["schedule"])
    assert when.utcoffset() is not None
    assert when == datetime(2026, 10, 20, 6, 40, tzinfo=timezone.utc)  # 08:00+01:00 minus 20 min
    entry = json.loads((tmp_path / "shadow_jobs.json").read_text())[sid]
    assert entry["fix_id"] == "f1" and entry["live_job_id"] == LIVE
    assert entry["run_at"] == when.timestamp() and isinstance(entry["run_at"], float)
    assert stat.S_IMODE(os.stat(tmp_path / "shadow_jobs.json").st_mode) == 0o600
    assert bridge.jobs[LIVE]["prompt"] == "old"  # live job untouched


def test_shadow_job_prompt_fix_uses_its_prompt_and_toolsets(tmp_path):
    bridge = FakeBridge(jobs={LIVE: live_job()})
    sid = schedule_shadow(job_fix(add=["tuner"]), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    assert bridge.jobs[sid]["prompt"] == "NEW PROMPT"
    assert bridge.jobs[sid]["enabled_toolsets"] == ["web", "tuner"]
    sid2 = schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    assert bridge.jobs[sid2]["enabled_toolsets"] == ["web"]
    entries = json.loads((tmp_path / "shadow_jobs.json").read_text())
    assert set(entries) == {sid, sid2}  # second schedule keeps the first entry


def test_shadow_carries_the_live_jobs_skills_model_and_context(tmp_path):
    """F6: the shadow runs like the live job (skills, model, provider, ...), but never
    with its delivery, origin, repeat or session attachment."""
    job = live_job() | {
        "skills": ["inbox-rules"], "skill": "legacy", "model": "m-1", "provider": "p-1",
        "base_url": "https://example.invalid/v1", "workdir": "/srv/w", "script": "s.py",
        "context_from": ["abc123abc123"], "deliver": "telegram", "origin": {"chat": 1},
        "repeat": 3, "no_agent": True, "attach_to_session": True}
    bridge = FakeBridge(jobs={LIVE: job})
    sid = schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    (call,) = [c for c in bridge.calls if c[0] == "create_job"]
    extra = call[1][5]
    assert extra == {k: job[k] for k in ("skills", "skill", "model", "provider", "base_url",
                                         "workdir", "context_from", "script")}
    shadow = bridge.jobs[sid]
    assert shadow["skills"] == ["inbox-rules"] and shadow["model"] == "m-1"
    assert shadow["deliver"] == "local"
    assert not {"origin", "repeat", "no_agent", "attach_to_session"} & set(shadow)


def test_shadow_passes_only_present_non_none_fields(tmp_path):
    bridge = FakeBridge(jobs={LIVE: live_job() | {"model": None, "skills": ["a"]}})
    schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    (call,) = [c for c in bridge.calls if c[0] == "create_job"]
    assert call[1][5] == {"skills": ["a"]}


def test_create_job_refuses_keys_off_the_allowlist():
    from hermes_tuner import bridge_entry
    for bad in ({"deliver": "telegram"}, {"origin": {}}, {"repeat": 1}, {"no_agent": True},
                {"attach_to_session": True}):
        with pytest.raises(ValueError):
            FakeBridge().create_job("p", "x", "n", "local", None, bad)
        with pytest.raises(ValueError):
            bridge_entry.create_job("p", "x", "n", "local", None, bad)


def test_shadow_none_toolsets_stay_none(tmp_path):
    bridge = FakeBridge(jobs={LIVE: live_job(toolsets=None)})
    sid = schedule_shadow(job_fix("composed_tool"), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    assert bridge.jobs[sid]["enabled_toolsets"] is None


@pytest.mark.parametrize("next_run", [None, "", "not a date", "2026-10-20T08:00:00",
                                      "2026-10-19T12:10:00+00:00"])
def test_shadow_skipped_when_time_unusable(tmp_path, next_run):
    # last case: live run 10 min away, so shadow time (live - 20 min) is already past
    bridge = FakeBridge(jobs={LIVE: live_job(next_run=next_run)})
    with pytest.raises(ValueError):
        schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    assert not [c for c in bridge.calls if c[0] == "create_job"]
    assert not (tmp_path / "shadow_jobs.json").exists()


def test_shadow_exactly_now_is_rejected_and_just_after_is_accepted(tmp_path):
    live_at = datetime.fromtimestamp(NOW + 20 * 60, timezone.utc)
    bridge = FakeBridge(jobs={LIVE: live_job(next_run=live_at.isoformat())})
    with pytest.raises(ValueError):
        schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    live_at = datetime.fromtimestamp(NOW + 20 * 60 + 1, timezone.utc)
    bridge.jobs[LIVE]["next_run_at"] = live_at.isoformat()
    assert schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)


def test_shadow_needs_a_prompt(tmp_path):
    bridge = FakeBridge(jobs={LIVE: live_job()})
    bad = Fix("f1", "composed_tool", LIVE, {"job_prompt": None}, "k")
    with pytest.raises(ValueError):
        schedule_shadow(bad, bridge.jobs[LIVE], bridge, tmp_path, NOW)
    with pytest.raises(ValueError):
        schedule_shadow(Fix("f1", "config", LIVE, {}, "k"), bridge.jobs[LIVE], bridge,
                        tmp_path, NOW)


def test_shadow_record_failure_removes_the_job(tmp_path, monkeypatch):
    import hermes_tuner.trial as trial
    bridge = FakeBridge(jobs={LIVE: live_job()})

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(trial, "atomic_write", boom)
    with pytest.raises(OSError):
        schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    assert list(bridge.jobs) == [LIVE]


# --- cleanup ---------------------------------------------------------------------

def shadow_setup(tmp_path, run_at, name=None):
    bridge = FakeBridge(jobs={LIVE: live_job()})
    sid = bridge.create_job("p", "x", name or policy.SHADOW_PREFIX + "f1", "local",
                            None)["id"]
    (tmp_path / "shadow_jobs.json").write_text(json.dumps(
        {sid: {"fix_id": "f1", "live_job_id": LIVE, "run_at": run_at}}))
    return bridge, sid


def test_cleanup_removes_stale_shadow_jobs(tmp_path):
    bridge, sid = shadow_setup(tmp_path, NOW - 3601)
    assert cleanup_shadows(bridge, tmp_path, NOW) == [sid]
    assert sid not in bridge.jobs and LIVE in bridge.jobs
    entry = json.loads((tmp_path / "shadow_jobs.json").read_text())[sid]
    assert entry["removed_at"] == NOW and entry["run_at"] == NOW - 3601
    assert cleanup_shadows(bridge, tmp_path, NOW + 10) == []  # idempotent


def test_cleanup_keeps_recent_and_upcoming(tmp_path):
    bridge, sid = shadow_setup(tmp_path, NOW - 3600)  # exactly 1 h: not over
    assert cleanup_shadows(bridge, tmp_path, NOW) == []
    assert sid in bridge.jobs
    assert "removed_at" not in json.loads((tmp_path / "shadow_jobs.json").read_text())[sid]
    bridge, sid = shadow_setup(tmp_path, NOW + 600)
    assert cleanup_shadows(bridge, tmp_path, NOW) == []


def test_cleanup_ignores_jobs_without_the_prefix(tmp_path):
    bridge, sid = shadow_setup(tmp_path, NOW - 7200, name="triage-copy")
    assert cleanup_shadows(bridge, tmp_path, NOW) == []
    assert sid in bridge.jobs


def test_cleanup_removes_orphan_prefixed_job_without_entry(tmp_path):
    bridge = FakeBridge(jobs={LIVE: live_job()})
    sid = bridge.create_job("p", "x", policy.SHADOW_PREFIX + "gone", "local", None)["id"]
    assert cleanup_shadows(bridge, tmp_path, NOW) == [sid]


def test_cleanup_marks_entry_whose_job_already_vanished(tmp_path):
    bridge, sid = shadow_setup(tmp_path, NOW - 7200)
    bridge.remove_job(sid)
    assert cleanup_shadows(bridge, tmp_path, NOW) == []
    assert json.loads((tmp_path / "shadow_jobs.json").read_text())[sid]["removed_at"] == NOW


def test_cleanup_bridge_failure_leaves_entry_unmarked(tmp_path):
    bridge, sid = shadow_setup(tmp_path, NOW - 7200)
    bridge.fail = {"remove_job"}
    assert cleanup_shadows(bridge, tmp_path, NOW) == []
    assert "removed_at" not in json.loads((tmp_path / "shadow_jobs.json").read_text())[sid]


# --- read_intents ------------------------------------------------------------------

ROOT_ID = "cron_3bc4e11d29b1_20261020_064000"
CHILD_ID = "20261020_064500_abcdef"


def test_read_intents_concatenates_skips_bad_lines_and_missing(tmp_path):
    d = tmp_path / "shadow"
    d.mkdir()
    (d / f"{ROOT_ID}.jsonl").write_text(
        json.dumps({"tool": "a"}) + "\n{broken\n\n[1]\n" + json.dumps({"tool": "b"}) + "\n")
    (d / f"{CHILD_ID}.jsonl").write_text(json.dumps({"tool": "c"}) + "\n")
    got = read_intents(tmp_path, [ROOT_ID, "cron_000000000000_20261020_000000", CHILD_ID, ROOT_ID])
    assert [i["tool"] for i in got] == ["a", "b", "c"]  # duplicate id read once
    assert read_intents(tmp_path / "nowhere", [ROOT_ID]) == []


def test_read_intents_rejects_path_injection(tmp_path):
    (tmp_path / "secret.jsonl").write_text(json.dumps({"tool": "leak"}) + "\n")
    (tmp_path / "shadow").mkdir()
    for bad in ["../secret", "../../secret", "/etc/passwd", "secret", ROOT_ID + "/../x",
                ROOT_ID + "\n", 5, None]:
        assert read_intents(tmp_path, [bad]) == []


def test_shadow_toolsets_equal_what_apply_sets_on_the_live_job(tmp_path):
    from hermes_tuner.ledger import Ledger, apply, job_after_for
    for payload_extra, expected in [({"add_toolsets": []}, ["web"]), ({}, ["web", "tuner"]),
                                    ({"add_toolsets": ["tuner"]}, ["web", "tuner"])]:
        fix = Fix("f1", "composed_tool", LIVE,
                  {"name": "tuned_x", "description": "d", "parameters": {"type": "object"},
                   "calls": ["web_search"], "mutating": False, "program": "print(1)\n",
                   "job_prompt": "NEW PROMPT", **payload_extra}, "k")
        shadow_bridge = FakeBridge(jobs={LIVE: live_job()})
        sid = schedule_shadow(fix, shadow_bridge.jobs[LIVE], shadow_bridge, tmp_path / f"s{len(expected)}{len(payload_extra)}", NOW)
        live_bridge = FakeBridge(jobs={LIVE: live_job()})
        root = tmp_path / f"t{len(expected)}{len(payload_extra)}"
        ledger = Ledger.load(root)
        ledger.add(FixRecord("f1", fix.kind, LIVE, "k", "shadow_pending", 1.0), fix)
        apply(fix, ledger, live_bridge, root, 100.0, skills_root=tmp_path)
        assert shadow_bridge.jobs[sid]["enabled_toolsets"] == expected
        assert live_bridge.jobs[LIVE]["enabled_toolsets"] == expected
        assert job_after_for(fix, live_job())["enabled_toolsets"] == expected
        assert shadow_bridge.jobs[sid]["prompt"] == live_bridge.jobs[LIVE]["prompt"]


def test_corrupt_shadow_file_is_not_overwritten(tmp_path):
    bad = tmp_path / "shadow_jobs.json"
    bad.write_text("{not json")
    bridge = FakeBridge(jobs={LIVE: live_job()})
    with pytest.raises(ValueError):
        schedule_shadow(job_fix(), bridge.jobs[LIVE], bridge, tmp_path, NOW)
    assert bad.read_text() == "{not json"
    assert list(bridge.jobs) == [LIVE]  # the created shadow job was rolled back
    with pytest.raises(ValueError):
        cleanup_shadows(bridge, tmp_path, NOW)
    assert bad.read_text() == "{not json"


def test_new_version_trashing_a_message_old_left_alone_is_a_flip():
    """Catches: comparing only common ids, so mail the old version kept by not
    touching it can be trashed or archived by the new version unnoticed."""
    old = {"m1": "KEEP", "m2": "KEEP"}
    v = compare_triage(old, {"m1": "KEEP", "m2": "KEEP", "m3": "TRASH", "m4": "ARCHIVE"})
    assert not v.ok and v.flips == ["m3", "m4"]
    assert compare_triage(old, {"m1": "KEEP", "m2": "KEEP", "m3": "KEEP"}).ok
    v = compare_triage({}, {"m9": "TRASH"})  # old decided nothing at all
    assert not v.ok and v.flips == ["m9"]


# --- the shadow's composed-tool calls ----------------------------------------------

OK = json.dumps({"result": {"messages": []}})
ERR = json.dumps({"error": "output exceeds 50000 bytes"})


@pytest.mark.parametrize("text, failed", [
    (OK, False),
    (json.dumps({"result": [1, 2]}), False),
    ('{"result": [{"message_id": "m1", "content": "cut sh', False),  # Hermes truncated it
    (ERR, True),
    (json.dumps({"result": {"error": "mcp__x is not callable"}}), True),
    ("", True),
    (None, True),
    ("Unknown tool: tuned_a", True),
    (json.dumps(["result"]), True),
])
def test_call_failed(text, failed):
    from hermes_tuner.trial import call_failed
    assert call_failed(text) is failed


def test_tool_check_fails_a_shadow_that_never_called_its_tool():
    """A composed tool that is never called proves nothing, however well the shadow's
    decisions agree with the live run's."""
    from hermes_tuner.trial import tool_check
    assert tool_check([(GET, "b"), (MOD, "ok")], "tuned_a", frozenset()) == \
        "shadow never called tuned_a"


def test_tool_check_fails_on_any_failed_call_to_its_tool():
    from hermes_tuner.trial import tool_check
    results = [("tuned_a", OK), ("tuned_a", ERR), (GET, "b"), ("tuned_a", OK)]
    assert tool_check(results, "tuned_a", frozenset()) == "tuned_a failed 1 of 3 calls"


def test_tool_check_passes_when_every_call_to_its_tool_succeeded():
    from hermes_tuner.trial import tool_check
    assert tool_check([("tuned_a", OK), (GET, "b"), ("tuned_a", OK)], "tuned_a",
                      frozenset()) is None


def test_tool_check_fails_when_the_shadow_used_another_fixs_shadow_tool():
    """The 2026-10-23 07:40 shadow: one call to its own tool, seven to the calendar
    fix's shadow-only tool. Its decisions rest on a tool that does not go live."""
    from hermes_tuner.trial import tool_check
    results = [("tuned_a", OK)] + [("tuned_b", OK)] * 7
    assert tool_check(results, "tuned_a", frozenset()) == \
        "shadow used tuned_b, which is not live and not this fix's tool"


@pytest.mark.parametrize("own", ["tuned_a", None])  # None: a job_prompt fix's shadow
def test_tool_check_ignores_refused_and_live_composed_tools(own):
    from hermes_tuner.trial import tool_check
    refused = json.dumps({"error": "tuned_b is in another fix's shadow trial"})
    results = [("tuned_b", refused), ("tuned_live", OK)]
    if own is not None:
        results.append((own, OK))
    assert tool_check(results, own, frozenset({"tuned_live"})) is None


def test_tool_check_names_every_foreign_tool_once_sorted():
    from hermes_tuner.trial import tool_check
    results = [("tuned_c", OK), ("tuned_b", OK), ("tuned_c", OK)]
    assert tool_check(results, None, frozenset()) == \
        "shadow used tuned_b, tuned_c, which are not live and not this fix's tool"


# --- reducer shadows (Task 8) -------------------------------------------------------

def reducer_fix(base=None):
    p = {"tool": "mcp__google__get_gmail_message_content", "job": "cc68e24f21cd",
         "steps": [{"op": "collapse_ws"}]}
    if base is not None:
        p["_base"] = base
    return Fix("r1", "reducer", "cc68e24f21cd", p, "bytes:x")


@pytest.mark.parametrize("toolsets", [("web",), None])
def test_reducer_shadow_runs_the_live_prompt_and_toolsets(tmp_path, toolsets):
    """The reducer changes results, not the job: the shadow runs the job as drafted
    against (the payload's _base, which _shadow sets) with the live toolsets."""
    job = live_job(toolsets)
    bridge = FakeBridge(jobs={LIVE: job})
    sid = schedule_shadow(reducer_fix({"prompt": "old", "enabled_toolsets": job["enabled_toolsets"]}),
                          job, bridge, tmp_path, NOW)
    assert bridge.jobs[sid]["prompt"] == "old"
    assert bridge.jobs[sid]["enabled_toolsets"] == job["enabled_toolsets"]
    assert [c for c in bridge.calls if c[0] == "update_job"] == []


@pytest.mark.parametrize("base", [None, {}, {"prompt": ""}, {"prompt": 5}])
def test_reducer_shadow_without_a_base_prompt_is_refused(tmp_path, base):
    bridge = FakeBridge(jobs={LIVE: live_job()})
    with pytest.raises(ValueError):
        schedule_shadow(reducer_fix(base), live_job(), bridge, tmp_path, NOW)
    assert list(bridge.jobs) == [LIVE]


# --- early rollback (Task 11) --------------------------------------------------------

TOOL_OK = json.dumps({"result": {"messages": ["m1", "m2"]}})
TOOL_ERR = json.dumps({"error": "output exceeds 50000 bytes"})


def test_early_failures_batch_tool():
    """The live calendar batch tool: 14 of 41 calls failed on the output cap."""
    from hermes_tuner.trial import early_failures
    own = [TOOL_OK] * 27 + [TOOL_ERR] * 14
    assert early_failures("composed_tool", own, 0, [], []) == (14, 41)
    # a reducer's errors and re-reads do not count for a composed tool
    assert early_failures("composed_tool", own, 5, [9], [0]) == (14, 41)


def test_reducer_rereads_vs_baseline():
    """Baseline [0, 0, 3]: mean 1, median 0, max 3. Only the trial run with 2 re-reads
    exceeds the mean. `>=` mean gives 2, median gives 2, max gives 0."""
    from hermes_tuner.trial import early_failures
    assert early_failures("reducer", [], 0, [1, 2, 0], [0, 0, 3], reduced=7) == (1, 7)
    # reduce errors add to it; composed-tool results do not count for a reducer
    assert early_failures("reducer", [TOOL_ERR] * 3, 2, [1, 2, 0], [0, 0, 3],
                          reduced=7) == (3, 7)
    # no baseline runs: re-reads have nothing to exceed
    assert early_failures("reducer", [], 1, [4], [], reduced=0) == (1, 0)


def test_early_failures_job_prompt_has_none():
    from hermes_tuner.trial import early_failures
    assert early_failures("job_prompt", [TOOL_ERR] * 3, 4, [5], [0], reduced=9) == (0, 0)


def test_rereads_counts_repeats_of_the_tool_per_ref():
    """Sum over refs of (count - 1), for calls to `tool` with a ref."""
    from hermes_tuner.trial import rereads

    def call(i, name, ref):
        return ToolCall(f"c{i}", name, ("message_id",), ref, 10)
    calls = [call(0, GET, "m1"), call(1, GET, "m1"), call(2, GET, "m1"),
             call(3, GET, "m2"), call(4, GET, "m2"), call(5, GET, "m3"),
             call(6, GET, None), call(7, GET, None), call(8, MOD, "m1"), call(9, MOD, "m1")]
    s = Session(id="s", source="cron", job_id=LIVE, started_at=0.0, est_cost=0.0,
                prompt_tokens=0, output_tokens=0, calls=tuple(calls))
    assert rereads(s, GET) == 3   # m1: 2, m2: 1, m3: 0; None refs and MOD ignored
    assert rereads(s, MOD) == 1
    assert rereads(s, EVT) == 0
