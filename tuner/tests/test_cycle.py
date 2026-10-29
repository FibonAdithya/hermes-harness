"""The daily cycle, end to end on a real temp state.db, a FakeBridge and a tmp root."""
from __future__ import annotations

import fcntl
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory import FLAT, FLAT_RATE, V4_PRO, make_db
from hermes_tuner import policy
from hermes_tuner.__main__ import main
from hermes_tuner.bridge import FakeBridge as _FakeBridge
from hermes_tuner.cycle import CycleConfig, run_cycle, weekly_line
from hermes_tuner.history import History, HistoryUnavailable
from hermes_tuner.ledger import FixRecord, Ledger, _tool_files, write_shadow_tool
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
               "config_get", "config_set", "config_set_json", "oneshot", "restart_gateway",
               "config_settings"}
CFG_KEY = "compression.threshold"
PLUGINS = "plugins.enabled"


def FakeBridge(*args, config=None, **kw):
    """A FakeBridge whose Hermes config enables the tuner plugin (F4), unless the
    test's own config says otherwise."""
    return _FakeBridge(*args, config={PLUGINS: ["hermes-tuner"], **(config or {})}, **kw)


def heartbeat(cfg, registered_at):
    """What the plugin writes at register() (F4)."""
    cfg.root.mkdir(parents=True, exist_ok=True)
    (cfg.root / "plugin_heartbeat.json").write_text(
        json.dumps({"pid": 4242, "registered_at": registered_at}))


def sid(job, t):
    return f"cron_{job}_" + datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d_%H%M%S")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def triage_calls(n):
    """Search -> read -> modify, one call per model turn (a sequence candidate only
    saves turns when its calls sit on separate turns). Every result and argument value
    carries a sentinel."""
    return [
        (SEARCH, {"query": f"{ARG_SENTINEL} newer_than:1d"},
         f"{RESULT_SENTINEL} results: " + "x" * 400, 1),
        (GET, {"message_id": f"{ARG_SENTINEL}-{n}"}, f"{RESULT_SENTINEL} body " + "y" * 800, 2),
        (MOD, {"message_id": f"{ARG_SENTINEL}-{n}", "add_label_ids": ["STARRED"]},
         f"{RESULT_SENTINEL} ok", 3),
    ]


def tok(usd):
    """Token count whose priced cost at FLAT (FakeBridge's default list) is `usd`."""
    return round(usd * 1e6 / FLAT_RATE)


