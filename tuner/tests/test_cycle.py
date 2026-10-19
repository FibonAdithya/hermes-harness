"""The daily cycle, end to end on a real temp state.db, a FakeBridge and a tmp root."""
from __future__ import annotations

import fcntl
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory import make_db
from hermes_tuner import policy
from hermes_tuner.__main__ import main
from hermes_tuner.bridge import FakeBridge
from hermes_tuner.cycle import CycleConfig, run_cycle, weekly_line
from hermes_tuner.ledger import FixRecord, Ledger, _tool_files, _write_tool_dir
from hermes_tuner.model import Fix, Session, ToolCall
from hermes_tuner.proposals import change_hash

TRIAGE, CAL = "3bc4e11d29b1", "cc68e24f21cd"
DAY = 86400.0
NOW = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc).timestamp()  # a Monday, ISO week 43
SEARCH = "mcp__google__search_gmail_messages"
GET = "mcp__google__get_gmail_message_content"
MOD = "mcp__google__modify_gmail_message_labels"
RESULT_SENTINEL = "ZZ_SENTINEL_9f2c"
ARG_SENTINEL = "ZZ_SENTINEL_q"
ALL_METHODS = {"scan", "get_job", "list_jobs", "update_job", "create_job", "remove_job",
               "config_get", "config_set", "config_set_json", "oneshot", "restart_gateway"}
CFG_KEY = "compression.threshold"


def sid(job, t):
    return f"cron_{job}_" + datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d_%H%M%S")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def triage_calls(n):
    """Search -> read -> modify. Every result and argument value carries a sentinel."""
    return [
        (SEARCH, {"query": f"{ARG_SENTINEL} newer_than:1d"},
         f"{RESULT_SENTINEL} results: " + "x" * 400),
        (GET, {"message_id": f"{ARG_SENTINEL}-{n}"}, f"{RESULT_SENTINEL} body " + "y" * 800),
        (MOD, {"message_id": f"{ARG_SENTINEL}-{n}", "add_label_ids": ["STARRED"]},
         f"{RESULT_SENTINEL} ok"),
    ]


