"""Plugin registration, the shadow guard and the composed-tool handler."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_relay import GET, LABELS, SEARCH, STUB, load_plugin

SHADOW_SID = "cron_abc123abc123_20261020_074000"
LIVE_SID = "cron_3bc4e11d29b1_20261020_074000"


class FakeCtx:
    def __init__(self, results=None):
        self.tools: dict[str, dict] = {}
        self.hooks: list[tuple[str, object]] = []
        self.dispatched: list[tuple[str, dict, dict]] = []
        self.results = results or {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler,
                            "override": override}

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))

    def dispatch_tool(self, name, args, **kw):
        self.dispatched.append((name, args, kw))
        return self.results.get(name, json.dumps({"ok": name}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "tuner" / "tools").mkdir(parents=True)
    (tmp_path / "cron").mkdir()
    return tmp_path


def add_tool(home: Path, dirname: str, *, name=None, calls=(SEARCH,), mutating=False,
             program="def main(args, call):\n    return 1\n"):
    d = home / "tuner" / "tools" / dirname
    d.mkdir()
    (d / "tool.json").write_text(json.dumps({
        "name": name or dirname, "description": "does a thing",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        "calls": list(calls), "mutating": mutating}))
    (d / "program.py").write_text(program)
    return d


def shadow_jobs_by_name(home: Path):
    (home / "tuner" / "shadow_jobs.json").write_text("{}")
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "abc123abc123", "name": "tuner-shadow-x"},
        {"id": "3bc4e11d29b1", "name": "triage"},
    ]}))


def shadow_lines(home: Path, sid: str) -> list[dict]:
    p = home / "tuner" / "shadow" / f"{sid}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


def test_shadow_guard_blocks_by_name_without_ledger(home):
    plugin = load_plugin()
    shadow_jobs_by_name(home)
    args = {"event_id": "e1", "action": "update"}
    res = plugin.shadow_guard("mcp__google__manage_event", args, session_id=SHADOW_SID,
                              task_id="t", tool_call_id="c", turn_id="u")
    assert res == {"action": "block", "message": "dry run: recorded"}
    lines = shadow_lines(home, SHADOW_SID)
    assert len(lines) == 1
    assert lines[0]["tool"] == "mcp__google__manage_event" and lines[0]["args"] == args
    assert plugin.shadow_guard("mcp__google__manage_event", args, session_id=LIVE_SID) is None
    assert plugin.shadow_guard("mcp__google__manage_event", args, session_id="") is None
    assert shadow_lines(home, LIVE_SID) == []


def test_shadow_guard_blocks_by_ledger(home):
    plugin = load_plugin()
    (home / "tuner" / "shadow_jobs.json").write_text(json.dumps(
        {"abc123abc123": {"fix_id": "f1", "live_job_id": "3bc4e11d29b1", "run_at": "x"}}))
    # No jobs.json at all.
    res = plugin.shadow_guard("write_file", {"path": "/x"}, session_id=SHADOW_SID)
    assert res and res["action"] == "block"


def test_shadow_guard_list_shaped_jobs_and_corrupt_files(home):
    plugin = load_plugin()
    (home / "tuner" / "shadow_jobs.json").write_text("{corrupt")
    (home / "cron" / "jobs.json").write_text(json.dumps([
        {"id": "abc123abc123", "name": "tuner-shadow-y"}]))
    assert plugin.shadow_guard("memory", {}, session_id=SHADOW_SID)["action"] == "block"
    (home / "cron" / "jobs.json").write_text("[corrupt")
    assert plugin.shadow_guard("memory", {}, session_id=SHADOW_SID) is None


def test_shadow_session_id_must_match_exactly(home):
    plugin = load_plugin()
    shadow_jobs_by_name(home)
    for sid in (SHADOW_SID + "\n", SHADOW_SID + "x", "x" + SHADOW_SID,
                "cron_abc123abc123_20261020_0740"):
        assert plugin.shadow_guard("memory", {}, session_id=sid) is None
    assert not (home / "tuner" / "shadow").exists()


def test_shadow_guard_blocks_memory_and_gated_compute(home):
    plugin = load_plugin()
    shadow_jobs_by_name(home)
    for name in ("memory", "mcp__compute__run_task", "execute_code", "skill_manage",
                 "cronjob", "send_message", "delegate_task", "terminal", "patch",
                 "mcp__google__draft_gmail_message", LABELS,
                 "process", "browser_click", "mcp__other__x", "ha_call_service",
                 "tool_call"):
        res = plugin.shadow_guard(name, {}, session_id=SHADOW_SID)
        assert res == {"action": "block", "message": "dry run: recorded"}, name
    for name in ("mcp__compute__night_status", "mcp__compute__job_logs", SEARCH, GET,
                 "mcp__google__get_events", "read_file", "web_search",
                 "tool_search", "skill_view", "todo", "tuned_x"):
        assert plugin.shadow_guard(name, {}, session_id=SHADOW_SID) is None, name


def test_shadow_blocked_policy():
    from hermes_tuner import policy
    assert not (policy.MUTATING_TOOLS & policy.SHADOW_ALLOW)
    assert all(policy.shadow_blocked(n) for n in policy.MUTATING_TOOLS)
    assert policy.shadow_blocked("mcp__compute__anything_new")
    assert policy.shadow_blocked("some_future_tool")
    assert not policy.shadow_blocked("mcp__compute__night_log")
    assert not policy.shadow_blocked("read_file")
    assert not policy.shadow_blocked("tuned_anything")
    # M3: an unwrapped tool_call fires the hook under the inner tool's name; a
    # tool_call that reaches the hook did not unwrap, so it is blocked
    assert policy.shadow_blocked("tool_call") and "tool_call" not in policy.SHADOW_ALLOW


# --- compression lineage ---------------------------------------------------

CHILD = "20261020_074512_a1b2c3"
GRANDCHILD = "20261020_075501_d4e5f6"


def make_state_db(home: Path, rows: list[tuple[str, str | None]]) -> Path:
    path = home / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("create table sessions (id text primary key, source text, "
                 "parent_session_id text)")
    conn.executemany("insert into sessions values (?, 'cron', ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_compression_descendants_of_shadow_session_are_blocked(home):
    plugin = load_plugin()
    shadow_jobs_by_name(home)
    make_state_db(home, [(SHADOW_SID, None), (CHILD, SHADOW_SID), (GRANDCHILD, CHILD)])
    for sid in (CHILD, GRANDCHILD):
        res = plugin.shadow_guard(LABELS, {"message_id": "m"}, session_id=sid)
        assert res == {"action": "block", "message": "dry run: recorded"}, sid
    # Recorded under the shadow job's own session.
    assert [ln["tool"] for ln in shadow_lines(home, SHADOW_SID)] == [LABELS, LABELS]


def test_compression_child_of_live_session_not_blocked(home):
    plugin = load_plugin()
    shadow_jobs_by_name(home)
    make_state_db(home, [(LIVE_SID, None), ("20261020_080000_0a0b0c", LIVE_SID)])
    assert plugin.shadow_guard(LABELS, {}, session_id="20261020_080000_0a0b0c") is None
    assert plugin.shadow_guard(LABELS, {}, session_id="20261020_090000_ffffff") is None


def test_db_missing_fails_closed_only_when_shadow_due(home):
    plugin = load_plugin()
    sid = "20261020_074512_0f0f0f"
    ledger = home / "tuner" / "shadow_jobs.json"
    ledger.write_text(json.dumps({"abc123abc123": {
        "fix_id": "f", "live_job_id": "3bc4e11d29b1", "run_at": time.time() - 3 * 3600}}))
    assert plugin.shadow_guard(LABELS, {}, session_id=sid) is None
    now_iso = datetime.now(timezone.utc).isoformat()
    ledger.write_text(json.dumps({"abc123abc123": {
        "fix_id": "f", "live_job_id": "3bc4e11d29b1", "run_at": now_iso}}))
    res = plugin.shadow_guard(LABELS, {}, session_id=sid)
    assert res == {"action": "block", "message": "dry run: recorded"}
    ledger.write_text("{}")
    assert plugin.shadow_guard(LABELS, {}, session_id=sid) is None


def test_registration_reads_tool_dir(home):
    plugin = load_plugin()
    add_tool(home, "tuned_alpha")
    add_tool(home, "tuned_beta", calls=(LABELS,), mutating=True)
    add_tool(home, "alpha_unprefixed")
    add_tool(home, "tuned_mismatch", name="tuned_other")
    bad = add_tool(home, "tuned_broken")
    (bad / "tool.json").write_text("{not json")
    nop = add_tool(home, "tuned_noprogram")
    (nop / "program.py").unlink()
    syn = add_tool(home, "tuned_syntax", program="def main(:\n")
    (home / "tuner" / "tools" / "tuned_file").write_text("not a dir")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert sorted(ctx.tools) == ["tuned_alpha", "tuned_beta"]
    assert all(t["toolset"] == "tuner" and not t["override"] for t in ctx.tools.values())
    assert all(n.startswith("tuned_") for n in ctx.tools)
    assert ctx.tools["tuned_alpha"]["schema"]["name"] == "tuned_alpha"
    assert "dry_run" in ctx.tools["tuned_beta"]["schema"]["parameters"]["properties"]
    assert "dry_run" not in ctx.tools["tuned_alpha"]["schema"]["parameters"]["properties"]
    assert sorted(h for h, _ in ctx.hooks) == ["on_session_start", "pre_tool_call"]
    assert syn.exists()


def test_session_start_rescans(home):
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.tools == {}
    add_tool(home, "tuned_late")
    hook = dict(ctx.hooks)["on_session_start"]
    hook(session_id="s", model="m", platform="telegram")
    assert list(ctx.tools) == ["tuned_late"]


def test_missing_tools_dir_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nothing"))
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.tools == {}


# --- composed-tool handler -------------------------------------------------

@pytest.fixture
def local_runner(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "runner_argv",
                        lambda name: [sys.executable, "-u", str(STUB)])
    return plugin


MUTATE = (f"def main(args, call):\n"
          f"    call({SEARCH!r}, {{'query': args.get('q', '')}})\n"
          f"    return call({LABELS!r}, {{'message_id': 'm1', 'add_label_ids': ['L']}})\n")


def handler_for(plugin, ctx, home, **tool):
    add_tool(home, "tuned_t", **tool)
    plugin.register(ctx)
    return ctx.tools["tuned_t"]["handler"]


def test_handler_forwards_session_and_task_ids(home, local_runner):
    ctx = FakeCtx()
    h = handler_for(local_runner, ctx, home, calls=(SEARCH, LABELS), mutating=True,
                    program=MUTATE)
    out = json.loads(h({"q": "a"}, task_id="t1", session_id="s1", user_task="u"))
    assert out == {"result": {"ok": LABELS}}
    assert [(n, kw) for n, _, kw in ctx.dispatched] == [
        (SEARCH, {"task_id": "t1", "session_id": "s1"}),
        (LABELS, {"task_id": "t1", "session_id": "s1"})]


def test_handler_in_shadow_session_forces_dry_run(home, local_runner):
    shadow_jobs_by_name(home)
    ctx = FakeCtx()
    h = handler_for(local_runner, ctx, home, calls=(SEARCH, LABELS), mutating=True,
                    program=MUTATE)
    out = json.loads(h({"q": "a", "dry_run": False}, task_id="t", session_id=SHADOW_SID))
    assert out == {"result": {"dry_run": True}}
    assert [n for n, _, _ in ctx.dispatched] == [SEARCH]
    lines = shadow_lines(home, SHADOW_SID)
    assert [ln["tool"] for ln in lines] == [LABELS]


def test_handler_in_compression_child_of_shadow_forces_dry_run(home, local_runner):
    shadow_jobs_by_name(home)
    make_state_db(home, [(SHADOW_SID, None), (CHILD, SHADOW_SID)])
    ctx = FakeCtx()
    h = handler_for(local_runner, ctx, home, calls=(SEARCH, LABELS), mutating=True,
                    program=MUTATE)
    out = json.loads(h({"q": "a"}, task_id="t", session_id=CHILD))
    assert out == {"result": {"dry_run": True}}
    assert [n for n, _, _ in ctx.dispatched] == [SEARCH]
    assert [ln["tool"] for ln in shadow_lines(home, SHADOW_SID)] == [LABELS]


def test_handler_dry_run_arg_on_mutating_tool(home, local_runner):
    ctx = FakeCtx()
    h = handler_for(local_runner, ctx, home, calls=(SEARCH, LABELS), mutating=True,
                    program=MUTATE)
    out = json.loads(h({"q": "a", "dry_run": True}, session_id="s1"))
    assert out == {"result": {"dry_run": True}}
    assert [n for n, _, _ in ctx.dispatched] == [SEARCH]


def test_handler_undeclared_call_refused(home, local_runner):
    ctx = FakeCtx()
    h = handler_for(local_runner, ctx, home, calls=(SEARCH,), program=MUTATE)
    out = json.loads(h({"q": "a"}, session_id="s1"))
    assert out == {"result": {"error": f"{LABELS} is not callable"}}
    assert [n for n, _, _ in ctx.dispatched] == [SEARCH]


def test_handler_declared_but_unlisted_call_refused_until_approved(home, local_runner):
    from hermes_tuner import proposals
    prog = "def main(args, call):\n    return call('web_fetch', {'url': 'u'})\n"
    ctx = FakeCtx()
    h = handler_for(local_runner, ctx, home, calls=("web_fetch",), program=prog)
    assert json.loads(h({}, session_id="s1")) == {
        "result": {"error": "web_fetch is not callable"}}
    assert ctx.dispatched == []
    change = {"type": "callable_add", "tool": "web_fetch"}
    (home / "tuner" / "proposals").mkdir()
    (home / "tuner" / "proposals" / "p1.json").write_text(json.dumps(
        {"id": "p1", "change": change, "why": "", "hash": proposals.change_hash(change)}))
    (home / "broker" / "state").mkdir(parents=True)
    (home / "broker" / "state" / "approved_proposals.json").write_text(json.dumps(
        [{"proposal_id": "p1", "hash": proposals.change_hash(change), "approved_at": "x"}]))
    assert json.loads(h({}, session_id="s1")) == {"result": {"ok": "web_fetch"}}
    assert [n for n, _, _ in ctx.dispatched] == ["web_fetch"]


# --- shadow-only tools (F3) and the heartbeat (F4) ---------------------------

def test_shadow_only_tool_refused_outside_shadow_sessions(home, local_runner):
    shadow_jobs_by_name(home)
    ctx = FakeCtx()
    d = add_tool(home, "tuned_t", calls=(SEARCH, LABELS), mutating=True, program=MUTATE)
    spec = json.loads((d / "tool.json").read_text())
    # shadow_jobs_by_name: job abc123abc123 is named tuner-shadow-x, i.e. fix "x"
    (d / "tool.json").write_text(json.dumps(dict(spec, shadow_only=True, shadow_fix="x")))
    local_runner.register(ctx)
    h = ctx.tools["tuned_t"]["handler"]
    out = json.loads(h({"q": "a"}, task_id="t", session_id=LIVE_SID))
    assert "error" in out and "shadow" in out["error"]
    assert ctx.dispatched == []
    # in a shadow session it runs (as a dry run)
    out = json.loads(h({"q": "a"}, task_id="t", session_id=SHADOW_SID))
    assert out == {"result": {"dry_run": True}}
    assert [n for n, _, _ in ctx.dispatched] == [SEARCH]


OTHER_SHADOW_SID = "cron_def456def456_20261020_054000"


def shadow_only_tool(home, ctx, plugin, shadow_fix):
    d = add_tool(home, "tuned_t", calls=(SEARCH,))
    spec = json.loads((d / "tool.json").read_text())
    if shadow_fix is not None:
        spec["shadow_fix"] = shadow_fix
    (d / "tool.json").write_text(json.dumps(dict(spec, shadow_only=True)))
    plugin.register(ctx)
    return ctx.tools["tuned_t"]["handler"]


def two_shadow_jobs(home):
    """Two shadows are scheduled, for fixes F1 (job abc123abc123) and F2 (job def456def456)."""
    (home / "tuner" / "shadow_jobs.json").write_text(json.dumps({
        "abc123abc123": {"fix_id": "F1", "live_job_id": "3bc4e11d29b1", "run_at": time.time()},
        "def456def456": {"fix_id": "F2", "live_job_id": "cc68e24f21cd", "run_at": time.time()},
    }))
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "abc123abc123", "name": "tuner-shadow-F1"},
        {"id": "def456def456", "name": "tuner-shadow-F2"},
        {"id": "3bc4e11d29b1", "name": "morning-briefing"},
    ]}))


def test_shadow_only_tool_runs_only_in_its_own_fixs_shadow(home, local_runner):
    """2026-10-23: the morning briefing's shadow called the calendar fix's shadow-only
    tool 7 times. A shadow-only tool runs only in the shadow of the fix that wrote it."""
    two_shadow_jobs(home)
    ctx = FakeCtx()
    h = shadow_only_tool(home, ctx, local_runner, "F1")
    assert json.loads(h({"q": "a"}, session_id=SHADOW_SID)) == {"result": 1}
    out = json.loads(h({"q": "a"}, session_id=OTHER_SHADOW_SID))
    assert out == {"error": "tuned_t is in another fix's shadow trial and runs only "
                            "in that fix's shadow session"}
    out = json.loads(h({"q": "a"}, session_id=LIVE_SID))
    assert "error" in out and "shadow" in out["error"]


def test_shadow_only_tool_owner_found_by_job_name_without_ledger_entry(home, local_runner):
    """shadow_jobs.json lacks the entry (the plugin recognises the shadow by its job name,
    tuner-shadow-<fix id>): the owner comes from the name too."""
    shadow_jobs_by_name(home)  # abc123abc123 is tuner-shadow-x
    ctx = FakeCtx()
    h = shadow_only_tool(home, ctx, local_runner, "x")
    assert json.loads(h({"q": "a"}, session_id=SHADOW_SID)) == {"result": 1}


def test_shadow_only_tool_without_an_owner_is_refused_everywhere(home, local_runner):
    """A tool.json written before it named its fix belongs to no shadow: refused."""
    two_shadow_jobs(home)
    ctx = FakeCtx()
    h = shadow_only_tool(home, ctx, local_runner, None)
    for session in (SHADOW_SID, OTHER_SHADOW_SID, LIVE_SID):
        assert "error" in json.loads(h({"q": "a"}, session_id=session))
    assert ctx.dispatched == []


@pytest.mark.parametrize("shadow_fix", [None, "F1"])
def test_shadow_only_tool_refused_when_the_shadows_fix_is_unknown(home, local_runner,
                                                                  shadow_fix):
    """The shadow's ledger entry has no fix_id and no job carries its name: no fix owns
    this session, so no shadow-only tool runs in it, an owner-less one included."""
    (home / "tuner" / "shadow_jobs.json").write_text(json.dumps({
        "abc123abc123": {"live_job_id": "3bc4e11d29b1", "run_at": time.time()}}))
    ctx = FakeCtx()
    h = shadow_only_tool(home, ctx, local_runner, shadow_fix)
    assert "another fix" in json.loads(h({"q": "a"}, session_id=SHADOW_SID))["error"]
    assert ctx.dispatched == []


def test_shadow_fix_must_be_a_string(home):
    plugin = load_plugin()
    d = add_tool(home, "tuned_t")
    spec = json.loads((d / "tool.json").read_text())
    (d / "tool.json").write_text(json.dumps(dict(spec, shadow_only=True, shadow_fix=7)))
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.tools == {}


def test_shadow_only_must_be_a_boolean(home):
    plugin = load_plugin()
    d = add_tool(home, "tuned_t")
    spec = json.loads((d / "tool.json").read_text())
    (d / "tool.json").write_text(json.dumps(dict(spec, shadow_only="yes")))
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.tools == {}


def test_register_writes_heartbeat(home):
    import os
    plugin = load_plugin()
    t0 = time.time()
    plugin.register(FakeCtx())
    beat = json.loads((home / "tuner" / "plugin_heartbeat.json").read_text())
    assert beat["pid"] == os.getpid()
    assert t0 <= beat["registered_at"] <= time.time()
    assert not [p for p in (home / "tuner").iterdir() if p.name.startswith(".tmp")]


def test_heartbeat_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "tuner").write_text("a file, not a dir")
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert sorted(h for h, _ in ctx.hooks) == ["on_session_start", "pre_tool_call"]