def triage_runs(days=range(1, 13), cost=0.10, start_hour=7, raw=0.0, tokens=None, base=None):
    """Daily triage runs at 07:00 UTC, `days` days before NOW's date (after it for
    negative days). `cost` is the priced cost (set through tokens); `raw` is the
    billing figure, $0 under subscription billing, which the tuner ignores."""
    tokens = tok(cost) if tokens is None else tokens
    base = NOW - 3 * 3600 + start_hour * 3600 if base is None else base
    return [{"id": sid(TRIAGE, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
             "cost": raw, "in": tokens - 1000, "out": 1000, "calls": triage_calls(d)}
            for d in days]


def cal_runs(days, cost):
    base = NOW - 3 * 3600 + 6 * 3600
    return [{"id": sid(CAL, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
             "cost": 0.0, "in": tok(cost) - 500, "out": 500,
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
    heartbeat(cfg, NOW - 60)  # the gateway (and the plugin) last started a minute ago
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
    return [c for c in bridge.calls if c[0] in _FakeBridge.MUTATING]


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
    led = Ledger.load(env.root)
    (rec,) = led.records
    assert rec.state == "shadow_pending" and rec.target == TRIAGE and rec.spend_usd > 0
    assert [c for c in bridge.calls if c[0] == "create_job"]
    assert report.shadowed == [rec.id] and report.errors == []
    # F8: the drafted-against job is recorded, and never written to Hermes
    assert led.fix(rec.id).payload["_base"] == {"prompt": "Triage the inbox.",
                                                "enabled_toolsets": ["google"]}
    assert "_base" not in json.dumps(bridge.calls)


# --- budget ------------------------------------------------------------------------------

def kept_cal_fix(cfg):
    """A kept calendar fix whose 28-day savings are large (baseline $0.50, now $0.10)."""
    fix = Fix("20260929-cccc0001", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
              "seq:cal")
    add_record(cfg, FixRecord(fix.id, fix.kind, CAL, fix.candidate_key, "kept",
                              NOW - 20 * DAY, applied_at=NOW - 20 * DAY,
                              baseline_runs=[brk(0.50, 500)] * 10, live_from=NOW - 20 * DAY), fix)
    return cal_runs(range(21, 41), 0.50) + cal_runs(range(1, 20), 0.10)


def config_trial(cfg, applied_days_ago=8, baseline_runs=None):
    """A config trial whose stored baseline is 20 runs at $0.10 (triage_runs' price)."""
    fix = Fix("20261011-dddd0001", "config", CFG_KEY, {"key": CFG_KEY, "value": 0.7},
              "schema_lookup")
    led = add_record(cfg, FixRecord(fix.id, "config", CFG_KEY, fix.candidate_key, "trial",
                                    NOW - applied_days_ago * DAY,
                                    applied_at=NOW - applied_days_ago * DAY,
                                    needs_restart=True,
                                    live_from=NOW - applied_days_ago * DAY,
                                    baseline_runs=([brk(0.10)] * 20 if baseline_runs is None
                                                   else baseline_runs)), fix)
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
    # projected = DRAFT_EST_USD 0.02 + triage median priced cost 0.10 = 0.12
    assert bool(oneshots(bridge)) is drafts
    assert Ledger.load(env.root).get(fix.id).state == "rolled_back"
    assert bridge.config[CFG_KEY] == 0.5
    assert fix.id in report.rolled_back
    # the config change needed a restart, so undoing it does too (NOW is 04:00 London, quiet)
    assert bridge.calls.count(("restart_gateway", ())) == 1


@pytest.mark.parametrize("spent, with_savings, drafts", [
    (0.04, False, True),   # the 2026-10-21 run: failed drafts alone must not pause drafting
    (1.00, False, True),   # at the allowance, not over it
    (1.10, False, False),  # over the allowance with nothing saved: paused
    (1.10, True, True),    # over the allowance but savings cover it
])
def test_spend_exceeding_savings_and_allowance_pauses_drafting(env, spent, with_savings, drafts):
    """Catches: pausing on spend > savings alone (any spend before the first saving
    then pauses the tuner for four weeks), or ignoring savings above the allowance."""
    sessions = triage_runs()
    if with_savings:
        sessions += kept_cal_fix(env)
    build(env, sessions)
    # spent 10 days ago: last ISO week, so this week's spend is 0 (cap not hit).
    old = Fix("20261009-ffff0001", "job_prompt", CAL, {}, "seq:old")
    add_record(env, FixRecord(old.id, "job_prompt", CAL, "seq:old", "rejected",
                              NOW - 10 * DAY, spend_usd=spent), old)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    assert bool(oneshots(bridge)) is drafts
    if not drafts:
        assert report.budget == ("paused: 28-day spend $1.1000 > 28-day savings $0.0000 "
                                 "and > $1.00 starting allowance (ESTIMATE)")


def test_pause_allowance_matches_the_weekly_cap():
    """The allowance is the weekly cap's size (spec 6). Catches: a changed constant."""
    assert policy.PAUSE_ALLOWANCE_USD == policy.WEEKLY_CAP_USD == 1.0


# --- real setting names ------------------------------------------------------------------

def chat_schema_sessions():
    """21 chat sessions with two tool_describe calls each: a schema_lookup candidate
    worth more than MIN_DRAFT_MONTHLY_USD."""
    out = []
    for d in range(1, 22):
        out.append({"id": f"2026{d:04d}_000000_ab12cd", "source": "telegram",
                    "started_at": NOW - d * DAY, "cost": 0.05, "in": 10_000, "out": 10,
                    "calls": [("tool_describe", {"name": "a"}, "s" * 300),
                              ("tool_describe", {"name": "b"}, "s" * 300)]})
    return out


SETTINGS_CONFIG = {CFG_KEY: 0.5, "tools.tool_search.enabled": "auto",
                   "compression.threshold_tokens": None, "display.x": 1,
                   "terminal.backend": "local"}


def test_draft_request_lists_the_changeable_settings(env):
    """Catches: the model shown no setting names (it guessed one on 2026-10-21)."""
    build(env, chat_schema_sessions())
    cfg_reply = {"kind": "config", "target": CFG_KEY, "payload": {"key": CFG_KEY, "value": 0.7}}
    bridge = FakeBridge(jobs=jobs(), config=SETTINGS_CONFIG, oneshot_script=[reply(cfg_reply)])
    run_cycle(env, bridge, NOW)
    ((_m, (_i, user, _mt)),) = oneshots(bridge)
    assert json.loads(user)["settings"] == {CFG_KEY: 0.5, "tools.tool_search.enabled": "auto"}


def test_cycle_filters_the_settings_the_bridge_returns(env):
    """The tuner does not trust the bridge's filtering: a forbidden key or a
    non-finite value it returns is neither shown to the model nor accepted.
    Catches: the cycle using the bridge's result unfiltered."""
    class Loose(_FakeBridge):
        def config_settings(self, prefixes):
            self._enter("config_settings", list(prefixes))
            return {CFG_KEY: 0.5, "compression.nan": float("nan"), "mcp_servers.g.x": 1}

    build(env, chat_schema_sessions())
    bad = "mcp_servers.g.x"
    cfg_reply = {"kind": "config", "target": bad, "payload": {"key": bad, "value": 2}}
    bridge = Loose(jobs=jobs(), config={PLUGINS: ["hermes-tuner"], CFG_KEY: 0.5},
                   oneshot_script=[reply(cfg_reply)])
    run_cycle(env, bridge, NOW)
    ((_m, (_i, user, _mt)),) = oneshots(bridge)
    assert json.loads(user)["settings"] == {CFG_KEY: 0.5}
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "rejected"


def test_unknown_setting_is_rejected_when_drafted(env):
    """The 2026-10-21 run: tools.tool_search.cache_schemas reached apply, which refused
    it only for having no value to roll back to. Now limits reject it, before any
    config read or write, with the reason in the ledger."""
    build(env, chat_schema_sessions())
    bad = "tools.tool_search.cache_schemas"
    cfg_reply = {"kind": "config", "target": bad, "payload": {"key": bad, "value": True}}
    bridge = FakeBridge(jobs=jobs(), config=SETTINGS_CONFIG, oneshot_script=[reply(cfg_reply)])
    report = run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "rejected"
    assert f"config key {bad!r} is not an existing Hermes setting" in rec.reason
    assert not [c for c in bridge.calls if c[0] in ("config_get", "config_set")]
    assert report.errors == [] and report.applied == []


def test_unreadable_settings_reject_config_fixes(env):
    """Fail closed: with no settings list, no config fix passes, and the failure is
    reported once."""
    build(env, chat_schema_sessions())
    cfg_reply = {"kind": "config", "target": CFG_KEY, "payload": {"key": CFG_KEY, "value": 0.7}}
    bridge = FakeBridge(jobs=jobs(), config=SETTINGS_CONFIG, oneshot_script=[reply(cfg_reply)],
                        fail={"config_settings"})
    report = run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "rejected" and "not an existing Hermes setting" in rec.reason
    assert bridge.config[CFG_KEY] == 0.5
    assert [e for e in report.errors if e.startswith("config_settings:")] and \
        len(report.errors) == 1


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
    build(env, chat_schema_sessions())
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
    env.root.mkdir(parents=True, exist_ok=True)
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
    """`cost` is the priced cost (through prompt tokens); the raw billing figure is $0."""
    return Session(id=sid(job, t), source="cron", job_id=job, started_at=t, est_cost=0.0,
                   prompt_tokens=tok(cost), output_tokens=0,
                   calls=tuple(ToolCall(f"c{i}", GET, ("message_id",), None, 10)
                               for i in range(n_calls)), input_tokens=tok(cost))


@pytest.mark.parametrize("live_days_ago, saved", [(10, "0.21"), (2.5, "0.14")])
def test_weekly_saving_is_baseline_minus_actual(tmp_path, live_days_ago, saved):
    """Baseline from before applied_at; runs counted from live_from (10 days ago: all
    three this week; 2.5 days ago, after a late restart: only two)."""
    led = Ledger(tmp_path)
    applied = NOW - 10 * DAY
    led.records.append(FixRecord("20261009-abcd0001", "job_prompt", TRIAGE, "seq:x", "kept",
                                 applied, applied_at=applied, baseline_runs=[brk(0.12, 0)] * 5,
                                 decided_at=NOW - 2 * DAY, live_from=NOW - live_days_ago * DAY))
    # waiting for a restart: never counted
    led.records.append(FixRecord("20261009-abcd0003", "job_prompt", CAL, "seq:c", "trial",
                                 applied, applied_at=applied, needs_restart=True))
    # runs before applied_at priced at $0.30: the saving uses the baseline runs stored at
    # go-live ($0.12), not a recomputed one (the history window no longer reaches back)
    sessions = [run_session(TRIAGE, applied - d * DAY, 0.30) for d in range(1, 6)]
    sessions += [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (8, 9)]  # before the week
    sessions += [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (1, 2, 3)]  # this week
    sessions += [run_session(TRIAGE, NOW - 4 * DAY, 0.30, n_calls=0)]  # outage: not a run
    sessions += [run_session(CAL, applied - d * DAY, 0.05) for d in range(1, 6)]
    sessions += [run_session(CAL, NOW - DAY, 0.01)]  # its fix awaits a restart: not counted
    line = weekly_line(led, sessions, NOW, FLAT)
    assert line == (f"Hermes tuner: saved ~${saved} this week (ESTIMATE at fake-model list "
                    "prices, fake-flat), 0 applied, 1 kept, 0 rolled back; tuner spent $0.00")


def test_tuner_spend_prices_shadow_runs_from_tokens(tmp_path):
    led = Ledger(tmp_path)
    led.records.append(FixRecord("20261018-abcd0004", "job_prompt", TRIAGE, "k", "rejected",
                                 NOW - DAY, spend_usd=0.03))
    shadow = Session(id=sid(SHADOW_JOB, NOW - DAY), source="cron", job_id=SHADOW_JOB,
                     started_at=NOW - DAY, est_cost=0.0, prompt_tokens=tok(0.05),
                     output_tokens=0, calls=(), input_tokens=tok(0.05))
    line = weekly_line(led, [shadow], NOW, FLAT, shadow_ids={SHADOW_JOB})
    assert line.endswith("; tuner spent $0.08")  # 0.03 drafting + 0.05 shadow (billed $0)


def test_weekly_line_counts_errors(tmp_path):
    led = Ledger(tmp_path)
    led.records.append(FixRecord("20261018-abcd0002", "config", CFG_KEY, "k", "bridge_error",
                                 NOW - DAY, spend_usd=0.03))
    # the cycle logs each failure once, bridge_error records included: not counted twice
    line = weekly_line(led, [], NOW, FLAT, errors=1)
    assert line.endswith("; tuner spent $0.03; errors: 1 "
                         "(see ~/.hermes/tuner/cycle_state.json and ledger.json)")
    assert "errors" not in weekly_line(led, [], NOW, FLAT, errors=0)


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


SETUP_SESSIONS: dict = {}  # env.root -> the sessions shadow_setup built


TOOL_OK = json.dumps({"result": {"messages": ["m1", "m2"]}})
TOOL_ERR = json.dumps({"error": "output exceeds 50000 bytes"})


def shadow_setup(env, old_mod, new_mod, composed=False, live=True, base_runs=None,
                 shadow_ids=("m1", "m2"), tool_results=(TOOL_OK,), extra_calls=(),
                 legacy_tool=False):
    """A shadow-pending triage fix whose shadow ran at 06:40 yesterday and (if `live`)
    a live run at 07:00. `composed`: a composed_tool fix needing a restart, with the
    tool dir written as at shadow scheduling, whose shadow called tuned_triage once per
    `tool_results` entry (each the text the call returned). `extra_calls` go on the
    end of the shadow's calls. `legacy_tool`: the tool dir as written before tool.json
    named its fix."""
    shadow_t = SHADOW_T
    live_t = shadow_t + 20 * 60
    sessions = triage_runs(range(2, 30)) if base_runs is None else list(base_runs)
    own = [("tuned_triage", {}, r) for r in tool_results] if composed else []
    sessions.append({"id": sid(SHADOW_JOB, shadow_t), "source": "cron", "started_at": shadow_t,
                     "cost": 0.08, "in": 80_000, "out": 800,
                     "calls": own + [(GET, {"message_id": m}, "b") for m in shadow_ids]
                     + list(extra_calls)})
    if live:
        sessions.append({"id": sid(TRIAGE, live_t), "source": "cron", "started_at": live_t,
                         "cost": 0.10, "in": 100_000, "out": 1000,
                         "calls": [(GET, {"message_id": "m1"}, "b"),
                                   (GET, {"message_id": "m2"}, "b")] + old_mod})
    build(env, sessions)
    SETUP_SESSIONS[env.root] = sessions
    base = {"_base": {"prompt": "Triage the inbox.", "enabled_toolsets": ["google"]}}
    if composed:
        fix = Fix("20261018-bbbb0002", "composed_tool", TRIAGE, dict(TOOL) | base, "seq:x")
        write_shadow_tool(env.root / "tools", fix.payload, fix.id)
        if legacy_tool:
            spec, _program = _tool_files(fix.payload)
            (env.root / "tools" / "tuned_triage" / "tool.json").write_text(
                json.dumps(dict(spec, shadow_only=True)))
    else:
        fix = Fix("20261018-bbbb0001", "job_prompt", TRIAGE,
                  {"prompt": "new prompt", "add_toolsets": []} | base, "seq:x")
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


def test_shadow_pass_applies_with_priced_baseline_cost(env):
    # raw billing says $0.50 a run; tokens say $0.10: the baseline is the priced figure
    fix = shadow_setup(env, old_mod=[(MOD, {"message_id": "m2", "add_label_ids": ["TRASH"]}, "ok")],
                       new_mod=[{"message_id": "m2", "add_label_ids": ["TRASH"]}],
                       base_runs=triage_runs(range(2, 30), cost=0.10, raw=0.50))
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "trial" and rec.applied_at == NOW
    assert rec.live_from == NOW  # a job_prompt fix needs no restart: live at once
    assert rec.baseline_cost == pytest.approx(0.10, rel=1e-5)
    assert bridge.jobs[TRIAGE]["prompt"] == "new prompt"
    assert fix.id in report.applied


def test_billing_change_rolls_back_a_fix_that_costs_more_tokens(env):
    """Reviewer scenario (F1): baseline runs billed $0.12 raw (DeepInfra), trial runs
    billed $0 (subscription) with 50% more tokens. Raw dollars would keep it."""
    base = triage_runs(range(2, 30), tokens=100_000, raw=0.12)
    fix = shadow_setup(env, old_mod=[], new_mod=[], base_runs=base)
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"
    later = triage_runs(range(-5, 0), tokens=150_000, raw=0.0)  # the 5 days after NOW
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + later)
    report = run_cycle(env, bridge, NOW + 6 * DAY)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "rolled_back" and fix.id in report.rolled_back
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


def test_shadow_with_no_common_messages_fails(env):
    """F5: the shadow read other messages than the live run decided on: inconclusive."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], shadow_ids=("m7", "m8"))
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason == "no common messages"
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


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
    bridge = FakeBridge()
    assert main(["detect", "--fixture", str(out), "--now", "2026-10-19"], bridge=bridge) == 0
    lines = capsys.readouterr().out.splitlines()
    assert 1 <= len(lines) <= 10
    assert all(json.loads(x)["job_id"] == TRIAGE for x in lines)
    assert bridge.calls == [("pricing", ())]  # no --pricing: the price list comes from Hermes
    # anchored a year later, the 28-day window is empty
    assert main(["detect", "--db", str(env.db), "--now", "2027-10-19"], bridge=bridge) == 0
    assert capsys.readouterr().out == ""


def test_cli_detect_prices_from_a_pricing_file(env, capsys, tmp_path):
    build(env, triage_runs())
    flat, doubled = tmp_path / "flat.json", tmp_path / "doubled.json"
    flat.write_text(json.dumps(FLAT.to_dict()))
    doubled.write_text(json.dumps(FLAT.to_dict() | {"input_per_m": 2 * FLAT_RATE,
                                                    "output_per_m": 2 * FLAT_RATE}))
    bridge = FakeBridge(fail={"pricing"})
    usd = []
    for f in (flat, doubled):
        assert main(["detect", "--db", str(env.db), "--now", "2026-10-19", "--pricing", str(f)],
                    bridge=bridge) == 0
        usd.append(json.loads(capsys.readouterr().out.splitlines()[0])["est_monthly_usd"])
    assert usd[1] == pytest.approx(2 * usd[0]) and usd[0] > 0
    assert bridge.calls == []  # the file is used; Hermes is not asked
    with pytest.raises(SystemExit):
        main(["detect", "--now", "2026-10-19", "--pricing", str(flat)], bridge=bridge)


def test_cli_pricing_prints_the_resolved_list(capsys):
    assert main(["pricing"], bridge=FakeBridge(price=V4_PRO.to_dict())) == 0
    assert json.loads(capsys.readouterr().out) == V4_PRO.to_dict()


# --- restart-aware shadows and trials (spec 4.5, 4.7) ------------------------------------

def state(cfg):
    return json.loads((cfg.root / "cycle_state.json").read_text())


COMPOSED_REPLY = {"kind": "composed_tool", "target": TRIAGE, "payload": dict(TOOL) | {
    "program": (f"def main(args, call):\n"
                f"    return len([call({GET!r}, {{'message_id': m}}) for m in args['ids']])\n"),
    "parameters": {"type": "object", "properties": {"ids": {"type": "array"}}},
    "replay_inputs": [{"param": "ids", "from_call": GET, "arg": "message_id",
                       "collect": "list"}]}}


def _relay_mod():
    from test_relay import relay
    return relay()


def replay_kw():
    """The real relay.run_program with the local stub runner (no Docker)."""
    from test_relay import RUNNER
    return {"run_program": _relay_mod().run_program, "replay_argv": lambda name: RUNNER}


def test_composed_shadow_requests_restart(env):
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(COMPOSED_REPLY)])
    report = run_cycle(env, bridge, NOW, **replay_kw())  # 04:00 London, quiet, no job within 15 min
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "shadow_pending" and rec.needs_restart
    assert (env.root / "tools" / "tuned_triage" / "program.py").exists()
    assert json.loads((env.root / "tools" / "tuned_triage" / "tool.json").read_text())[
        "shadow_only"] is True  # F3: not callable outside the shadow until go-live
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
    ([SCHEDULED_AT], True),                  # same cycle: restart step follows scheduling
    ([SCHEDULED_AT - 1], False),             # just before scheduling
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
                              applied_at=NOW - 10 * DAY, baseline_runs=[brk(0.20)] * 10,
                              needs_restart=True, live_from=live_from), fix)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def history_since(env, monkeypatch):
    """Run a cycle and return the `since` it read the history from."""
    from hermes_tuner.history import History
    seen = []
    real = History.sessions

    def spy(self, since, until=None):
        seen.append(since)
        return real(self, since, until)
    monkeypatch.setattr(History, "sessions", spy)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    return seen[0]


def test_history_window_not_widened_by_old_kept_record(env, monkeypatch):
    """F7: a kept fix carries its priced baseline; it must not drag the read back."""
    build(env, triage_runs())
    for i, state_ in enumerate(("kept", "applied")):
        fix = Fix(f"20250601-aaaa000{i}", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
                  "seq:old")
        add_record(env, FixRecord(fix.id, fix.kind, CAL, "seq:old", state_, NOW - 400 * DAY,
                                  applied_at=NOW - 400 * DAY, baseline_cost=0.1,
                                  live_from=NOW - 400 * DAY, decided_at=NOW - 390 * DAY), fix)
    assert history_since(env, monkeypatch) == NOW - 35 * DAY


def test_history_window_covers_a_trial_baseline(env, monkeypatch):
    build(env, triage_runs())
    fix = Fix("20261001-aaaa0003", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
              "seq:c")
    add_record(env, FixRecord(fix.id, fix.kind, CAL, "seq:c", "trial", NOW - 18 * DAY,
                              applied_at=NOW - 18 * DAY, baseline_cost=0.1,
                              live_from=NOW - 18 * DAY), fix)
    assert history_since(env, monkeypatch) == NOW - 46 * DAY


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
                     "calls": [("tuned_triage", {}, TOOL_OK), (GET, {"message_id": "m1"}, "b"),
                               (GET, {"message_id": "m2"}, "b")]})
    sessions.append({"id": sid(TRIAGE, SHADOW_T + 1200), "source": "cron",
                     "started_at": SHADOW_T + 1200, "cost": 0.10, "in": 100_000, "out": 1000,
                     "calls": [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]})
    env.db.unlink()
    build(env, sessions)
    heartbeat(env, NOW + 1)  # the plugin reloaded at that restart
    run_cycle(env, FakeBridge(jobs=jobs(), config={INCLUDE: ["x_tool"]}), NOW + 3600)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def test_restart_times_pruned_to_28_days(env):
    build(env, triage_runs())
    (env.root / "cycle_state.json").write_text(json.dumps(
        {"restart_times": [NOW - 29 * DAY, NOW - 27 * DAY], "pending_restart": True}))
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert state(env)["restart_times"] == [NOW - 27 * DAY, NOW]


def test_composed_shadow_restarted_in_its_scheduling_cycle_reaches_trial(env):
    """The designed path, end to end: cycle 1 drafts a composed tool, writes the tool
    dir, schedules the shadow and restarts the gateway in the same run (same `now`).
    The shadow runs at run_at with the tool loaded, the live run 20 min later; cycle 2
    compares them and the fix goes into trial."""
    build(env, triage_runs())
    b1 = FakeBridge(jobs=jobs(), oneshot_script=[reply(COMPOSED_REPLY)])
    r1 = run_cycle(env, b1, NOW, **replay_kw())
    assert r1.restarted and state(env)["restart_times"] == [NOW]
    (shadow_id, entry), = json.loads((env.root / "shadow_jobs.json").read_text()).items()
    assert entry["scheduled_at"] == NOW
    run_at = entry["run_at"]
    calls = [(GET, {"message_id": "m1"}, "b"), (GET, {"message_id": "m2"}, "b")]
    sessions = triage_runs()
    sessions.append({"id": sid(shadow_id, run_at), "source": "cron", "started_at": run_at,
                     "cost": 0.08, "in": 80_000, "out": 800,
                     "calls": [("tuned_triage", {}, TOOL_OK)] + calls})
    sessions.append({"id": sid(TRIAGE, run_at + 1200), "source": "cron",
                     "started_at": run_at + 1200, "cost": 0.10, "in": 100_000, "out": 1000,
                     "calls": calls})
    env.db.unlink()
    build(env, sessions)
    heartbeat(env, NOW + 1)  # the restart in cycle 1 reloaded the plugin
    b2 = FakeBridge(jobs=jobs())
    r2 = run_cycle(env, b2, run_at + 7200)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "trial" and rec.kind == "composed_tool", rec.reason
    assert rec.id in r2.applied
    assert (env.root / "tools" / "tuned_triage" / "program.py").exists()
    assert "shadow_only" not in json.loads(
        (env.root / "tools" / "tuned_triage" / "tool.json").read_text())
    assert b2.jobs[TRIAGE]["prompt"] == "new prompt"
    assert "tuner" in b2.jobs[TRIAGE]["enabled_toolsets"]


# --- F4: shadows and composed tools need the plugin loaded -------------------------------

@pytest.mark.parametrize("plugins, beat", [
    (None, NOW - 60),                     # plugins.enabled unset
    (["other-plugin"], NOW - 60),         # enabled list without hermes-tuner
    ("hermes-tuner", NOW - 60),           # not a list
    (["hermes-tuner"], None),             # no heartbeat
    (["hermes-tuner"], NOW - 2 * DAY),    # heartbeat older than the last restart
])
def test_no_shadow_scheduled_without_the_plugin(env, plugins, beat):
    build(env, triage_runs())
    (env.root / "plugin_heartbeat.json").unlink()
    if beat is not None:
        heartbeat(env, beat)
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": [NOW - DAY]}))
    bridge = _FakeBridge(jobs=jobs(), config={PLUGINS: plugins} if plugins else {},
                         oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    assert "plugin not loaded; shadow not scheduled" in report.errors
    assert not [c for c in bridge.calls if c[0] in ("create_job", "oneshot")]
    assert report.shadowed == []


def test_shadow_scheduled_when_heartbeat_follows_the_last_restart(env):
    """Control for the test above: same restart, fresh heartbeat."""
    build(env, triage_runs())
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": [NOW - DAY]}))
    heartbeat(env, NOW - DAY)  # registered at the restart itself: loaded
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    assert len(report.shadowed) == 1 and report.errors == []


# --- the shadow's composed-tool calls ---------------------------------------------------

def restarted_before_shadow(env):
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": [SHADOW_T - 3600]}))


@pytest.mark.parametrize("legacy_tool", [False, True])
def test_composed_shadow_that_never_called_its_tool_fails(env, legacy_tool):
    """Issue 1: the shadow's decisions match the live run's, but it never called the
    fix's tool, so nothing shows the tool works. It must not go live."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True, tool_results=(),
                       legacy_tool=legacy_tool)
    restarted_before_shadow(env)
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason == "shadow never called tuned_triage"
    assert fix.id in report.rejected
    assert not (env.root / "tools" / "tuned_triage").exists()
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


def test_composed_shadow_with_a_failed_tool_call_fails(env):
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True,
                       tool_results=(TOOL_OK, TOOL_ERR, TOOL_OK))
    restarted_before_shadow(env)
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason == "tuned_triage failed 1 of 3 calls"
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


def test_composed_shadow_that_called_its_tool_successfully_goes_live(env):
    """Control for the two tests above."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True,
                       tool_results=(TOOL_OK, TOOL_OK))
    restarted_before_shadow(env)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


OTHER = Fix("20261018-cccc0003", "composed_tool", CAL,
            dict(TOOL, name="tuned_other", job_prompt="Sync with tuned_other."), "seq:y")


@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("other_state, ok", [("shadow_pending", False), ("shadow_failed", False),
                                             ("trial", True), ("kept", True)])
def test_shadow_that_used_another_fixs_tool_fails_unless_that_tool_is_live(
        env, composed, other_state, ok):
    """Issue 2: the 2026-10-23 briefing shadow called the calendar fix's shadow-only
    tool 7 times. Decisions resting on a tool that is not live (and not this fix's own)
    say nothing about this fix. A tool that is live is part of the job either way."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=composed,
                       extra_calls=[("tuned_other", {}, TOOL_OK)] * 7)
    if composed:
        restarted_before_shadow(env)
    led = add_record(env, FixRecord(OTHER.id, OTHER.kind, CAL, "seq:y", other_state,
                                    NOW - DAY, applied_at=NOW - DAY, live_from=NOW - DAY,
                                    baseline_runs=[]), OTHER)
    del led
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    if ok:
        assert rec.state == "trial", rec.reason
        return
    assert rec.state == "shadow_failed"
    assert rec.reason == "shadow used tuned_other, which is not live and not this fix's tool"
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


def test_shadow_with_a_refused_call_to_another_fixs_tool_is_judged_on_decisions(env):
    """Under the plugin's refusal the other fix's tool returns an error: the shadow
    wasted a call, but its decisions do not rest on that tool."""
    refused = json.dumps({"error": "tuned_other is in another fix's shadow trial and runs "
                                   "only in that fix's shadow session"})
    fix = shadow_setup(env, old_mod=[], new_mod=[], extra_calls=[("tuned_other", {}, refused)])
    add_record(env, FixRecord(OTHER.id, OTHER.kind, CAL, "seq:y", "shadow_pending", NOW - DAY),
               OTHER)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def test_composed_go_live_waits_for_the_plugin(env):
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True)
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": [SHADOW_T - 3600]}))
    heartbeat(env, SHADOW_T - 7200)  # from before that restart: plugin not reloaded
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_pending" and rec.applied_at is None
    assert any(e.startswith("plugin not loaded") for e in report.errors)
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."