def triage_runs(days=range(1, 13), cost=0.10, start_hour=7):
    """Daily triage runs at 07:00 UTC, `days` days before NOW's date."""
    base = NOW - 3 * 3600 + start_hour * 3600  # 07:00 on NOW's date
    return [{"id": sid(TRIAGE, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
             "cost": cost, "in": 100_000, "out": 1000, "calls": triage_calls(d)} for d in days]


def cal_runs(days, cost):
    base = NOW - 3 * 3600 + 6 * 3600
    return [{"id": sid(CAL, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
             "cost": cost, "in": 50_000, "out": 500,
             "calls": [("mcp__google__get_events", {"calendar_id": "primary"}, "events")]}
            for d in days]


def jobs():
    return {TRIAGE: {"id": TRIAGE, "name": "morning-briefing", "prompt": "Triage the inbox.",
                     "schedule": "0 7 * * *", "enabled_toolsets": ["google"], "enabled": True,
                     "next_run_at": iso(NOW + 4 * 3600)},
            CAL: {"id": CAL, "name": "calendar-sync", "prompt": "Sync the calendar.",
                  "schedule": "0 6 * * *", "enabled_toolsets": ["google"], "enabled": True,
                  "next_run_at": iso(NOW + 3 * 3600)}}


def reply(obj, tokens=(1000, 200)):
    return {"text": json.dumps(obj), "input_tokens": tokens[0], "output_tokens": tokens[1]}


JOB_PROMPT_REPLY = {"kind": "job_prompt", "target": TRIAGE,
                    "payload": {"prompt": "Triage the inbox; skip mail already starred.",
                                "add_toolsets": []}}


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "hermes"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / ".bundled_manifest").write_text("bundled-one:abc\n")
    (home / "broker" / "state").mkdir(parents=True)
    cfg = CycleConfig(root=home / "tuner", db=home / "state.db",
                      broker_state=home / "broker" / "state", skills_root=home / "skills",
                      dry_run=False)
    return cfg


def build(cfg, sessions):
    make_db(cfg.db, sessions)


def add_record(cfg, rec, fix):
    led = Ledger.load(cfg.root)
    led.add(rec, fix)
    return led


def oneshots(bridge):
    return [c for c in bridge.calls if c[0] == "oneshot"]


def mutating(bridge):
    return [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


# --- one active fix per job ----------------------------------------------------------

def test_cycle_respects_one_active_fix_per_job(env):
    build(env, triage_runs())
    fix = Fix("20261018-aaaa0001", "job_prompt", TRIAGE, {"prompt": "p", "add_toolsets": []},
              "seq:x")
    add_record(env, FixRecord(fix.id, fix.kind, TRIAGE, fix.candidate_key, "trial",
                              NOW - DAY, applied_at=NOW - DAY), fix)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    assert oneshots(bridge) == []
    assert Ledger.load(env.root).get(fix.id).state == "trial"
    assert report.errors == []


def test_cycle_drafts_and_shadows_when_job_is_free(env):
    """The control for the test above: the same history without an active fix drafts."""
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    assert len(oneshots(bridge)) == 1
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "shadow_pending" and rec.target == TRIAGE and rec.spend_usd > 0
    assert [c for c in bridge.calls if c[0] == "create_job"]
    assert report.shadowed == [rec.id] and report.errors == []


# --- budget ------------------------------------------------------------------------------

def kept_cal_fix(cfg):
    """A kept calendar fix whose 28-day savings are large (baseline $0.50, now $0.10)."""
    fix = Fix("20260929-cccc0001", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
              "seq:cal")
    add_record(cfg, FixRecord(fix.id, fix.kind, CAL, fix.candidate_key, "kept",
                              NOW - 20 * DAY, applied_at=NOW - 20 * DAY,
                              baseline_cost=0.50, live_from=NOW - 20 * DAY), fix)
    return cal_runs(range(21, 41), 0.50) + cal_runs(range(1, 20), 0.10)


def config_trial(cfg, applied_days_ago=8):
    fix = Fix("20261011-dddd0001", "config", CFG_KEY, {"key": CFG_KEY, "value": 0.7},
              "schema_lookup")
    led = add_record(cfg, FixRecord(fix.id, "config", CFG_KEY, fix.candidate_key, "trial",
                                    NOW - applied_days_ago * DAY,
                                    applied_at=NOW - applied_days_ago * DAY,
                                    needs_restart=True,
                                    live_from=NOW - applied_days_ago * DAY), fix)
    vdir = led.version_dir(fix.id)
    (vdir / "before.json").write_text(json.dumps({"key": CFG_KEY, "value": 0.5}))
    (vdir / "after.json").write_text(json.dumps({"key": CFG_KEY, "value": 0.7}))
    return fix


@pytest.mark.parametrize("week_spend, drafts", [(0.99, False), (0.95, False), (0.85, True)])
def test_budget_exhausted_skips_drafting_but_still_evaluates(env, week_spend, drafts):
    # Triage runs cost $0.10 both before and after the config fix: not cheaper -> rollback.
    build(env, triage_runs(range(1, 30)) + kept_cal_fix(env))
    fix = config_trial(env)
    spent = Fix("20261019-eeee0001", "job_prompt", CAL, {}, "seq:old")
    add_record(env, FixRecord(spent.id, "job_prompt", CAL, "seq:old", "rejected",
                              NOW - 3600, spend_usd=week_spend), spent)
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7},
                        oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    # projected = DRAFT_EST_USD 0.02 + triage median run_cost 0.10 = 0.12
    assert bool(oneshots(bridge)) is drafts
    assert Ledger.load(env.root).get(fix.id).state == "rolled_back"
    assert bridge.config[CFG_KEY] == 0.5
    assert fix.id in report.rolled_back
    # the config change needed a restart, so undoing it does too (NOW is 04:00 London, quiet)
    assert bridge.calls.count(("restart_gateway", ())) == 1


@pytest.mark.parametrize("with_savings, drafts", [(False, False), (True, True)])
def test_spend_exceeding_savings_pauses_drafting(env, with_savings, drafts):
    sessions = triage_runs()
    if with_savings:
        sessions += kept_cal_fix(env)
    build(env, sessions)
    # $0.10 spent 10 days ago: last ISO week, so this week's spend is 0 (cap not hit).
    old = Fix("20261009-ffff0001", "job_prompt", CAL, {}, "seq:old")
    add_record(env, FixRecord(old.id, "job_prompt", CAL, "seq:old", "rejected",
                              NOW - 10 * DAY, spend_usd=0.10), old)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    run_cycle(env, bridge, NOW)
    assert bool(oneshots(bridge)) is drafts


# --- bridge failure ----------------------------------------------------------------------

def test_bridge_failure_applies_nothing_and_reports(env):
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)],
                        fail=ALL_METHODS)
    report = run_cycle(env, bridge, NOW)
    assert report.errors
    assert all(r.state != "trial" for r in Ledger.load(env.root).records)
    line = (env.root / "outbox" / "weekly-2026-W43.txt").read_text()
    assert "; errors: " in line and line.count("\n") <= 1


