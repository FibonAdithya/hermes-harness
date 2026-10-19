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
from hermes_tuner.ledger import FixRecord, Ledger
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
                              baseline_cost=0.50), fix)
    return cal_runs(range(21, 41), 0.50) + cal_runs(range(1, 20), 0.10)


def config_trial(cfg, applied_days_ago=8):
    fix = Fix("20261011-dddd0001", "config", CFG_KEY, {"key": CFG_KEY, "value": 0.7},
              "schema_lookup")
    led = add_record(cfg, FixRecord(fix.id, "config", CFG_KEY, fix.candidate_key, "trial",
                                    NOW - applied_days_ago * DAY,
                                    applied_at=NOW - applied_days_ago * DAY,
                                    needs_restart=True), fix)
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


def test_weekly_saving_is_baseline_minus_actual(tmp_path):
    led = Ledger(tmp_path)
    applied = NOW - 10 * DAY
    led.records.append(FixRecord("20261009-abcd0001", "job_prompt", TRIAGE, "seq:x", "kept",
                                 applied, applied_at=applied, baseline_cost=0.12,
                                 decided_at=NOW - 2 * DAY))
    sessions = [run_session(TRIAGE, applied - d * DAY, 0.12) for d in range(1, 6)]  # baseline
    sessions += [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (8, 9)]  # before the week
    sessions += [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (1, 2, 3)]  # this week
    sessions += [run_session(TRIAGE, NOW - 4 * DAY, 0.30, n_calls=0)]  # outage: not a run
    sessions += [run_session(CAL, NOW - DAY, 0.01)]  # another job: not counted
    line = weekly_line(led, sessions, NOW)
    assert line == ("Hermes tuner: saved ~$0.21 this week (ESTIMATE), 0 applied, 1 kept, "
                    "0 rolled back; tuner spent $0.00")


def test_weekly_line_counts_errors(tmp_path):
    led = Ledger(tmp_path)
    led.records.append(FixRecord("20261018-abcd0002", "config", CFG_KEY, "k", "bridge_error",
                                 NOW - DAY, spend_usd=0.03))
    line = weekly_line(led, [], NOW, errors=1)
    assert line.endswith("; tuner spent $0.03; errors: 2 (see ~/.hermes/tuner/ledger.json)")


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


def shadow_setup(env, old_mod, new_mod):
    """A shadow-pending triage fix whose shadow ran at 06:40 yesterday and a live run at 07:00."""
    shadow_t = NOW - 3 * 3600 - DAY + 6 * 3600 + 40 * 60
    live_t = shadow_t + 20 * 60
    sessions = triage_runs(range(2, 30))
    sessions.append({"id": sid(SHADOW_JOB, shadow_t), "source": "cron", "started_at": shadow_t,
                     "cost": 0.08, "in": 80_000, "out": 800,
                     "calls": [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]})
    sessions.append({"id": sid(TRIAGE, live_t), "source": "cron", "started_at": live_t,
                     "cost": 0.10, "in": 100_000, "out": 1000,
                     "calls": [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]
                     + old_mod})
    build(env, sessions)
    fix = Fix("20261018-bbbb0001", "job_prompt", TRIAGE,
              {"prompt": "new prompt", "add_toolsets": []}, "seq:x")
    add_record(env, FixRecord(fix.id, "job_prompt", TRIAGE, "seq:x", "shadow_pending",
                              NOW - DAY - 3600, spend_usd=0.01), fix)
    (env.root / "shadow_jobs.json").write_text(json.dumps({SHADOW_JOB: {
        "fix_id": fix.id, "live_job_id": TRIAGE, "run_at": shadow_t,
        "created_at": NOW - DAY - 3600}}))
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