# --- F8: an owner edit after drafting stops go-live ---------------------------------------

@pytest.mark.parametrize("field, value", [("prompt", "Owner's new triage prompt."),
                                          ("enabled_toolsets", ["google", "web"])])
@pytest.mark.parametrize("composed", [False, True])
def test_owner_edit_after_drafting_closes_owner_changed(env, field, value, composed):
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=composed)
    if composed:
        (env.root / "cycle_state.json").write_text(
            json.dumps({"restart_times": [SHADOW_T - 3600]}))
    js = jobs()
    js[TRIAGE][field] = value
    bridge = FakeBridge(jobs=js)
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "owner_changed" and rec.applied_at is None
    assert bridge.jobs[TRIAGE][field] == value
    assert not [c for c in bridge.calls if c[0] == "update_job"]
    assert not (env.root / "tools" / "tuned_triage").exists()


# --- m1: an unexpected exception in one step does not stop the cycle -----------------------

def test_unexpected_error_in_a_step_is_logged_and_the_cycle_continues(env, monkeypatch):
    from hermes_tuner import trial as trial_mod
    fix = shadow_setup(env, old_mod=[], new_mod=[])

    def boom(*a, **k):
        raise RuntimeError("compare exploded")
    monkeypatch.setattr(trial_mod, "compare_triage", boom)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    report = run_cycle(env, bridge, NOW)
    assert any("compare exploded" in e for e in report.errors)
    assert Ledger.load(env.root).get(fix.id).state == "shadow_pending"
    line = (env.root / "outbox" / "weekly-2026-W43.txt").read_text()
    assert "; errors: 1 (" in line
    assert any("compare exploded" in e["error"] for e in state(env)["errors"])


def test_unexpected_error_after_drafting_still_records_the_spend(env, monkeypatch):
    from hermes_tuner import cycle_draft
    build(env, triage_runs())

    def boom(*a, **k):
        raise RuntimeError("limits exploded")
    monkeypatch.setattr(cycle_draft, "check_fix", boom)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY, tokens=(10_000, 2000))])
    report = run_cycle(env, bridge, NOW)
    assert any("limits exploded" in e for e in report.errors)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "rejected"
    assert rec.spend_usd == pytest.approx(12_000 * FLAT_RATE / 1e6)
    assert report.spend_usd == pytest.approx(rec.spend_usd)
    assert (env.root / "outbox" / "weekly-2026-W43.txt").exists()


# --- m3: a trial with no verdict after 21 days is rolled back ------------------------------

@pytest.mark.parametrize("age, rolled", [(21 * DAY, True), (21 * DAY - 60, False)])
def test_trial_waiting_for_a_restart_is_rolled_back_after_21_days(env, age, rolled):
    build(env, triage_runs())
    fix = config_trial(env)
    led = Ledger.load(env.root)
    rec = led.get(fix.id)
    rec.applied_at, rec.live_from = NOW - age, None  # the restart never happened
    led.save()
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7})
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    if rolled:
        assert rec.state == "rolled_back" and rec.reason == "no verdict in 21 days"
        assert bridge.config[CFG_KEY] == 0.5
    else:
        assert rec.state == "trial" and bridge.config[CFG_KEY] == 0.7


@pytest.mark.parametrize("age, rolled", [(21 * DAY, True), (20 * DAY, False)])
def test_job_trial_short_of_runs_is_rolled_back_after_21_days(env, age, rolled):
    # only 2 runs since the fix went live: fewer than JOB_TRIAL_RUNS, so no verdict
    build(env, triage_runs(range(22, 40)) + triage_runs([1, 2]))
    fix = Fix("20260928-aaaa0009", "job_prompt", TRIAGE, {"prompt": "p", "add_toolsets": []},
              "seq:x")
    led = add_record(env, FixRecord(fix.id, fix.kind, TRIAGE, "seq:x", "trial", NOW - age,
                                    applied_at=NOW - age, baseline_runs=[brk(0.10)] * 10,
                                    live_from=NOW - age), fix)
    vdir = led.version_dir(fix.id)
    (vdir / "before.json").write_text(json.dumps(
        {"prompt": "Triage the inbox.", "enabled_toolsets": ["google"]}))
    (vdir / "after.json").write_text(json.dumps({"prompt": "p", "enabled_toolsets": ["google"]}))
    js = jobs()
    js[TRIAGE]["prompt"] = "p"
    bridge = FakeBridge(jobs=js)
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    if rolled:
        assert rec.state == "rolled_back" and rec.reason == "no verdict in 21 days"
        assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."
    else:
        assert rec.state == "trial" and bridge.jobs[TRIAGE]["prompt"] == "p"


# --- model pricing (amendment 2026-10-19-hermes-tuner-pricing) -------------------------------

def brk(usd, out=1000):
    """A stored baseline run [input, cache read, cache write, output] priced `usd` at FLAT."""
    return [tok(usd) - out, 0, 0, out]


@pytest.mark.parametrize("priced", [False, True])
def test_no_pricing_skips_drafting_and_verdicts_but_runs_safety_steps(env, priced):
    build(env, triage_runs(range(1, 30)) + cal_runs(range(1, 10), 0.05))
    # a calendar trial live 10 days: 9 runs at $0.05 against a $0.50 baseline ("keep")
    cal = Fix("20261009-cccc0002", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
              "seq:cal")
    add_record(env, FixRecord(cal.id, cal.kind, CAL, cal.candidate_key, "trial", NOW - 10 * DAY,
                              applied_at=NOW - 10 * DAY, live_from=NOW - 10 * DAY,
                              baseline_runs=[brk(0.50, 500)] * 10), cal)
    cfg_fix = config_trial(env, applied_days_ago=22)  # past the 21-day deadline
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7},
                        oneshot_script=[reply(JOB_PROMPT_REPLY)],
                        fail=set() if priced else {"pricing"})
    report = run_cycle(env, bridge, NOW)
    led = Ledger.load(env.root)
    line = (env.root / "outbox" / "weekly-2026-W43.txt").read_text()
    assert led.get(cfg_fix.id).state == "rolled_back" and bridge.config[CFG_KEY] == 0.5
    if priced:  # the control: the same run with a price list drafts and judges
        assert len(oneshots(bridge)) == 1
        assert led.get(cal.id).state == "kept"
        assert led.get(cfg_fix.id).reason == "trial: not cheaper than baseline"
        assert "pricing unavailable" not in line and "no model pricing" not in report.errors
    else:
        assert oneshots(bridge) == []
        assert led.get(cal.id).state == "trial"
        assert led.get(cfg_fix.id).reason == "no verdict in 21 days"
        assert "pricing unavailable" in line and "$" not in line
        assert "no model pricing" in report.errors