def test_bridge_failure_mid_apply_records_bridge_error(env):
    """A drafted config fix whose apply fails: bridge_error, never trial."""
    sessions = []
    for d in range(1, 22):  # 21 chat sessions with two tool_describe calls each
        t = NOW - d * DAY
        sessions.append({"id": f"2026{d:04d}_000000_ab12cd", "source": "telegram",
                         "started_at": t, "cost": 0.05, "in": 1000, "out": 10,
                         "calls": [("tool_describe", {"name": "a"}, "s" * 300),
                                   ("tool_describe", {"name": "b"}, "s" * 300)]})
    build(env, sessions)
    cfg_reply = {"kind": "config", "target": CFG_KEY, "payload": {"key": CFG_KEY, "value": 0.7}}
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.5}, oneshot_script=[reply(cfg_reply)],
                        fail={"config_set"})
    report = run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "bridge_error" and report.applied == [] and report.errors
    assert bridge.config[CFG_KEY] == 0.5
    # one failure (a bridge_error record and its log entry) is counted once
    assert "; errors: 1 (" in (env.root / "outbox" / "weekly-2026-W43.txt").read_text()


# --- lock ----------------------------------------------------------------------------------

def test_second_concurrent_run_exits(env, monkeypatch, capsys):
    build(env, triage_runs())
    env.root.mkdir(parents=True)
    with open(env.root / ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
        report = run_cycle(env, bridge, NOW)
        assert report.skipped == "already running" and bridge.calls == []
        monkeypatch.setenv("HERMES_HOME", str(env.root.parent))
        assert main(["run"], bridge=bridge, now=NOW) == 0
        assert "already running" in capsys.readouterr().out
        assert bridge.calls == []
    # once released, a run proceeds
    run_cycle(env, bridge, NOW)
    assert bridge.calls


# --- dry run ---------------------------------------------------------------------------------

def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def test_dry_run_applies_nothing(env, monkeypatch, capsys):
    build(env, triage_runs(range(1, 30)))
    config_trial(env)  # due for a verdict
    # an approved proposal and a stale shadow job
    change = {"type": "mcp_include_add", "server": "google", "tool": "x_tool"}
    pdir = env.root / "proposals"
    pdir.mkdir(parents=True)
    (pdir / "p1.json").write_text(json.dumps({"id": "p1", "change": change, "why": "w",
                                              "hash": change_hash(change)}))
    (env.broker_state / "approved_proposals.json").write_text(json.dumps(
        [{"proposal_id": "p1", "hash": change_hash(change), "approved_at": NOW - 100}]))
    js = jobs()
    js["0123456789ab"] = {"id": "0123456789ab", "name": policy.SHADOW_PREFIX + "old",
                          "next_run_at": iso(NOW - 5 * DAY), "enabled": True}
    bridge = FakeBridge(jobs=js, config={CFG_KEY: 0.7, "mcp_servers.google.tools.include": []},
                        oneshot_script=[reply(JOB_PROMPT_REPLY)])
    before = tree(env.root)
    monkeypatch.setenv("HERMES_HOME", str(env.root.parent))
    assert main(["run", "--dry-run"], bridge=bridge, now=NOW) == 0
    out = json.loads(capsys.readouterr().out)
    assert mutating(bridge) == []
    assert tree(env.root) == before
    assert any(p.startswith("draft ") for p in out["planned"])
    assert any(p.startswith("rollback ") for p in out["planned"])


# --- weekly line --------------------------------------------------------------------------

def test_weekly_line_once_per_iso_week(env):
    build(env, triage_runs())
    outbox = env.root / "outbox"
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    run_cycle(env, bridge, NOW)
    first = (outbox / "weekly-2026-W43.txt").read_text()
    assert first.startswith("Hermes tuner: saved ~$") and first.count("\n") == 1
    (outbox / "weekly-2026-W43.txt").rename(outbox / "weekly-2026-W43.sent")  # broker sent it
    run_cycle(env, FakeBridge(jobs=jobs()), NOW + DAY)  # Tuesday: same ISO week
    assert sorted(p.name for p in outbox.iterdir()) == ["weekly-2026-W43.sent"]
    (outbox / "weekly-2026-W43.sent").unlink()  # outbox cleaned: the cycle state still knows
    run_cycle(env, FakeBridge(jobs=jobs()), NOW + 6 * DAY)  # Sunday: same ISO week
    assert sorted(p.name for p in outbox.iterdir()) == []
    run_cycle(env, FakeBridge(jobs=jobs()), NOW + 7 * DAY)  # next Monday
    assert sorted(p.name for p in outbox.iterdir()) == ["weekly-2026-W44.txt"]


def run_session(job, t, cost, n_calls=1):
    return Session(id=sid(job, t), source="cron", job_id=job, started_at=t, est_cost=cost,
                   prompt_tokens=1000, output_tokens=10,
                   calls=tuple(ToolCall(f"c{i}", GET, ("message_id",), None, 10)
                               for i in range(n_calls)))


@pytest.mark.parametrize("live_days_ago, saved", [(10, "0.21"), (2.5, "0.14")])
def test_weekly_saving_is_baseline_minus_actual(tmp_path, live_days_ago, saved):
    """Baseline from before applied_at; runs counted from live_from (10 days ago: all
    three this week; 2.5 days ago, after a late restart: only two)."""
    led = Ledger(tmp_path)
    applied = NOW - 10 * DAY
    led.records.append(FixRecord("20261009-abcd0001", "job_prompt", TRIAGE, "seq:x", "kept",
                                 applied, applied_at=applied, baseline_cost=0.12,
                                 decided_at=NOW - 2 * DAY, live_from=NOW - live_days_ago * DAY))
    # waiting for a restart: never counted
    led.records.append(FixRecord("20261009-abcd0003", "job_prompt", CAL, "seq:c", "trial",
                                 applied, applied_at=applied, needs_restart=True))
    sessions = [run_session(TRIAGE, applied - d * DAY, 0.12) for d in range(1, 6)]  # baseline
    sessions += [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (8, 9)]  # before the week
    sessions += [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (1, 2, 3)]  # this week
    sessions += [run_session(TRIAGE, NOW - 4 * DAY, 0.30, n_calls=0)]  # outage: not a run
    sessions += [run_session(CAL, applied - d * DAY, 0.05) for d in range(1, 6)]
    sessions += [run_session(CAL, NOW - DAY, 0.01)]  # its fix awaits a restart: not counted
    line = weekly_line(led, sessions, NOW)
    assert line == (f"Hermes tuner: saved ~${saved} this week (ESTIMATE), 0 applied, 1 kept, "
                    "0 rolled back; tuner spent $0.00")


def test_weekly_line_counts_errors(tmp_path):
    led = Ledger(tmp_path)
    led.records.append(FixRecord("20261018-abcd0002", "config", CFG_KEY, "k", "bridge_error",
                                 NOW - DAY, spend_usd=0.03))
    # the cycle logs each failure once, bridge_error records included: not counted twice
    line = weekly_line(led, [], NOW, errors=1)
    assert line.endswith("; tuner spent $0.03; errors: 1 (see ~/.hermes/tuner/ledger.json)")
    assert "errors" not in weekly_line(led, [], NOW, errors=0)


# --- spec 4.3: nothing from results or argument values reaches the drafting model -------------

def test_no_result_text_reaches_drafting_model(env):
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)] * 3)
    run_cycle(env, bridge, NOW)
    calls = oneshots(bridge)
    assert len(calls) >= 1  # a real candidate was detected and drafted
    for _m, (instructions, user_input, _max) in calls:
        for sentinel in (RESULT_SENTINEL, ARG_SENTINEL):
            assert sentinel not in instructions
            assert sentinel not in user_input
    # the request did carry the call shape (names and keys), so it is not empty
    assert SEARCH in calls[0][1][1] and "query" in calls[0][1][1]


# --- shadow evaluation -----------------------------------------------------------------------

SHADOW_JOB = "abcdef012345"


SHADOW_T = NOW - 3 * 3600 - DAY + 6 * 3600 + 40 * 60  # 06:40 UTC yesterday
SCHEDULED_AT = NOW - DAY - 3600                          # 02:00 UTC yesterday
TOOL = {"name": "tuned_triage", "description": "search and read in one step",
        "parameters": {}, "calls": [SEARCH, GET], "mutating": False,
        "program": "def main(args, call):\n    return 1\n", "job_prompt": "new prompt"}


def shadow_setup(env, old_mod, new_mod, composed=False, live=True):
    """A shadow-pending triage fix whose shadow ran at 06:40 yesterday and (if `live`)
    a live run at 07:00. `composed`: a composed_tool fix needing a restart, with the
    tool dir written as at shadow scheduling."""
    shadow_t = SHADOW_T
    live_t = shadow_t + 20 * 60
    sessions = triage_runs(range(2, 30))
    sessions.append({"id": sid(SHADOW_JOB, shadow_t), "source": "cron", "started_at": shadow_t,
                     "cost": 0.08, "in": 80_000, "out": 800,
                     "calls": [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]})
    if live:
        sessions.append({"id": sid(TRIAGE, live_t), "source": "cron", "started_at": live_t,
                         "cost": 0.10, "in": 100_000, "out": 1000,
                         "calls": [(GET, {"message_id": "m1"}, "b"),
                                   (GET, {"message_id": "m2"}, "b")] + old_mod})
    build(env, sessions)
    if composed:
        fix = Fix("20261018-bbbb0002", "composed_tool", TRIAGE, dict(TOOL), "seq:x")
        spec, program = _tool_files(fix.payload)
        _write_tool_dir(env.root / "tools", spec["name"], spec, program)
    else:
        fix = Fix("20261018-bbbb0001", "job_prompt", TRIAGE,
                  {"prompt": "new prompt", "add_toolsets": []}, "seq:x")
    add_record(env, FixRecord(fix.id, fix.kind, TRIAGE, "seq:x", "shadow_pending",
                              NOW - DAY - 3600, spend_usd=0.01, needs_restart=composed), fix)
    (env.root / "shadow_jobs.json").write_text(json.dumps({SHADOW_JOB: {
        "fix_id": fix.id, "live_job_id": TRIAGE, "run_at": shadow_t,
        "created_at": NOW - DAY - 3600, "scheduled_at": SCHEDULED_AT}}))
    shadow_dir = env.root / "shadow"
    shadow_dir.mkdir()
    (shadow_dir / f"{sid(SHADOW_JOB, shadow_t)}.jsonl").write_text(
        "\n".join(json.dumps({"tool": MOD, "args": a}) for a in new_mod))
    return fix