@pytest.mark.parametrize("extra, used", [(0, True), (1, False)])
def test_cached_pricing_used_within_max_age_not_after(env, extra, used):
    build(env, triage_runs())
    fetched = NOW - policy.PRICING_MAX_AGE_S - extra
    env.root.mkdir(parents=True, exist_ok=True)
    cached = FLAT.to_dict() | {"fetched_at": fetched}
    (env.root / "cycle_state.json").write_text(json.dumps({"pricing": cached}))
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)], fail={"pricing"})
    report = run_cycle(env, bridge, NOW)
    day = datetime.fromtimestamp(fetched, timezone.utc).strftime("%Y-%m-%d")
    if used:
        assert len(oneshots(bridge)) == 1
        assert any(e.startswith(f"pricing lookup failed; using cached list from {day}: ")
                   and len(e) > len(f"pricing lookup failed; using cached list from {day}: ")
                   for e in report.errors)
        assert "no model pricing" not in report.errors
    else:
        assert oneshots(bridge) == [] and "no model pricing" in report.errors
    assert state(env)["pricing"]["fetched_at"] == fetched  # a failed lookup stores nothing


def test_pricing_lookup_is_stored_with_its_fetch_time(env):
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), price=V4_PRO.to_dict() | {"fetched_at": 5.0})
    report = run_cycle(env, bridge, NOW)
    assert state(env)["pricing"] == V4_PRO.to_dict() | {"fetched_at": NOW}
    assert report.pricing == V4_PRO.to_dict() | {"fetched_at": NOW}
    assert bridge.calls.count(("pricing", ())) == 1


def test_dry_run_reports_pricing_and_its_failure(env):
    build(env, triage_runs())
    report = run_cycle(CycleConfig(**{**env.__dict__, "dry_run": True}),
                       FakeBridge(jobs=jobs(), price=V4_PRO.to_dict()), NOW)
    assert report.pricing["model"] == "deepseek-v4-pro" and not (env.root / "cycle_state.json").exists()
    bad = run_cycle(CycleConfig(**{**env.__dict__, "dry_run": True}),
                    FakeBridge(jobs=jobs(), fail={"pricing"}), NOW)
    assert bad.pricing is None and "no model pricing" in bad.errors


def test_pricing_change_rebases_baseline(env):
    """Live under list A (baseline_runs stored); trial runs have exactly the baseline's
    tokens; then list B halves every rate. B on both sides: equal cost -> rollback. A
    baseline priced under A against runs under B would look 50% cheaper -> keep."""
    list_a = FLAT.to_dict()
    list_b = FLAT.to_dict() | {"input_per_m": FLAT_RATE / 2, "output_per_m": FLAT_RATE / 2,
                               "pricing_version": "halved"}
    fix = shadow_setup(env, old_mod=[], new_mod=[],
                       base_runs=triage_runs(range(2, 30), cost=0.10))
    bridge = FakeBridge(jobs=jobs(), price=list_a)
    run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    # days 2..28 lie inside [applied_at - 28 d, applied_at), plus yesterday's live run
    assert rec.state == "trial" and len(rec.baseline_runs) == 28
    assert brk(0.10) in rec.baseline_runs
    later = triage_runs(range(-5, 0), cost=0.10)  # the 5 days after NOW, same tokens
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + later)
    bridge.price = list_b
    report = run_cycle(env, bridge, NOW + 6 * DAY)
    assert Ledger.load(env.root).get(fix.id).state == "rolled_back"
    assert fix.id in report.rolled_back


def test_savings_use_stored_baseline_runs_without_history(tmp_path):
    """A kept fix applied 400 days ago: its baseline window is far outside the loaded
    history, so the saving can only come from the stored baseline_runs."""
    led = Ledger(tmp_path)
    applied = NOW - 400 * DAY
    led.records.append(FixRecord("20250915-abcd0001", "job_prompt", TRIAGE, "seq:x", "kept",
                                 applied, applied_at=applied, live_from=applied,
                                 decided_at=applied + 10 * DAY,
                                 baseline_runs=[brk(0.12, 0)] * 5))
    sessions = [run_session(TRIAGE, NOW - d * DAY, 0.05) for d in (1, 2, 3)]
    assert weekly_line(led, sessions, NOW, FLAT).startswith("Hermes tuner: saved ~$0.21 ")


def test_weekly_line_names_price_source(tmp_path):
    led = Ledger(tmp_path)
    assert weekly_line(led, [], NOW, V4_PRO) == (
        "Hermes tuner: saved ~$0.00 this week (ESTIMATE at deepseek-v4-pro list prices, "
        "deepseek-pricing-2026-07), 0 applied, 0 kept, 0 rolled back; tuner spent $0.00")
    assert weekly_line(led, [], NOW, None) == (
        "Hermes tuner: savings unknown this week (pricing unavailable), 0 applied, "
        "0 kept, 0 rolled back; tuner spend unknown")


def test_global_fix_stores_baseline_runs_at_go_live(env):
    """A drafted config fix goes live with the chat and editable-job runs of the 28 days
    before it as token counts (what its verdict is priced from); other jobs' runs and
    runs without tool calls are not in it."""
    sessions = []
    for d in range(1, 22):  # 21 chat sessions with two tool_describe calls each, a
        # schema_lookup candidate above MIN_DRAFT_MONTHLY_USD
        sessions.append({"id": f"2026{d:04d}_000000_ab12cd", "source": "telegram",
                         "started_at": NOW - d * DAY, "cost": 0.05, "in": 10_000 + d,
                         "cache": 7, "cw": 3, "out": 10,
                         "calls": [("tool_describe", {"name": "a"}, "s" * 300),
                                   ("tool_describe", {"name": "b"}, "s" * 300)]})
    sessions.append({"id": sid("0123456789ab", NOW - DAY), "source": "cron",
                     "started_at": NOW - DAY, "in": 5, "out": 5,
                     "calls": [("web_search", {}, "r")]})                 # not an editable job
    sessions.append({"id": "20261018_000000_cd34ef", "source": "telegram",
                     "started_at": NOW - DAY, "in": 9, "out": 9})          # no tool calls
    sessions += cal_runs([2], 0.05)
    build(env, sessions)
    cfg_reply = {"kind": "config", "target": CFG_KEY, "payload": {"key": CFG_KEY, "value": 0.7}}
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.5}, oneshot_script=[reply(cfg_reply)])
    run_cycle(env, bridge, NOW)
    (rec,) = [r for r in Ledger.load(env.root).records if r.kind == "config"]
    assert rec.state == "trial"
    chats = sorted([10_000 + d, 7, 3, 10] for d in range(1, 22))
    assert sorted(rec.baseline_runs) == sorted(chats + [[tok(0.05) - 500, 0, 0, 500]])


def test_global_record_without_baseline_runs_waits_for_the_deadline(env):
    """A config trial saved before baseline_runs existed is due for a verdict (8 days
    live) but has nothing to be priced against: it continues; the deadline decides."""
    build(env, triage_runs(range(1, 30)))
    fix = config_trial(env)
    led = Ledger.load(env.root)
    led.get(fix.id).baseline_runs = None
    led.save()
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7})
    run_cycle(env, bridge, NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial" and bridge.config[CFG_KEY] == 0.7


# --- review fixes: restart clock, rollback_pending retry --------------------------------

def test_restart_checks_the_window_when_it_restarts_not_at_cycle_start(env):
    """Catches: checking restart_allowed against the cycle's start time after slow
    drafting, so the restart lands inside a job's margin."""
    build(env, triage_runs())
    approve_proposal(env)
    run_cycle(env, FakeBridge(jobs=jobs(), config={INCLUDE: []}), T_OUT)
    due = jobs()
    due[TRIAGE]["next_run_at"] = iso(T_IN + 20 * 60)  # 20 min after the cycle starts
    late = FakeBridge(jobs=due, config={INCLUDE: ["x_tool"]})
    run_cycle(env, late, T_IN, clock=lambda: T_IN + 10 * 60)  # 10 min in: job due in 10
    assert ("restart_gateway", ()) not in late.calls
    assert state(env)["pending_restart"] is True
    ok = FakeBridge(jobs=due, config={INCLUDE: ["x_tool"]})  # control: no delay
    run_cycle(env, ok, T_IN, clock=lambda: T_IN + 60)
    assert ok.calls.count(("restart_gateway", ())) == 1
    assert Ledger.load(env.root).get("p1").live_from == T_IN + 60  # the restart's time


def test_rollback_pending_is_retried_by_the_cycle(env):
    """Catches: a change left live by a failed apply that nothing ever reverts."""
    build(env, triage_runs())
    fix = Fix("20261018-eeee0001", "config", CFG_KEY, {"key": CFG_KEY, "value": 0.7},
              "schema_lookup")
    led = add_record(env, FixRecord(fix.id, fix.kind, CFG_KEY, fix.candidate_key,
                                    "rollback_pending", NOW - DAY), fix)
    vdir = led.version_dir(fix.id)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "before.json").write_text(json.dumps({"key": CFG_KEY, "value": 0.5}))
    (vdir / "after.json").write_text(json.dumps({"key": CFG_KEY, "value": 0.7}))
    bridge = FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7})
    report = run_cycle(env, bridge, NOW)
    assert bridge.config[CFG_KEY] == 0.5
    assert Ledger.load(env.root).get(fix.id).state == "rolled_back"
    assert fix.id in report.rolled_back


def test_failed_drafting_call_is_charged_and_counts_toward_the_pause(env):
    """Catches: a oneshot that times out (possibly billed) costing $0 to the budget.
    $0.99 spent last week leaves the 28-day spend just under the $1 allowance; the
    failed call's charge takes it over, so the next day does not draft."""
    build(env, triage_runs())
    old = Fix("20261009-ffff0002", "job_prompt", CAL, {}, "seq:old")
    add_record(env, FixRecord(old.id, "job_prompt", CAL, "seq:old", "rejected",
                              NOW - 10 * DAY, spend_usd=0.99), old)
    bridge = FakeBridge(jobs=jobs(), fail={"oneshot"})
    report = run_cycle(env, bridge, NOW)
    assert len(oneshots(bridge)) == 1
    (rec,) = [r for r in Ledger.load(env.root).records if r.id != old.id]
    assert rec.state == "rejected" and rec.spend_usd == policy.DRAFT_EST_USD
    assert "oneshot" in rec.reason
    assert any("oneshot" in e for e in report.errors)
    bridge2 = FakeBridge(jobs=jobs(), fail={"oneshot"})
    run_cycle(env, bridge2, NOW + DAY)
    assert oneshots(bridge2) == []  # the charged spend counts against the budget