def test_shadow_pass_applies_with_raw_baseline_cost(env):
    fix = shadow_setup(env, old_mod=[(MOD, {"message_id": "m2", "add_label_ids": ["TRASH"]}, "ok")],
                       new_mod=[{"message_id": "m2", "add_label_ids": ["TRASH"]}])
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "trial" and rec.applied_at == NOW
    assert rec.live_from == NOW  # a job_prompt fix needs no restart: live at once
    assert rec.baseline_cost == pytest.approx(0.10)
    assert bridge.jobs[TRIAGE]["prompt"] == "new prompt"
    assert fix.id in report.applied


def test_shadow_flip_fails_without_applying(env):
    fix = shadow_setup(env, old_mod=[],
                       new_mod=[{"message_id": "m1", "remove_label_ids": ["INBOX"]}])
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and "m1" not in rec.reason
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


# --- CLI detect and scrub --------------------------------------------------------------------

def test_cli_scrub_then_detect_from_fixture(env, capsys):
    build(env, triage_runs())
    out = env.db.parent / "fixture.json.gz"
    assert main(["scrub", "--db", str(env.db), "--out", str(out)]) == 0
    raw = gzip.decompress(out.read_bytes())
    assert RESULT_SENTINEL.encode() not in raw and ARG_SENTINEL.encode() not in raw
    assert main(["detect", "--db", str(env.db), "--fixture", str(out),
                 "--now", "2026-10-19"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert 1 <= len(lines) <= 10
    assert all(json.loads(x)["job_id"] == TRIAGE for x in lines)
    # anchored a year later, the 28-day window is empty
    assert main(["detect", "--db", str(env.db), "--now", "2027-10-19"]) == 0
    assert capsys.readouterr().out == ""


# --- restart-aware shadows and trials (spec 4.5, 4.7) ------------------------------------

def state(cfg):
    return json.loads((cfg.root / "cycle_state.json").read_text())


COMPOSED_REPLY = {"kind": "composed_tool", "target": TRIAGE, "payload": dict(TOOL)}


def test_composed_shadow_requests_restart(env):
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(COMPOSED_REPLY)])
    report = run_cycle(env, bridge, NOW)  # 04:00 London, quiet, no job within 15 min
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "shadow_pending" and rec.needs_restart
    assert (env.root / "tools" / "tuned_triage" / "program.py").exists()
    assert bridge.calls.count(("restart_gateway", ())) == 1 and report.restarted
    assert state(env)["restart_times"] == [NOW] and state(env)["pending_restart"] is False
    (entry,) = json.loads((env.root / "shadow_jobs.json").read_text()).values()
    assert entry["scheduled_at"] == NOW


def test_shadow_that_ran_before_restart_fails(env):
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True)
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason == "shadow ran before restart"
    assert not (env.root / "tools" / "tuned_triage").exists()
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


@pytest.mark.parametrize("restarts, ok", [
    ([SHADOW_T - 3600], True),               # between scheduling and the shadow
    ([SHADOW_T], True),                      # exactly at the shadow start: allowed
    ([SCHEDULED_AT - DAY], False),           # before scheduling: tool never loaded
    ([SCHEDULED_AT], False),                 # at scheduling: not after the tool dir
    ([SHADOW_T + 60], False),                # after the shadow ran
    ([SCHEDULED_AT - DAY, SHADOW_T + 60], False),
    ([SCHEDULED_AT - DAY, SHADOW_T - 3600, NOW - 600], True),  # later restarts don't matter
])
def test_shadow_after_restart_goes_live_from_next_restart(env, restarts, ok):
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True)
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": restarts}))
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    if not ok:
        assert rec.state == "shadow_failed" and rec.reason == "shadow ran before restart"
        return
    assert rec.state == "trial" and rec.applied_at == NOW
    # applying needs a restart; NOW is inside the window, so it restarts and the fix is
    # live from then
    assert bridge.calls.count(("restart_gateway", ())) == 1
    assert rec.live_from == NOW and state(env)["restart_times"][-1] == NOW