@pytest.mark.parametrize("composed", [False, True])
def test_shadow_with_an_unreadable_fix_fails_without_stopping_the_step(env, composed):
    """The tool check loads fix.json. An unreadable one fails that shadow; it must not
    raise out of evaluate_shadows and leave every later shadow unevaluated."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=composed,
                       tool_results=(TOOL_OK,))
    restarted_before_shadow(env)
    (env.root / "versions" / fix.id / "fix.json").write_text("{not json")
    report = run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason.startswith("fix unreadable")
    assert not [e for e in report.errors if "evaluate_shadows" in e]


# --- reducers: calendar sync bytes candidates go to the replay search (Task 8) -------------

GET_EVENTS = "mcp__google__get_events"
# Synthetic text only: no real mail enters the repository.
PICNIC = "Synthetic newsletter paragraph about the team picnic and its sandwiches. "
CAL_BASE = {"prompt": "Sync the calendar.", "enabled_toolsets": ["google"]}
RFIX = "20261018-r0000aaaa"


def big_result(n):
    """A result shaped like the live MCP ones: the text twice (~43k chars)."""
    text = PICNIC * 300 + f"item {n}"
    return json.dumps({"result": text, "structuredContent": {"result": text}})


def plain_result(n):
    """Plain text (~22k chars) whose only reused string is its last line."""
    return PICNIC * 300 + f"\nClosing reference QZ-{n:04d}-tail-of-message"


def bytes_runs(job, tool=GET, days=range(1, 13), result=big_result, said=lambda d: []):
    """Runs of `job` at 06:00 UTC that each read one big result, then take 3 more
    turns (`said(d)` their texts): one bytes candidate for (job, tool), well above
    MIN_DRAFT_MONTHLY_USD."""
    base = NOW - 3 * 3600 + 6 * 3600
    return [{"id": sid(job, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
             "in": tok(0.10) - 500, "out": 500, "final_turns": 3, "final_content": said(d),
             "calls": [(tool, {"message_id": f"m{d}"}, result(d), 1)]} for d in days]


def reducer_files(cfg):
    d = cfg.root / "reducers"
    return {p.name: json.loads(p.read_text()) for p in d.glob("*.json")} if d.is_dir() else {}


def test_cycle_shadows_reducer_for_calendar_sync(env):
    build(env, bytes_runs(CAL))
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    led = Ledger.load(env.root)
    (rec,) = led.records
    assert rec.kind == "reducer" and rec.state == "shadow_pending", rec.reason
    assert rec.target == CAL and rec.candidate_key == "bytes:" + GET
    assert rec.id.startswith("20261019-r") and rec.spend_usd == 0.0
    assert rec.est_saving_per_run is not None and rec.est_saving_per_run > 0
    files = reducer_files(env)
    assert list(files) == [f"{rec.id}.json"]
    assert files[f"{rec.id}.json"]["shadow_only"] is True
    assert files[f"{rec.id}.json"]["spec"]["tool"] == GET
    assert "_base" not in files[f"{rec.id}.json"]["spec"]
    assert oneshots(bridge) == []
    assert report.shadowed == [rec.id] and report.errors == []


def test_shadow_schedules_reducer_with_live_prompt(env):
    """Catches: _shadow_prompt or job_after_for still raising for a reducer (the record
    goes to rejected, "shadow not scheduled")."""
    build(env, bytes_runs(CAL))
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "shadow_pending", rec.reason
    (create,) = [c for c in bridge.calls if c[0] == "create_job"]
    prompt, _schedule, name, _deliver, toolsets, _extra = create[1]
    assert prompt == "Sync the calendar." and toolsets == ["google"]
    assert name == policy.SHADOW_PREFIX + rec.id
    assert [c for c in bridge.calls if c[0] == "update_job"] == []
    assert bridge.jobs[CAL]["prompt"] == "Sync the calendar."


def test_reducer_search_failure_records_a_placeholder(env):
    """Every result's last line is reused later, so no cap keeps it and nothing else
    cuts 15%: no reducer, a rejected placeholder carrying the reason code, no spend."""
    build(env, bytes_runs(CAL, result=plain_result,
                          said=lambda d: [f"Filed under QZ-{d:04d}-tail-of-message."]))
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    led = Ledger.load(env.root)
    (rec,) = led.records
    assert rec.state == "rejected" and rec.kind == "reducer"
    assert rec.reason == "reducer search: cut_below_min" and rec.spend_usd == 0.0
    assert reducer_files(env) == {} and oneshots(bridge) == []


@pytest.mark.parametrize("job, tool", [(TRIAGE, GET), (CAL, "mcp__google__list_calendars")])
def test_morning_briefing_bytes_goes_to_model(env, monkeypatch, job, tool):
    """A bytes candidate outside REDUCER_JOBS, or for a tool outside REDUCER_TOOLS, is
    drafted by the model as before and never searched."""
    from hermes_tuner import cycle_draft
    searched = []
    monkeypatch.setattr(cycle_draft, "search_reducer",
                        lambda *a, **k: searched.append(a) or None)
    build(env, bytes_runs(job, tool))
    reply_obj = dict(JOB_PROMPT_REPLY, target=job)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(reply_obj)])
    run_cycle(env, bridge, NOW)
    (call,) = oneshots(bridge)
    assert "bytes:" in json.dumps(call)
    assert searched == []
    assert reducer_files(env) == {}


def test_select_skips_cheap_candidates(env):
    from hermes_tuner.cycle import CycleReport, _Cycle
    from hermes_tuner.detect import Candidate

    def cand(usd, key):
        return Candidate(kind="bytes", key=key, job_id=CAL, sessions=10, occurrences=10,
                         result_chars=1000, est_monthly_usd=usd)
    c = _Cycle(env, FakeBridge(jobs=jobs()), NOW, CycleReport())
    c.ledger = Ledger.load(env.root)
    assert c._select([cand(0.04, "bytes:a"), cand(0.06, "bytes:b")]) == [cand(0.06, "bytes:b")]
    assert c._select([cand(0.05, "bytes:c")]) == [cand(0.05, "bytes:c")]  # at the floor: kept
    assert c._select([cand(0.04, "bytes:a")]) == []


@pytest.mark.parametrize("kept_tool, blocked", [(GET, True), (SEARCH, False)])
def test_no_second_reducer_for_same_tool(env, kept_tool, blocked):
    kept = Fix("20261001-r0000bbbb", "reducer", CAL,
               {"tool": kept_tool, "job": CAL, "steps": [{"op": "collapse_ws"}],
                "_base": CAL_BASE}, "bytes:" + kept_tool)
    add_record(env, FixRecord(kept.id, "reducer", CAL, kept.candidate_key, "kept",
                              NOW - 18 * DAY, applied_at=NOW - 18 * DAY,
                              live_from=NOW - 18 * DAY, baseline_runs=[]), kept)
    build(env, bytes_runs(CAL))
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    led = Ledger.load(env.root)
    assert oneshots(bridge) == [] and report.errors == []
    get_jobs = [c for c in bridge.calls if c[0] == "get_job"]
    if blocked:
        assert [r.id for r in led.records] == [kept.id]
        assert reducer_files(env) == {}
        assert get_jobs == []  # the local ledger check comes before any bridge call
        return
    (new,) = [r for r in led.records if r.id != kept.id]  # control: another tool is free
    assert new.kind == "reducer" and new.state == "shadow_pending"
    assert get_jobs


def test_kept_reducer_does_not_block_the_jobs_next_candidate(env):
    """A kept reducer for (calendar sync, search) leaves search's bytes candidate on top
    of the job's ranking. It is dropped before the pick, so the job's next candidate
    (get's bytes) is searched this cycle. Catches: _select picking the taken candidate,
    which _reduce_one then ends with no record, every cycle."""
    kept = Fix("20261001-r0000cccc", "reducer", CAL,
               {"tool": SEARCH, "job": CAL, "steps": [{"op": "collapse_ws"}],
                "_base": CAL_BASE}, "bytes:" + SEARCH)
    add_record(env, FixRecord(kept.id, "reducer", CAL, kept.candidate_key, "kept",
                              NOW - 18 * DAY, applied_at=NOW - 18 * DAY,
                              live_from=NOW - 18 * DAY, baseline_runs=[]), kept)
    base = NOW - 3 * 3600 + 6 * 3600
    build(env, [{"id": sid(CAL, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
                 "in": tok(0.10) - 500, "out": 500, "final_turns": 3, "final_content": [],
                 "calls": [(SEARCH, {"query": f"q{d}"}, big_result(d) * 2, 1),
                           (GET, {"message_id": f"m{d}"}, big_result(d), 2)]}
                for d in range(1, 13)])
    report = run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    led = Ledger.load(env.root)
    new = [r for r in led.records if r.id != kept.id]
    assert [r.candidate_key for r in new] == ["bytes:" + GET], [r.reason for r in new]
    assert new[0].kind == "reducer" and report.errors == []


def test_detect_counts_composed_tool_results_under_the_tool_it_calls(env):
    """aliases from tools/*/tool.json: a composed tool calling one tool is counted as
    that tool, so its bytes reach the reducer route (and the search finds no direct
    calls to replay). Unreadable tool.json files are skipped."""
    tools = env.root / "tools"
    (tools / "tuned_get").mkdir(parents=True)
    (tools / "tuned_get" / "tool.json").write_text(json.dumps({"name": "tuned_get",
                                                               "calls": [GET]}))
    (tools / "broken").mkdir()
    (tools / "broken" / "tool.json").write_text("{not json")
    build(env, bytes_runs(CAL, tool="tuned_get"))
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.candidate_key == "bytes:" + GET and rec.kind == "reducer"
    assert rec.reason == "reducer search: no_runs"
    assert oneshots(bridge) == []


def reducer_setup(env, events=None, live=True):
    """A shadow-pending calendar reducer whose shadow ran at 06:40 yesterday, with its
    shadow-only reducer file, and (if `live`) a live run at 07:00. `events`: lines for
    reducer_events/<fix id>.jsonl (default: one reduction in the shadow session)."""
    shadow_sid = sid(SHADOW_JOB, SHADOW_T)
    sessions = cal_runs(range(2, 30), 0.10)
    sessions.append({"id": shadow_sid, "source": "cron", "started_at": SHADOW_T,
                     "in": 80_000, "out": 800, "calls": [(GET, {"message_id": "m1"}, "b")]})
    if live:
        live_t = SHADOW_T + 20 * 60
        sessions.append({"id": sid(CAL, live_t), "source": "cron", "started_at": live_t,
                         "in": 100_000, "out": 1000,
                         "calls": [(GET, {"message_id": "m1"}, "b")]})
    build(env, sessions)
    payload = {"tool": GET, "job": CAL, "steps": [{"op": "collapse_ws"}], "_base": CAL_BASE}
    fix = Fix(RFIX, "reducer", CAL, payload, "bytes:" + GET)
    from hermes_tuner.ledger import write_shadow_reducer
    write_shadow_reducer(env.root, payload, fix.id)
    add_record(env, FixRecord(fix.id, "reducer", CAL, fix.candidate_key, "shadow_pending",
                              NOW - DAY - 3600), fix)
    (env.root / "shadow_jobs.json").write_text(json.dumps({SHADOW_JOB: {
        "fix_id": fix.id, "live_job_id": CAL, "run_at": SHADOW_T,
        "created_at": NOW - DAY - 3600, "scheduled_at": SCHEDULED_AT}}))
    if events is None:
        events = [{"session_id": shadow_sid, "tool_call_id": "c0", "before": 900, "after": 400}]
    if events:
        d = env.root / "reducer_events"
        d.mkdir()
        (d / f"{fix.id}.jsonl").write_text("\n".join(
            e if isinstance(e, str) else json.dumps(e) for e in events) + "\n")
    return fix


def test_reducer_goes_live_after_good_shadow(env):
    fix = reducer_setup(env)
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "trial" and rec.applied_at == NOW, rec.reason
    assert rec.baseline_runs  # stored at go-live, as for other job fixes
    assert reducer_files(env)[f"{fix.id}.json"]["shadow_only"] is False
    assert [c for c in bridge.calls if c[0] == "update_job"] == []
    assert fix.id in report.applied


LIVE_SID = sid(CAL, SHADOW_T + 20 * 60)


@pytest.mark.parametrize("events", [
    [],                                                                    # no event file
    [{"session_id": LIVE_SID, "tool_call_id": "c0", "before": 9, "after": 4}],  # other session
    [{"session_id": sid(SHADOW_JOB, SHADOW_T), "tool_call_id": "c0", "error": "TypeError"}],
    ["{broken", "[1]"],
])
def test_reducer_shadow_without_events_fails(env, events):
    fix = reducer_setup(env, events=events)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_failed" and rec.reason == "shadow never reduced a result"
    assert reducer_files(env) == {}


def test_reducer_event_in_a_compressed_child_of_the_shadow_counts(env):
    """The hook logs the agent's current session id: after compression, a child id."""
    child = "20261018_064500_abcd12"
    fix = reducer_setup(env, events=[{"session_id": child, "tool_call_id": "c0",
                                      "before": 900, "after": 400}])
    import sqlite3
    conn = sqlite3.connect(env.db)
    conn.execute("update sessions set end_reason = 'compression' where id = ?",
                 (sid(SHADOW_JOB, SHADOW_T),))
    conn.execute("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?)",
                 (child, "cron", SHADOW_T + 300, 0, 0, 0, 0, 0.0, "m",
                  sid(SHADOW_JOB, SHADOW_T), None))
    conn.commit()
    conn.close()
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"


def test_reducer_go_live_needs_plugin(env):
    fix = reducer_setup(env)
    (env.root / "cycle_state.json").write_text(json.dumps({"restart_times": [SHADOW_T - 3600]}))
    heartbeat(env, SHADOW_T - 7200)  # from before that restart: plugin not reloaded
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "shadow_pending" and rec.applied_at is None
    assert f"plugin not loaded; {fix.id} not applied" in report.errors
    assert reducer_files(env)[f"{fix.id}.json"]["shadow_only"] is True


def test_reducer_shadow_not_scheduled_removes_its_file(env):
    """The live job has no next run: the shadow cannot be scheduled, the record is
    rejected and the shadow-only reducer file written for it is removed."""
    build(env, bytes_runs(CAL))
    js = jobs()
    js[CAL]["next_run_at"] = None
    bridge = FakeBridge(jobs=js)
    run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.kind == "reducer" and rec.state == "rejected"
    assert rec.reason.startswith("shadow not scheduled")
    assert reducer_files(env) == {}


def test_reducer_search_error_reports_only_its_type(env, monkeypatch):
    """An unexpected error inside the search is reported by type: its message could
    hold result text, which never leaves the tuner process."""
    from hermes_tuner import cycle_draft

    def boom(*a, **k):
        raise KeyError(RESULT_SENTINEL)
    monkeypatch.setattr(cycle_draft, "search_reducer", boom)
    build(env, bytes_runs(CAL))
    report = run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert report.errors == ["reducer search bytes:" + GET + ": unexpected KeyError"]
    assert Ledger.load(env.root).records == []
    assert RESULT_SENTINEL not in (env.root / "cycle_state.json").read_text()


def small_result(n):
    """A result like big_result but ~2k chars: halving it saves little."""
    text = PICNIC * 14 + f"item {n}"
    return json.dumps({"result": text, "structuredContent": {"result": text}})


def aliased_runs(direct_result, direct=3):
    """12 runs of calendar sync reading big results through tuned_get (aliased to GET,
    so the bytes candidate is large; they never call GET directly) and `direct` runs
    calling GET directly, the only calls the replay search sees."""
    return (bytes_runs(CAL, tool="tuned_get", days=range(1, 13))
            + bytes_runs(CAL, days=range(13, 13 + direct), result=direct_result))




def write_alias(cfg):
    d = cfg.root / "tools" / "tuned_get"
    d.mkdir(parents=True)
    (d / "tool.json").write_text(json.dumps({"name": "tuned_get", "calls": [GET]}))


@pytest.mark.parametrize("direct_result, direct, floor_ok", [
    (small_result, 3, False),
    # $0.0132 a run cut on 3 runs: $0.042 a month. Multiplying by all 15 of the
    # job's runs (12 never call GET directly) would claim $0.21 and pass the floor.
    (big_result, 3, False),
    (big_result, 6, True),   # $0.085 a month
])
def test_reducer_saving_below_the_floor_is_not_shadowed(env, direct_result, direct, floor_ok):
    """The candidate cleared the floor on all of GET's bytes (most through tuned_get), but
    the reducer only cuts the direct calls: its own monthly saving, the per-run saving
    times the runs that call the tool, must clear it too."""
    write_alias(env)
    build(env, aliased_runs(direct_result, direct))
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    creates = [c for c in bridge.calls if c[0] == "create_job"]
    if floor_ok:
        assert rec.state == "shadow_pending", rec.reason
        assert creates and list(reducer_files(env)) == [f"{rec.id}.json"]
        return
    assert rec.kind == "reducer" and rec.state == "rejected"
    assert rec.reason == "reducer search: saving_below_floor" and rec.spend_usd == 0.0
    assert creates == [] and reducer_files(env) == {}


def test_reducer_saving_at_the_floor_is_shadowed(env, monkeypatch):
    """A monthly saving equal to MIN_DRAFT_MONTHLY_USD is not below it. The search
    saw the tool in 5 runs: the saving is per_run x 5 x 30/28."""
    from hermes_tuner import cycle_draft
    from hermes_tuner.replay import ReducerResult
    spec = {"tool": GET, "job": CAL, "steps": [{"op": "collapse_ws"}]}
    per_run = 0.0011
    monkeypatch.setattr(cycle_draft, "search_reducer", lambda *a, **k: ReducerResult(
        spec, "ok", 5, 5, 100, 50, 0, 0, per_run))
    monkeypatch.setattr(policy, "MIN_DRAFT_MONTHLY_USD", per_run * 5 * 30 / 28)
    build(env, bytes_runs(CAL))  # 12 runs in the window
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "shadow_pending", rec.reason
    assert [c for c in bridge.calls if c[0] == "create_job"]


def test_reducer_projection_has_no_drafting_estimate(env):
    """A reducer makes no model call: the weekly cap counts only its shadow run. Week
    spend $0.89 + shadow $0.10 fits the $1.00 cap; + DRAFT_EST_USD $0.02 would not."""
    spent = Fix("20261019-eeee0002", "job_prompt", CAL, {}, "seq:old")
    add_record(env, FixRecord(spent.id, "job_prompt", CAL, "seq:old", "rejected",
                              NOW - 3600, spend_usd=0.89), spent)
    build(env, bytes_runs(CAL))
    bridge = FakeBridge(jobs=jobs())
    report = run_cycle(env, bridge, NOW)
    assert report.budget == ""
    recs = [r for r in Ledger.load(env.root).records if r.kind == "reducer"]
    assert [r.state for r in recs] == ["shadow_pending"]


# --- Task 9: composed tools are replayed before shadow ----------------------------------

BATCH_REPLY = {"kind": "composed_tool", "target": TRIAGE, "payload": {
    "name": "tuned_fetch", "description": "fetch messages in one step", "mutating": False,
    "parameters": {"type": "object", "properties": {"message_ids": {"type": "array"}}},
    "calls": [GET], "job_prompt": "Triage the inbox; fetch messages with tuned_fetch.",
    "replay_inputs": [{"param": "message_ids", "from_call": GET, "arg": "message_id",
                       "collect": "list"}],
    "program": (f"def main(args, call):\n"
                f"    return [call({GET!r}, {{'message_id': m}}) for m in args['message_ids']]\n")}}


def mail_runs(days=range(1, 13), n=8, chars=7000):
    """Triage runs: a search, then `n` fetches of `chars`-char mails on one turn, whose
    recorded args carry user_google_email (which the program never sends)."""
    base = NOW - 3 * 3600 + 7 * 3600
    out = []
    for d in days:
        calls = [(SEARCH, {"query": "newer_than:1d"}, "results " + "x" * 400, 1)]
        calls += [(GET, {"message_id": f"m{d}-{k}", "user_google_email": "o@example.test"},
                   (f"{RESULT_SENTINEL} {d}-{k} " + "y" * chars)[:chars], 2) for k in range(n)]
        out.append({"id": sid(TRIAGE, base - d * DAY), "source": "cron",
                    "started_at": base - d * DAY, "cost": 0.0, "in": tok(0.10) - 1000,
                    "out": 1000, "calls": calls, "final_turns": 3})
    return out


def test_replay_composed_hits_output_cap(env):
    """Acceptance check 3: a batch fetcher over 8 x 7,000-char mails exceeds the
    50,000-byte cap on replay and is rejected before shadow, at the draft's spend.
    Catches: an exact-args-only stub (reason would be "too few sessions")."""
    build(env, mail_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(BATCH_REPLY)])
    report = run_cycle(env, bridge, NOW, **replay_kw())
    assert len(oneshots(bridge)) == 1
    (rec,) = Ledger.load(env.root).records
    assert rec.kind == "composed_tool" and rec.state == "rejected"
    assert rec.reason == "replay: limit hit in 1 of 1 sessions"
    assert rec.spend_usd == pytest.approx(1200 * FLAT_RATE / 1e6)
    assert rec.est_saving_per_run is None
    assert rec.id in report.rejected and report.shadowed == []
    assert not (env.root / "shadow_jobs.json").exists()
    assert not (env.root / "tools").exists() or not any((env.root / "tools").iterdir())
    assert not report.restarted
    assert RESULT_SENTINEL not in json.dumps(report.__dict__, default=str)


def test_composed_draft_that_fits_reaches_shadow_with_an_estimate(env):
    """The same program on 2 small mails per run fits; it is shadowed and its record
    carries est_saving_per_run = monthly / (runs x 30/28)."""
    build(env, mail_runs(n=2, chars=300))
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(BATCH_REPLY)])
    calls = []
    real = _relay_mod().run_program

    def spy(*a, **kw):
        out = real(*a, **kw)
        calls.append(out)
        return out
    kw = replay_kw() | {"run_program": spy}
    report = run_cycle(env, bridge, NOW, **kw)
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "shadow_pending", rec.reason
    assert len(calls) == policy.REPLAY_MAX_SESSIONS
    # Recompute independently: the same replay on the same history, plus the turns part.
    from hermes_tuner.history import History
    from hermes_tuner.replay import composed_monthly, replay_composed
    (_m, (_i, user, _mt)), = oneshots(bridge)
    cand = json.loads(user)["candidate"]
    turns = cand["est_monthly_usd"] if cand["kind"] == "sequence" else 0.0
    runs = History.open(env.db).replay_runs(TRIAGE, NOW - 28 * DAY)
    rep = replay_composed(BATCH_REPLY["payload"], runs, real, pricing=FLAT,
                          argv_for=replay_kw()["replay_argv"])
    assert rep.eligible == 12 and turns > 0   # BATCH echoes bodies: the turns part pays
    want = composed_monthly(rep, turns) / (rep.eligible * 30 / 28)
    assert rec.est_saving_per_run == pytest.approx(want)
    assert rec.id in report.shadowed


def test_composed_estimate_not_stored_when_shadow_not_scheduled(env):
    """est_saving_per_run is set only on a draft whose shadow was scheduled."""
    build(env, mail_runs(n=2, chars=300))
    payload = dict(BATCH_REPLY["payload"], job_prompt=None)  # no prompt: shadow refused
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(dict(BATCH_REPLY, payload=payload))])
    run_cycle(env, bridge, NOW, **replay_kw())
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "rejected" and rec.reason.startswith("shadow not scheduled")
    assert rec.est_saving_per_run is None


def test_replay_budget_is_shared_across_drafts_in_a_cycle(env):
    """With an injected clock, the first draft's replay uses most of
    REPLAY_CYCLE_BUDGET_S; the second draft's replay gets no run and is rejected with
    "replay: no time left". The first is unaffected."""
    from hermes_tuner.cycle import CycleReport, _Cycle
    from hermes_tuner.detect import Candidate
    from hermes_tuner.history import History
    from hermes_tuner.model import Fix
    build(env, mail_runs(n=2, chars=300))
    t = [NOW]
    real = _relay_mod().run_program

    def slow(*a, **kw):
        t[0] += 70
        return real(*a, **kw)
    c = _Cycle(env, FakeBridge(jobs=jobs()), NOW, CycleReport(), clock=lambda: t[0],
               run_program=slow, replay_argv=replay_kw()["replay_argv"])
    c.history, c.pricing = History.open(env.db), FLAT
    cand = Candidate("sequence", f"seq:{SEARCH}>{GET}", TRIAGE, 12, 12, 1000, 1.0)
    fix = Fix("f-one", "composed_tool", TRIAGE, BATCH_REPLY["payload"], cand.key)
    assert c._replay_gate(cand, fix)[0] is None          # 4 runs fit (360 -> 80 left)
    assert t[0] == NOW + 4 * 70
    reason, per_run = c._replay_gate(cand, Fix("f-two", "composed_tool", TRIAGE,
                                               BATCH_REPLY["payload"], cand.key))
    assert (reason, per_run) == ("replay: no time left", None)
    assert t[0] == NOW + 4 * 70                          # no run started



def test_composed_replay_failure_rejects_with_the_type_only(env):
    """A replay that raises: rejected with the exception type; no message text (it
    could hold result text) reaches the ledger or the report."""
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(COMPOSED_REPLY)])

    def boom(*a, **kw):
        raise ValueError(RESULT_SENTINEL)
    report = run_cycle(env, bridge, NOW, **(replay_kw() | {"run_program": boom}))
    (rec,) = Ledger.load(env.root).records
    assert rec.state == "rejected" and rec.reason == "replay: internal error ValueError"
    assert rec.spend_usd > 0
    assert RESULT_SENTINEL not in json.dumps(report.__dict__, default=str)
    assert RESULT_SENTINEL not in (env.root / "ledger.json").read_text()


def test_composed_request_carries_the_turn_layout(env):
    """triage_calls puts one call on each of three turns."""
    build(env, triage_runs())
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(COMPOSED_REPLY)])
    run_cycle(env, bridge, NOW, **replay_kw())
    (_m, (_instr, user, _mt)), = oneshots(bridge)
    assert json.loads(user)["turn_layout"] == [1, 1, 1]


def test_turns_part_counts_only_for_sequence_candidates():
    """A bytes candidate's est_monthly_usd is carried bytes; adding it would count
    the bytes saving twice."""
    from hermes_tuner.cycle_draft import DraftSteps
    from hermes_tuner.detect import Candidate
    seq = Candidate("sequence", "seq:a>b", TRIAGE, 3, 3, 10, 0.7)
    assert DraftSteps._turns_usd(seq) == 0.7
    assert DraftSteps._turns_usd(Candidate("bytes", "bytes:" + GET, TRIAGE, 3, 3, 10, 0.7)) == 0.0


def test_default_replay_runner_is_the_plugin_relay_in_docker():
    from hermes_tuner.cycle_draft import load_relay
    relay = load_relay()
    argv = relay.production_argv("tuner-replay-abcd1234")
    assert argv[:len(policy.SANDBOX_ARGV)] == list(policy.SANDBOX_ARGV)
    assert argv[argv.index("--name") + 1] == "tuner-replay-abcd1234"
    assert relay.run_program.__name__ == "run_program"


# --- back-off (Task 10) ----------------------------------------------------------------

COPY_TEXT = f"{RESULT_SENTINEL} body " + "y" * 800  # what every triage GET returned
COPY = "payload.prompt: contains text copied from untrusted tool results"
COPY_REPLY = {"kind": "job_prompt", "target": TRIAGE,
              "payload": {"prompt": "Triage the inbox. " + COPY_TEXT, "add_toolsets": []}}


def request_key(call):
    (_m, (_instr, user, _mt)) = call
    return json.loads(user)["candidate"]["key"]


@pytest.mark.parametrize("prompt_changes, last_drafts", [(False, 0), (True, 1)])
def test_cycle_sends_no_draft_when_parked(env, prompt_changes, last_drafts):
    """Acceptance check 5. Cycles on days 1, 2, 10 and 19, each drafting a copy-check
    rejection for one candidate. Day 10 drafts because the 7-day skip after day 2 has
    ended. On day 19 the newest rejection is 9 days old, so "skip" cannot be the reason
    for no draft; only parking is. The control: the job's prompt changed before day 19,
    so the candidate is no longer parked and is drafted again.
    Catches: the skip measured from the oldest record (day 10 would not draft); parking
    without the input_hash check (the control would not draft)."""
    build(env, triage_runs(days=range(-19, 13)))  # runs before every cycle's 28 days
    drafts, keys = {}, []
    the_jobs = jobs()
    for day in (1, 2, 10, 19):
        if day == 19 and prompt_changes:
            the_jobs[TRIAGE]["prompt"] = "Triage the inbox. Be brief."
        bridge = FakeBridge(jobs=the_jobs, oneshot_script=[reply(COPY_REPLY)])
        heartbeat(env, NOW + (day - 1) * DAY - 60)
        report = run_cycle(env, bridge, NOW + (day - 1) * DAY)
        calls = oneshots(bridge)
        keys += [request_key(c) for c in calls]
        drafts[day] = len(calls)
        assert report.errors == [], report.errors
        assert report.parked == (1 if day == 19 and not prompt_changes else 0)
    assert drafts == {1: 1, 2: 1, 10: 1, 19: last_drafts}
    assert len(set(keys)) == 1  # every draft was for the same candidate
    recs = Ledger.load(env.root).records
    assert len(recs) == 3 + last_drafts
    from hermes_tuner.backoff import input_hash, reason_class
    assert [reason_class(r) for r in recs] == ["copy"] * len(recs)
    assert [r.input_hash for r in recs[:3]] == [input_hash(jobs()[TRIAGE], None)] * 3



def test_cycle_drafting_history_reaches_the_request(env):
    """The second draft's request names the first one's copy rejection by class and
    field only; the reason text and the copied text never reach the model."""
    build(env, triage_runs(days=range(-3, 13)))
    calls = []
    for day in (1, 2):
        bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(COPY_REPLY)])
        heartbeat(env, NOW + (day - 1) * DAY - 60)
        run_cycle(env, bridge, NOW + (day - 1) * DAY)
        calls += oneshots(bridge)
    first, second = (json.loads(c[1][1]) for c in calls)
    assert first["history"] == []
    assert second["history"] == [{"kind": "job_prompt", "state": "rejected",
                                  "reason_class": "copy", "fields": ["prompt"]}]
    assert "copied from" not in calls[1][1][1] and RESULT_SENTINEL not in calls[1][1][1]


def _cands(*keys):
    from hermes_tuner.detect import Candidate
    return [Candidate(kind="sequence", key=k, job_id=TRIAGE, sessions=10, occurrences=10,
                      result_chars=1000, est_monthly_usd=1.0 - i / 10)
            for i, k in enumerate(keys)]


def _parked_records(env, key, prompt="Triage the inbox.", target=TRIAGE):
    from hermes_tuner.backoff import input_hash
    h = input_hash({"prompt": prompt}, None)
    for d in (30, 20, 10):
        fix = Fix(f"2026-p{d}-{len(key)}", "job_prompt", target, {}, key)
        add_record(env, FixRecord(fix.id, fix.kind, target, key, "rejected", NOW - d * DAY,
                                  reason=COPY, input_hash=h), fix)


def _select_cycle(env, bridge):
    from hermes_tuner.cycle import CycleReport, _Cycle
    c = _Cycle(env, bridge, NOW, CycleReport())
    c.ledger, c.state = Ledger.load(env.root), {}
    return c


def test_parked_candidate_lets_next_candidate_draft(env):
    """The job's top candidate is parked; its second candidate is picked. Catches: the
    back-off check placed after the per-job pick (the job would get no candidate)."""
    _parked_records(env, "seq:top")
    c = _select_cycle(env, FakeBridge(jobs=jobs()))
    top, second = _cands("seq:top", "seq:second")
    assert c._select([top, second]) == [second]
    assert c.report.parked == 1
    assert c.report.errors == []


def test_parking_is_per_job(env):
    """The same candidate key under another job is not parked: records are matched on
    the candidate's job too."""
    _parked_records(env, "seq:top", target=CAL)
    c = _select_cycle(env, FakeBridge(jobs=jobs()))
    top, second = _cands("seq:top", "seq:second")
    assert c._select([top, second]) == [top]
    assert c.report.parked == 0


def test_select_fetches_each_job_once_and_drafting_reuses_it(env):
    """With records to check, _select reads the live job once; the draft reuses it.
    Two different failed outcomes: enough to need the job's hash, not backed off."""
    build(env, triage_runs())
    key = f"seq:{SEARCH}>{GET}>{MOD}"
    for i, (tag, reason) in enumerate((("unparseable", "unparseable draft"),
                                       ("bridge_error", "drafting call failed: x"))):
        fix = Fix(f"20261010-uu00000{i}", tag, TRIAGE, {}, key)
        add_record(env, FixRecord(fix.id, fix.kind, TRIAGE, key, "rejected",
                                  NOW - (2 - i) * DAY, reason=reason), fix)
    bridge = FakeBridge(jobs=jobs(), oneshot_script=[reply(JOB_PROMPT_REPLY)])
    run_cycle(env, bridge, NOW)
    (call,) = oneshots(bridge)
    assert request_key(call) == key
    assert [c for c in bridge.calls if c[0] == "get_job"] == [("get_job", (TRIAGE,))]
    assert len(json.loads(call[1][1])["history"]) == 2
    (new,) = [r for r in Ledger.load(env.root).records if not r.id.startswith("20261010-uu")]
    from hermes_tuner.backoff import input_hash
    assert new.input_hash == input_hash(jobs()[TRIAGE], None)


def test_select_skips_a_job_whose_fetch_fails(env):
    """A bridge error reading the job: its candidates are skipped this cycle, with one
    error, and nothing is drafted."""
    _parked_records(env, "seq:top")
    c = _select_cycle(env, FakeBridge(jobs=jobs(), fail={"get_job"}))
    assert c._select(_cands("seq:top", "seq:second", "seq:third")) == []
    assert len(c.report.errors) == 1 and "get_job" in c.report.errors[0]


def test_global_candidates_back_off_on_their_skill_text(env):
    """A skill_load candidate is parked on the hash of its job (none) and its skill
    text; editing the skill unparks it. Catches: global candidates not filtered, or
    the skill text left out of the hash."""
    from hermes_tuner.backoff import input_hash
    from hermes_tuner.detect import Candidate
    skill = env.skills_root / "notes" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: notes\n---\nbody\n")
    key = "skill_load:notes"
    h = input_hash(None, skill.read_text())
    for d in (30, 20, 10):
        fix = Fix(f"2026-s{d}", "skill", str(skill), {}, key)
        add_record(env, FixRecord(fix.id, "skill", "notes/SKILL.md", key, "rejected",
                                  NOW - d * DAY, reason="skill fixes must leave the "
                                  "frontmatter byte-identical", input_hash=h), fix)
    cand = Candidate(kind="skill_load", key=key, job_id=None, sessions=10, occurrences=10,
                     result_chars=1000, est_monthly_usd=1.0)
    c = _select_cycle(env, FakeBridge(jobs=jobs()))
    assert c._select([cand]) == [] and c.report.parked == 1
    skill.write_text("---\nname: notes\n---\nbody, edited\n")
    c = _select_cycle(env, FakeBridge(jobs=jobs()))
    assert c._select([cand]) == [cand] and c.report.parked == 0


# --- early rollback, trial_runs, estimate vs measured (Task 11) ---------------------------

def composed_trial(env):
    """A composed batch-tool fix taken to trial by the real cycle, live from NOW."""
    fix = shadow_setup(env, old_mod=[], new_mod=[], composed=True, tool_results=(TOOL_OK,))
    restarted_before_shadow(env)
    bridge = FakeBridge(jobs=jobs())
    run_cycle(env, bridge, NOW)
    led = Ledger.load(env.root)
    rec = led.get(fix.id)
    assert rec.state == "trial", rec.reason
    rec.live_from = NOW
    led.save()
    return fix, bridge


def own_tool_runs(results_per_run, cost=0.04):
    """Triage runs on the days after NOW, each calling tuned_triage once per result,
    then reading a message (a result that is not a composed tool's: not its own)."""
    base = NOW - 3 * 3600 + 7 * 3600
    return [{"id": sid(TRIAGE, base + (i + 1) * DAY), "source": "cron",
             "started_at": base + (i + 1) * DAY, "in": tok(cost) - 1000, "out": 1000,
             "calls": [("tuned_triage", {}, r) for r in results]
             + [(GET, {"message_id": "m"}, "b")]}
            for i, results in enumerate(results_per_run)]


@pytest.mark.parametrize("errors, calls, rolled", [
    (14, 41, True),    # the live calendar batch tool
    (1, 11, False),    # 9%: under 10% and fewer than 2
    (1, 10, True),     # 10% exactly
    (2, 30, True),     # 2 failures, under 10%
])
def test_batch_tool_rolls_back_early(env, errors, calls, rolled):
    fix, bridge = composed_trial(env)
    results = [TOOL_ERR] * errors + [TOOL_OK] * (calls - errors)
    later = own_tool_runs([results[:calls // 2], results[calls // 2:]])  # 2 runs: short of 5
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + later)
    report = run_cycle(env, bridge, NOW + 3 * DAY)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.trial_runs == 2
    if not rolled:
        assert rec.state == "trial" and rec.measured_saving_per_run is None
        assert bridge.jobs[TRIAGE]["prompt"] == "new prompt"
        return
    assert rec.state == "rolled_back" and fix.id in report.rolled_back
    assert rec.reason == f"trial: {errors} failures in {calls} calls (early)"
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."
    # baseline runs at $0.10, trial runs at $0.04
    assert rec.measured_saving_per_run == pytest.approx(0.06, abs=1e-4)


def test_early_rollback_runs_without_pricing(env):
    fix, bridge = composed_trial(env)
    later = own_tool_runs([[TOOL_ERR] * 7 + [TOOL_OK] * 13, [TOOL_ERR] * 7 + [TOOL_OK] * 14])
    env.db.unlink()
    build(env, SETUP_SESSIONS[env.root] + later)
    bridge.fail = {"pricing"}
    st = state(env)
    del st["pricing"]  # no cached list either
    (env.root / "cycle_state.json").write_text(json.dumps(st))
    report = run_cycle(env, bridge, NOW + 3 * DAY)
    assert "no model pricing" in report.errors
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "rolled_back", rec.reason
    assert rec.reason == "trial: 14 failures in 41 calls (early)"
    assert rec.measured_saving_per_run is None  # no price list to measure with


def reducer_trial(env, case):
    """A calendar reducer on trial, live 5 days: baseline runs (days 6-30) read each
    message once; 4 trial runs (days 1-4). Events: 20 reductions and 1 error in the
    trial runs, 1 error in a baseline run (not the trial's), a reduction and an error
    in a shadow run of the job (neither the trial's), a trial line with neither key,
    2 unreadable lines.
    case "child": a second trial error, logged under a compressed child's id.
    case "reread": trial run 3 reads m3 twice (1 re-read; the baseline mean is 0).
    case "window": trial re-reads [1, 30, 0, 0] against 25 baseline runs with 0, and
    2 runs (days 34-35) more than 28 days before live_from with 50 re-reads each. Right:
    2 runs exceed the mean 0. Trial runs leaking into the baseline (mean 31/29): 1.
    The older runs counted (mean 100/27): 1."""
    base = NOW - 3 * 3600 + 6 * 3600
    live = NOW - 5 * DAY

    def run(d, calls):
        return {"id": sid(CAL, base - d * DAY), "source": "cron", "started_at": base - d * DAY,
                "in": tok(0.10) - 500, "out": 500, "calls": calls}
    sessions = [run(d, [(GET, {"message_id": f"m{d}"}, "b")]) for d in range(6, 31)]
    if case == "window":
        sessions += [run(d, [(GET, {"message_id": f"m{d}"}, "b")] * 51) for d in (34, 35)]
    shadow_sid = sid(SHADOW_JOB, base - 3 * DAY - 1200)
    sessions.append({"id": shadow_sid, "source": "cron", "started_at": base - 3 * DAY - 1200,
                     "in": 100, "out": 10, "calls": [(GET, {"message_id": "m3"}, "b")]})
    trial_ids = []
    window = {4: 1, 3: 30, 2: 0, 1: 0}
    for d in (4, 3, 2, 1):
        calls = [(GET, {"message_id": f"m{d}"}, "b")]
        if case == "reread" and d == 3:
            calls.append((GET, {"message_id": "m3"}, "b"))
        if case == "window":
            calls += [(GET, {"message_id": f"m{d}"}, "b")] * window[d]
        sessions.append(run(d, calls))
        trial_ids.append(sessions[-1]["id"])
    child = "20261017_064500_abcd12"
    if case == "child":
        sessions[-2]["end_reason"] = "compression"
        sessions.append({"id": child, "source": "cron", "started_at": base - 2 * DAY + 300,
                         "parent": trial_ids[2], "calls": []})
    build(env, sessions)
    payload = {"tool": GET, "job": CAL, "steps": [{"op": "collapse_ws"}], "_base": CAL_BASE}
    fix = Fix(RFIX, "reducer", CAL, payload, "bytes:" + GET)
    led = add_record(env, FixRecord(fix.id, "reducer", CAL, fix.candidate_key, "trial",
                                    live - DAY, applied_at=live, live_from=live,
                                    baseline_runs=[brk(0.10)] * 10), fix)
    vdir = led.version_dir(fix.id)
    (vdir / "before.json").write_text(json.dumps({"tool": None, "job": None}))
    (vdir / "after.json").write_text(json.dumps(payload))
    events = [{"session_id": trial_ids[i % 4], "tool_call_id": f"c{i}", "before": 9,
               "after": 4} for i in range(20)]
    events.append({"session_id": trial_ids[0], "tool_call_id": "e0", "error": "TypeError"})
    events.append({"session_id": sid(CAL, base - 10 * DAY), "tool_call_id": "e1",
                   "error": "ambiguous"})
    events.append({"session_id": shadow_sid, "tool_call_id": "s0", "before": 9, "after": 4})
    events.append({"session_id": shadow_sid, "tool_call_id": "s1", "error": "ambiguous"})
    if case == "child":
        events.append({"session_id": child, "tool_call_id": "e2", "error": "ambiguous"})
    d = env.root / "reducer_events"
    d.mkdir()
    events.append({"session_id": trial_ids[1], "tool_call_id": "x"})  # neither: not a call
    (d / f"{fix.id}.jsonl").write_text("\n".join(
        [json.dumps(e) for e in events] + ["{broken", "[1]"]) + "\n")
    return fix


@pytest.mark.parametrize("case, failures", [("none", 1), ("child", 2), ("reread", 2),
                                            ("window", 3)])
def test_reducer_trial_rolls_back_early(env, case, failures):
    """The shadow run's reduction and error count as neither a call nor a failure:
    calls stay 20, and "none" stays at 1 failure (5%)."""
    fix = reducer_trial(env, case)
    (env.root / "shadow_jobs.json").write_text(json.dumps({SHADOW_JOB: {
        "fix_id": "20261012-r0000bbbb", "live_job_id": CAL, "run_at": NOW - 3 * DAY,
        "created_at": NOW - 4 * DAY, "scheduled_at": NOW - 4 * DAY,
        "removed_at": NOW - 2 * DAY}}))
    report = run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.trial_runs == 4
    if case == "none":
        assert rec.state == "trial", rec.reason
        return
    assert rec.state == "rolled_back" and fix.id in report.rolled_back
    assert rec.reason == f"trial: {failures} failures in 20 calls (early)"


class LockedHistory(History):
    """A state.db whose message reads fail as sqlite does when locked past its timeout."""

    def call_results(self, session_id):
        raise HistoryUnavailable("database is locked")

    def chain_ids(self, root_id):
        raise HistoryUnavailable("database is locked")


@pytest.mark.parametrize("kind", ["composed_tool", "reducer"])
def test_history_failure_in_early_check_does_not_stop_judging(env, kind):
    """A locked state.db during one record's early check is logged (type and message
    only) and that record waits; a later record 30 days past live_from is still rolled
    back at the deadline."""
    from hermes_tuner.cycle import CycleReport, _Cycle
    if kind == "composed_tool":
        fix, bridge = composed_trial(env)
        env.db.unlink()
        build(env, SETUP_SESSIONS[env.root] + own_tool_runs([[TOOL_ERR] * 5]))
        now, other, prompt = NOW + 3 * DAY, CAL, "Sync the calendar."
    else:
        fix, bridge = reducer_trial(env, "child"), FakeBridge(jobs=jobs())
        now, other, prompt = NOW, TRIAGE, "Triage the inbox."
    old = Fix("20260920-aaaa0010", "job_prompt", other, {"prompt": "p", "add_toolsets": []},
              "seq:old")
    led = add_record(env, FixRecord(old.id, old.kind, other, "seq:old", "trial", now - 31 * DAY,
                                    applied_at=now - 30 * DAY, live_from=now - 30 * DAY,
                                    baseline_runs=[brk(0.10)] * 10), old)
    vdir = led.version_dir(old.id)
    (vdir / "before.json").write_text(json.dumps({"prompt": prompt,
                                                  "enabled_toolsets": ["google"]}))
    (vdir / "after.json").write_text(json.dumps({"prompt": "p", "enabled_toolsets": ["google"]}))
    bridge.jobs[other]["prompt"] = "p"
    c = _Cycle(env, bridge, now, CycleReport())
    c.ledger, c.state, c.pricing, c.shadow_ids = Ledger.load(env.root), {}, FLAT, set()
    c.history = LockedHistory.open(env.db)
    c.sessions = c.history.sessions(now - 60 * DAY)
    c.judge_trials()
    led = Ledger.load(env.root)
    assert led.get(fix.id).state == "trial"
    assert led.get(old.id).state == "rolled_back"
    assert led.get(old.id).reason == "no verdict in 21 days"
    assert c.report.errors == [f"early check {fix.id}: HistoryUnavailable: database is locked"]


def test_trial_runs_written(env):
    """4 runs since live_from (plus an outage run with no calls, and a shadow run):
    trial_runs is 4, saved although the verdict is continue."""
    base = NOW - 3 * 3600 + 6 * 3600
    sessions = cal_runs(range(1, 5), 0.05) + cal_runs(range(6, 30), 0.50)
    sessions.append({"id": sid(CAL, base - 4.5 * DAY), "source": "cron",
                     "started_at": base - 4.5 * DAY, "in": 100, "out": 0, "calls": []})
    sessions.append({"id": sid(SHADOW_JOB, base - 3.5 * DAY), "source": "cron",
                     "started_at": base - 3.5 * DAY, "in": 100, "out": 0,
                     "calls": [("mcp__google__get_events", {}, "events")]})
    build(env, sessions)
    (env.root / "shadow_jobs.json").write_text(json.dumps({SHADOW_JOB: {
        "fix_id": "x", "live_job_id": CAL, "run_at": base - 3.5 * DAY,
        "created_at": base - 4 * DAY, "scheduled_at": base - 4 * DAY,
        "removed_at": base - 3 * DAY}}))
    fix = Fix("20261013-cccc0007", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
              "seq:cal")
    add_record(env, FixRecord(fix.id, fix.kind, CAL, fix.candidate_key, "trial", NOW - 5 * DAY,
                              applied_at=NOW - 5 * DAY, live_from=NOW - 5 * DAY,
                              baseline_runs=[brk(0.50, 500)] * 10), fix)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "trial" and rec.trial_runs == 4


@pytest.mark.parametrize("trial_cost, state, measured", [(0.05, "kept", 0.45),
                                                          (0.60, "rolled_back", -0.10)])
def test_measured_saving_recorded_on_verdict(env, trial_cost, state, measured):
    """Baseline median $0.50; 9 trial runs at `trial_cost` (one costlier outlier
    shows the median, not the mean, is used)."""
    runs = cal_runs(range(1, 9), trial_cost) + cal_runs([9], 5.0)
    build(env, runs)
    fix = Fix("20261009-cccc0002", "job_prompt", CAL, {"prompt": "p", "add_toolsets": []},
              "seq:cal")
    led = add_record(env, FixRecord(fix.id, fix.kind, CAL, fix.candidate_key, "trial",
                                    NOW - 10 * DAY, applied_at=NOW - 10 * DAY,
                                    live_from=NOW - 10 * DAY,
                                    baseline_runs=[brk(0.50, 500)] * 9 + [brk(9.0, 500)]), fix)
    vdir = led.version_dir(fix.id)
    (vdir / "before.json").write_text(json.dumps(
        {"prompt": "Sync the calendar.", "enabled_toolsets": ["google"]}))
    (vdir / "after.json").write_text(json.dumps({"prompt": "p", "enabled_toolsets": ["google"]}))
    js = jobs()
    js[CAL]["prompt"] = "p"
    run_cycle(env, FakeBridge(jobs=js), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == state and rec.trial_runs == 9
    assert rec.measured_saving_per_run == pytest.approx(measured, abs=1e-4)


def test_deadline_rollback_records_no_measured_saving(env):
    """2 runs since live_from 21 days ago: no verdict, rolled back at the deadline. Both
    sides have runs and there is a price list, but a deadline measures nothing."""
    build(env, triage_runs(range(22, 40)) + triage_runs([1, 2]))
    fix = Fix("20260928-aaaa0009", "job_prompt", TRIAGE, {"prompt": "p", "add_toolsets": []},
              "seq:x")
    led = add_record(env, FixRecord(fix.id, fix.kind, TRIAGE, "seq:x", "trial", NOW - 21 * DAY,
                                    applied_at=NOW - 21 * DAY, baseline_runs=[brk(0.10)] * 10,
                                    live_from=NOW - 21 * DAY), fix)
    vdir = led.version_dir(fix.id)
    (vdir / "before.json").write_text(json.dumps(
        {"prompt": "Triage the inbox.", "enabled_toolsets": ["google"]}))
    (vdir / "after.json").write_text(json.dumps({"prompt": "p", "enabled_toolsets": ["google"]}))
    js = jobs()
    js[TRIAGE]["prompt"] = "p"
    run_cycle(env, FakeBridge(jobs=js), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "rolled_back" and rec.reason == "no verdict in 21 days"
    assert rec.trial_runs == 2 and rec.measured_saving_per_run is None


def decided(led, i, state, days_ago, est, meas):
    led.records.append(FixRecord(f"20260901-eeee000{i}", "reducer", CAL, f"k{i}", state,
                                 NOW - 40 * DAY, decided_at=NOW - days_ago * DAY,
                                 est_saving_per_run=est, measured_saving_per_run=meas))


def test_weekly_line_estimates(tmp_path):
    led = Ledger(tmp_path)
    decided(led, 1, "kept", 2, 0.02, 0.01)
    assert weekly_line(led, [], NOW, FLAT).endswith(
        "; estimates: $0.02 vs measured $0.01 per run (mean of 1 fixes)")


def test_weekly_line_estimates_sum_decided_fixes_of_28_days(tmp_path):
    led = Ledger(tmp_path)
    decided(led, 1, "kept", 2, 0.02, 0.01)
    decided(led, 2, "rolled_back", 28 - 1 / 24, 0.04, -0.05)   # 27 days 23 h ago: in
    decided(led, 3, "kept", 28, 1.00, 1.00)                    # 28 days ago: out
    decided(led, 4, "kept", 3, 1.00, None)                     # no measurement: out
    decided(led, 5, "rolled_back", 3, None, 1.00)              # no estimate: out
    decided(led, 6, "trial", 3, 1.00, 1.00)                    # not decided: out
    line = weekly_line(led, [], NOW, FLAT)
    assert line.endswith("; estimates: $0.03 vs measured -$0.02 per run (mean of 2 fixes)")


def test_weekly_line_without_estimates_or_pricing_has_no_clause(tmp_path):
    led = Ledger(tmp_path)
    decided(led, 4, "kept", 3, 1.00, None)
    assert "estimates" not in weekly_line(led, [], NOW, FLAT)
    decided(led, 1, "kept", 2, 0.02, 0.01)
    assert "estimates" not in weekly_line(led, [], NOW, None)  # no dollars without a list


def test_weekly_line_parked(tmp_path):
    led = Ledger(tmp_path)
    decided(led, 1, "kept", 2, 0.02, 0.01)
    assert weekly_line(led, [], NOW, FLAT, errors=1, parked=3) == (
        "Hermes tuner: saved ~$0.00 this week (ESTIMATE at fake-model list prices, "
        "fake-flat), 0 applied, 1 kept, 0 rolled back; tuner spent $0.00"
        "; estimates: $0.02 vs measured $0.01 per run (mean of 1 fixes); parked: 3"
        "; errors: 1 (see ~/.hermes/tuner/cycle_state.json and ledger.json)")
    assert "parked" not in weekly_line(led, [], NOW, FLAT, parked=0)


def test_cycle_weekly_line_reports_parked(env):
    c = _select_cycle(env, FakeBridge(jobs=jobs()))
    c.sessions, c.pricing, c.shadow_ids = [], FLAT, set()
    c.report.parked = 2
    c.weekly()
    assert c.report.weekly.endswith("; parked: 2")
    assert (env.root / "outbox" / "weekly-2026-W43.txt").read_text().endswith("; parked: 2\n")