def test_shadow_with_no_live_run_fails_after_three_days(env):
    fix = shadow_setup(env, old_mod=[], new_mod=[], live=False)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "shadow_pending"  # still waiting
    run_cycle(env, FakeBridge(jobs=jobs()), NOW + 3 * DAY)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason == "no live run to compare"


@pytest.mark.parametrize("live_from", [NOW - 2.5 * DAY, None])
def test_trial_counts_runs_from_live_from(env, live_from):
    # 9 runs since applied_at at $0.10 against a $0.20 baseline would be a "keep";
    # only 2 runs since live_from (or none, before the restart) is "continue".
    build(env, triage_runs(range(1, 30)))
    fix = Fix("20261009-aaaa0002", "job_prompt", TRIAGE, {"prompt": "p", "add_toolsets": []},
              "seq:x")
    add_record(env, FixRecord(fix.id, fix.kind, TRIAGE, "seq:x", "trial", NOW - 10 * DAY,
                              applied_at=NOW - 10 * DAY, baseline_cost=0.20,
                              needs_restart=True, live_from=live_from), fix)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def test_ledger_load_backfills_live_from(tmp_path):
    recs = [{"id": "a1", "kind": "job_prompt", "target": TRIAGE, "candidate_key": "k",
             "state": "trial", "created_at": 1.0, "applied_at": 5.0, "needs_restart": False},
            {"id": "a2", "kind": "config", "target": CFG_KEY, "candidate_key": "k",
             "state": "trial", "created_at": 1.0, "applied_at": 6.0, "needs_restart": True}]
    (tmp_path / "ledger.json").write_text(json.dumps({"records": recs}))
    led = Ledger.load(tmp_path)
    assert led.get("a1").live_from == 5.0 and led.get("a2").live_from is None


def test_outcome_of_line_writing_run_is_in_exactly_one_weekly_line(env):
    build(env, triage_runs(range(1, 30)))
    config_trial(env)  # due: rolled back at NOW by the run that writes W43
    run_cycle(env, FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7},
                              oneshot_script=[reply(JOB_PROMPT_REPLY)]), NOW)
    w43 = (env.root / "outbox" / "weekly-2026-W43.txt").read_text()
    run_cycle(env, FakeBridge(jobs=jobs()), NOW + 7 * DAY)
    w44 = (env.root / "outbox" / "weekly-2026-W44.txt").read_text()
    assert ", 1 rolled back;" in w43 and ", 0 rolled back;" in w44
    assert state(env)["weekly_written_at"] == NOW + 7 * DAY


# --- restart gate at the cycle level ----------------------------------------------------------

T_OUT = NOW + 9 * 3600                                                   # 13:00 London
T_IN = datetime(2026, 10, 20, 1, 30, tzinfo=timezone.utc).timestamp()   # 02:30 London


def approve_proposal(cfg, pid="p1"):
    change = {"type": "mcp_include_add", "server": "google", "tool": "x_tool"}
    pdir = cfg.root / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{pid}.json").write_text(json.dumps({"id": pid, "change": change, "why": "w",
                                                  "hash": change_hash(change)}))
    (cfg.broker_state / "approved_proposals.json").write_text(json.dumps(
        [{"proposal_id": pid, "hash": change_hash(change), "approved_at": NOW - 100}]))


INCLUDE = "mcp_servers.google.tools.include"


def test_restart_waits_for_window_then_restarts_once(env):
    build(env, triage_runs())
    approve_proposal(env)
    bridge = FakeBridge(jobs=jobs(), config={INCLUDE: []})
    run_cycle(env, bridge, T_OUT)
    rec = Ledger.load(env.root).get("p1")
    assert rec.state == "applied" and rec.needs_restart and rec.live_from is None
    assert ("restart_gateway", ()) not in bridge.calls
    assert state(env)["pending_restart"] is True
    bridge2 = FakeBridge(jobs=jobs(), config={INCLUDE: ["x_tool"]})
    run_cycle(env, bridge2, T_IN)
    assert bridge2.calls.count(("restart_gateway", ())) == 1
    assert state(env)["pending_restart"] is False
    assert Ledger.load(env.root).get("p1").live_from == T_IN
    bridge3 = FakeBridge(jobs=jobs(), config={INCLUDE: ["x_tool"]})
    run_cycle(env, bridge3, T_IN + 60)  # flag cleared: no second restart
    assert ("restart_gateway", ()) not in bridge3.calls


def test_restart_in_window_waits_for_quiet(env):
    chat = {"id": "20261020_012000_ab12cd", "source": "telegram", "started_at": T_IN - 600,
            "cost": 0.01, "in": 10, "out": 1, "calls": [("todo", {}, "ok")]}
    build(env, triage_runs() + [chat])
    approve_proposal(env)
    bridge = FakeBridge(jobs=jobs(), config={INCLUDE: []})
    run_cycle(env, bridge, T_IN)
    assert ("restart_gateway", ()) not in bridge.calls
    assert state(env)["pending_restart"] is True
    assert Ledger.load(env.root).get("p1").live_from is None


def test_global_trial_days_count_from_live_from(env):
    build(env, triage_runs(range(1, 30)))
    fix = config_trial(env)  # applied 8 days ago ...
    led = Ledger.load(env.root)
    led.get(fix.id).live_from = NOW - 2 * DAY  # ... but live (restarted) only 2 days ago
    led.save()
    run_cycle(env, FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7}), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def test_later_unrelated_restart_does_not_fail_waiting_shadow(env):
    """Reviewer scenario: a valid shadow waits for its live run while an unrelated
    restart happens; the later restart must not fail it."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True, live=False)
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": [SHADOW_T - 3600]}))
    approve_proposal(env)  # an unrelated restart-needing change, applied this run
    b1 = FakeBridge(jobs=jobs(), config={INCLUDE: []})
    run_cycle(env, b1, NOW)
    assert b1.calls.count(("restart_gateway", ())) == 1
    assert state(env)["restart_times"] == [SHADOW_T - 3600, NOW]
    assert Ledger.load(env.root).get(fix.id).state == "shadow_pending"
    sessions = triage_runs(range(2, 30))  # now the live run has happened
    sessions.append({"id": sid(SHADOW_JOB, SHADOW_T), "source": "cron", "started_at": SHADOW_T,
                     "cost": 0.08, "in": 80_000, "out": 800,
                     "calls": [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]})
    sessions.append({"id": sid(TRIAGE, SHADOW_T + 1200), "source": "cron",
                     "started_at": SHADOW_T + 1200, "cost": 0.10, "in": 100_000, "out": 1000,
                     "calls": [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]})
    env.db.unlink()
    build(env, sessions)
    run_cycle(env, FakeBridge(jobs=jobs(), config={INCLUDE: ["x_tool"]}), NOW + 3600)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def test_restart_times_pruned_to_28_days(env):
    build(env, triage_runs())
    (env.root).mkdir(parents=True)
    (env.root / "cycle_state.json").write_text(json.dumps(
        {"restart_times": [NOW - 29 * DAY, NOW - 27 * DAY], "pending_restart": True}))
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert state(env)["restart_times"] == [NOW - 27 * DAY, NOW]
