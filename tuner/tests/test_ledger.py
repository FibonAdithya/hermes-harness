from __future__ import annotations

import base64
import inspect
import json
import os
import py_compile
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_tuner import bridge as bridge_mod
from hermes_tuner import bridge_entry, policy
from hermes_tuner.bridge import Bridge, BridgeError, FakeBridge, HermesBridge
from hermes_tuner.ledger import FixRecord, Ledger, apply, rollback
from hermes_tuner.model import Fix

JOB = "3bc4e11d29b1"
OLD = "old prompt"
NEW = "new prompt"


def make(tmp_path, **bridge_kw):
    root = tmp_path / "tuner"
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    bridge = FakeBridge(jobs={JOB: {"id": JOB, "prompt": OLD, "enabled_toolsets": ["web"]}},
                        config={"tools.tool_search.enabled": "auto"}, **bridge_kw)
    return root, skills, bridge, Ledger.load(root)


def record(fix, state="shadow_pending", **kw):
    return FixRecord(id=fix.id, kind=fix.kind, target=fix.target,
                     candidate_key=fix.candidate_key, state=state, created_at=1.0, **kw)


def do_apply(fix, ledger, bridge, root, skills, now=100.0, add=True):
    if add:
        ledger.add(record(fix), fix)
    return apply(fix, ledger, bridge, root, now, skills_root=skills)


def do_rollback(rec, ledger, bridge, root, skills, now=200.0):
    return rollback(rec, ledger, bridge, root, now, skills_root=skills)


def vfile(root, fix_id, name):
    return json.loads((root / "versions" / fix_id / name).read_text())


def job_fix(fid="f1", prompt=NEW, add=()):
    return Fix(fid, "job_prompt", JOB, {"prompt": prompt, "add_toolsets": list(add)}, "k")


def tool_payload(**kw):
    p = {"name": "tuned_x", "description": "d", "parameters": {"type": "object"},
         "calls": ["web_search"], "mutating": False, "program": "print(1)\n",
         "job_prompt": None}
    p.update(kw)
    return p


def test_apply_job_prompt_saves_before_and_after(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(job_fix(), ledger, bridge, root, skills)
    assert vfile(root, "f1", "before.json") == {"prompt": OLD, "enabled_toolsets": ["web"]}
    assert vfile(root, "f1", "after.json")["prompt"] == NEW
    assert bridge.get_job(JOB)["prompt"] == NEW
    assert rec.state == "trial" and rec.applied_at == 100.0 and rec.baseline_cost is None
    assert rec.needs_restart is False
    assert Ledger.load(root).records[0].state == "trial"  # persisted


def test_job_prompt_adds_toolset_only_to_list(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    do_apply(job_fix(add=["tuner"]), ledger, bridge, root, skills)
    assert bridge.get_job(JOB)["enabled_toolsets"] == ["web", "tuner"]
    # None toolsets (all tools) must stay None: a list would narrow the job.
    bridge.jobs["morning"] = {"id": "morning", "prompt": OLD, "enabled_toolsets": None}
    f = Fix("f2", "job_prompt", "morning", {"prompt": NEW, "add_toolsets": ["tuner"]}, "k2")
    do_apply(f, ledger, bridge, root, skills)
    assert bridge.get_job("morning")["enabled_toolsets"] is None
    assert vfile(root, "f2", "before.json")["enabled_toolsets"] is None


def test_unknown_payload_keys_are_ignored(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("f1", "job_prompt", JOB,
            {"prompt": NEW, "add_toolsets": [], "enabled": False, "schedule": "x"}, "k")
    do_apply(f, ledger, bridge, root, skills)
    (_, args), = [c for c in bridge.calls if c[0] == "update_job"]
    assert args == (JOB, {"prompt": NEW})


def test_rollback_restores_exact_previous_value(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.threshold"] = 0.8
    f = Fix("c1", "config", "compression.threshold",
            {"key": "compression.threshold", "value": 0.5}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.needs_restart is True
    assert bridge.config_get("compression.threshold") == 0.5
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "rolled_back"
    assert repr(bridge.config_get("compression.threshold")) == "0.8"


def test_rollback_job_prompt_restores_prompt_and_toolsets(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(job_fix(add=["tuner"]), ledger, bridge, root, skills)
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "rolled_back"
    job = bridge.get_job(JOB)
    assert job["prompt"] == OLD and job["enabled_toolsets"] == ["web"]


def test_rollback_skips_owner_edit(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(job_fix(), ledger, bridge, root, skills)
    bridge.jobs[JOB]["prompt"] = "owner text"
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "owner_changed"
    assert bridge.get_job(JOB)["prompt"] == "owner text"
    assert not [c for c in bridge.calls if c[0] == "update_job"][1:]  # only the apply call


def test_rollback_config_skips_owner_edit(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("c1", "config", "k", {"key": "compression.threshold", "value": 0.5}, "k")
    bridge.config["compression.threshold"] = 0.8
    rec = do_apply(f, ledger, bridge, root, skills)
    bridge.config["compression.threshold"] = 0.9
    assert do_rollback(rec, ledger, bridge, root, skills).state == "owner_changed"
    assert bridge.config["compression.threshold"] == 0.9


def test_config_coercion_does_not_look_like_owner_edit(tmp_path):
    # The live value read back as its string form ("false") is the value written
    # (False), not an owner edit.
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.enabled"] = True
    f = Fix("c1", "config", "k", {"key": "compression.enabled", "value": False}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    bridge.config["compression.enabled"] = "false"
    assert do_rollback(rec, ledger, bridge, root, skills).state == "rolled_back"
    assert bridge.config["compression.enabled"] is True


def test_config_without_previous_value_is_refused(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("c1", "config", "k", {"key": "compression.new_key", "value": 1}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "bridge_error" and "roll" in rec.reason
    assert "compression.new_key" not in bridge.config


def make_skill(skills, text="---\nname: mine\n---\nbody\n"):
    p = skills / "mine" / "SKILL.md"
    p.parent.mkdir()
    p.write_bytes(text.encode())
    p.chmod(0o640)
    return p


def test_skill_apply_writes_file_atomically_and_rolls_back(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    original = b"---\nname: mine\n---\nline\r\nbody \xff\xfe\n"  # CRLF and invalid utf-8
    p = skills / "mine" / "SKILL.md"
    p.parent.mkdir()
    p.write_bytes(original)
    p.chmod(0o640)
    f = Fix("s1", "skill", "mine", {"path": "mine/SKILL.md",
                                    "text": "---\nname: mine\n---\nnew\n"}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "trial"
    assert p.read_bytes() == b"---\nname: mine\n---\nnew\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o640
    assert [x.name for x in p.parent.iterdir()] == ["SKILL.md"]  # no temp files left
    assert base64.b64decode(vfile(root, "s1", "before.json")["b64"]) == original
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "rolled_back" and p.read_bytes() == original


def test_skill_rollback_skips_owner_edit(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    p = make_skill(skills)
    f = Fix("s1", "skill", "mine", {"path": "mine/SKILL.md",
                                    "text": "---\nname: mine\n---\nnew\n"}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    p.write_bytes(b"owner")
    assert do_rollback(rec, ledger, bridge, root, skills).state == "owner_changed"
    assert p.read_bytes() == b"owner"


def test_skill_outside_skills_root_is_refused(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    outside = tmp_path / "evil" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("x")
    f = Fix("s1", "skill", "x", {"path": "../evil/SKILL.md", "text": "y"}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "bridge_error"
    assert outside.read_text() == "x"


def test_composed_tool_apply_writes_tool_dir_and_rollback_removes_it(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("t1", "composed_tool", JOB, tool_payload(), "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    d = root / "tools" / "tuned_x"
    assert json.loads((d / "tool.json").read_text()) == {
        "name": "tuned_x", "description": "d", "parameters": {"type": "object"},
        "calls": ["web_search"], "mutating": False}
    assert (d / "program.py").read_text() == "print(1)\n"
    assert stat.S_IMODE((d / "program.py").stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert rec.state == "trial" and rec.needs_restart is policy.COMPOSED_TOOL_NEEDS_RESTART
    assert sorted(p.name for p in (root / "tools").iterdir()) == ["tuned_x"]
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "rolled_back" and not d.exists()


def test_go_live_rewrites_shadow_only_tool_without_the_flag(tmp_path):
    """F3: the tool dir written at shadow scheduling carries shadow_only; apply at
    go-live rewrites tool.json without it (and rollback still recognises it)."""
    from hermes_tuner.ledger import _tool_files, write_shadow_tool
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("t1", "composed_tool", JOB, tool_payload(), "k")
    write_shadow_tool(root / "tools", f.payload)
    d = root / "tools" / "tuned_x"
    assert json.loads((d / "tool.json").read_text())["shadow_only"] is True
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "trial"
    assert json.loads((d / "tool.json").read_text()) == _tool_files(f.payload)[0]
    assert do_rollback(rec, ledger, bridge, root, skills).state == "rolled_back"
    assert not d.exists()


def test_apply_refuses_a_foreign_tool_dir_of_the_same_name(tmp_path):
    from hermes_tuner.ledger import write_shadow_tool
    root, skills, bridge, ledger = make(tmp_path)
    write_shadow_tool(root / "tools", tool_payload() | {"program": "print(2)\n"})
    rec = do_apply(Fix("t1", "composed_tool", JOB, tool_payload(), "k"),
                   ledger, bridge, root, skills)
    assert rec.state == "bridge_error" and "already exists" in rec.reason
    assert (root / "tools" / "tuned_x" / "program.py").read_text() == "print(2)\n"


def test_composed_tool_with_job_prompt_updates_and_restores_job(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("t1", "composed_tool", JOB, tool_payload(job_prompt=NEW), "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    job = bridge.get_job(JOB)
    assert job["prompt"] == NEW and job["enabled_toolsets"] == ["web", "tuner"]
    before = vfile(root, "t1", "before.json")
    assert before["job"] == {"prompt": OLD, "enabled_toolsets": ["web"]}
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "rolled_back"
    assert bridge.get_job(JOB)["prompt"] == OLD
    assert not (root / "tools" / "tuned_x").exists()


def test_composed_tool_rollback_keeps_owner_edited_tool(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(Fix("t1", "composed_tool", JOB, tool_payload(), "k"),
                   ledger, bridge, root, skills)
    (root / "tools" / "tuned_x" / "program.py").write_text("print(2)\n")
    assert do_rollback(rec, ledger, bridge, root, skills).state == "owner_changed"
    assert (root / "tools" / "tuned_x" / "program.py").read_text() == "print(2)\n"


def test_composed_tool_failure_after_dir_write_removes_dir(tmp_path):
    root, skills, bridge, ledger = make(tmp_path, fail={"update_job"})
    f = Fix("t1", "composed_tool", JOB, tool_payload(job_prompt=NEW), "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "bridge_error"
    assert not (root / "tools" / "tuned_x").exists()
    assert bridge.jobs[JOB]["prompt"] == OLD


def test_bridge_error_marks_record_and_changes_nothing(tmp_path):
    root, skills, bridge, ledger = make(tmp_path, fail={"update_job"})
    rec = do_apply(job_fix(), ledger, bridge, root, skills)
    assert rec.state == "bridge_error" and "update_job" in rec.reason
    assert (root / "versions" / "f1" / "before.json").exists()
    assert not (root / "versions" / "f1" / "after.json").exists()
    assert bridge.jobs[JOB]["prompt"] == OLD
    assert rec.applied_at is None
    assert Ledger.load(root).records[0].state == "bridge_error"


def test_bridge_error_on_read_marks_record(tmp_path):
    root, skills, bridge, ledger = make(tmp_path, fail={"get_job"})
    rec = do_apply(job_fix(), ledger, bridge, root, skills)
    assert rec.state == "bridge_error"
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


def test_missing_job_is_bridge_error(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    bridge.jobs.clear()
    assert do_apply(job_fix(), ledger, bridge, root, skills).state == "bridge_error"


def test_new_reach_is_not_applied_here(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("n1", "new_reach", "x", {"change": {"type": "mcp_include_add"}}, "k")
    with pytest.raises(ValueError):
        do_apply(f, ledger, bridge, root, skills)
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


def test_foreign_toolset_is_refused_before_any_change(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    with pytest.raises(ValueError):
        do_apply(job_fix(add=["terminal"]), ledger, bridge, root, skills)
    assert bridge.jobs[JOB]["prompt"] == OLD


def test_apply_twice_and_rollback_of_unapplied_are_refused(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(job_fix(), ledger, bridge, root, skills)
    with pytest.raises(ValueError):
        apply(job_fix(), ledger, bridge, root, 300.0, skills_root=skills)
    rolled = do_rollback(rec, ledger, bridge, root, skills)
    with pytest.raises(ValueError):
        do_rollback(rolled, ledger, bridge, root, skills)


def test_failed_rollback_stays_applied_and_retry_restores(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(job_fix(add=["tuner"]), ledger, bridge, root, skills)
    bridge.fail.add("update_job")
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "trial" and "rollback failed" in out.reason and "update_job" in out.reason
    assert ledger.active_for(JOB) is out
    assert Ledger.load(root).records[0].state == "trial"
    assert bridge.jobs[JOB]["prompt"] == NEW
    bridge.fail.clear()
    out = do_rollback(out, ledger, bridge, root, skills)
    assert out.state == "rolled_back" and out.reason == ""
    assert bridge.jobs[JOB] == {"id": JOB, "prompt": OLD, "enabled_toolsets": ["web"]}
    assert ledger.active_for(JOB) is None


def test_rollback_retry_after_partial_restore_is_not_owner_changed(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    rec = do_apply(job_fix(), ledger, bridge, root, skills)
    bridge.jobs[JOB]["prompt"] = OLD  # restored by an earlier attempt that then failed
    assert do_rollback(rec, ledger, bridge, root, skills).state == "rolled_back"


def test_composed_rollback_failure_keeps_tool_dir_and_job(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("t1", "composed_tool", JOB, tool_payload(job_prompt=NEW), "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    bridge.fail.add("update_job")
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "trial" and "rollback failed" in out.reason
    assert (root / "tools" / "tuned_x" / "tool.json").exists()
    assert bridge.jobs[JOB]["prompt"] == NEW
    bridge.fail.clear()
    out = do_rollback(out, ledger, bridge, root, skills)
    assert out.state == "rolled_back"
    assert bridge.jobs[JOB]["prompt"] == OLD and not (root / "tools" / "tuned_x").exists()


@pytest.mark.parametrize("bad", [None, [1], {"a": 1}])
def test_unsettable_config_value_writes_nothing(tmp_path, bad):
    root, skills, bridge, ledger = make(tmp_path)
    f = Fix("c1", "config", "k", {"key": "tools.tool_search.enabled", "value": bad}, "k")
    with pytest.raises(ValueError):
        do_apply(f, ledger, bridge, root, skills, add=False)
    assert ledger.records == [] and not (root / "versions").exists()
    assert bridge.config["tools.tool_search.enabled"] == "auto"
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


@pytest.mark.parametrize("new", [-1, float("nan"), float("inf"), float("-inf"), 1e-05, "on",
                                 "1", -0.5])
def test_config_new_value_that_does_not_round_trip_writes_nothing(tmp_path, new):
    """M2: a value `hermes config set` would not store as given is refused before
    any write."""
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.x"] = 0.5
    f = Fix("c1", "config", "k", {"key": "compression.x", "value": new}, "k")
    with pytest.raises(ValueError):
        do_apply(f, ledger, bridge, root, skills, add=False)
    assert ledger.records == [] and not (root / "versions").exists()
    assert bridge.config["compression.x"] == 0.5
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]


@pytest.mark.parametrize("new", [0.7, 3, True, "auto"])
def test_config_new_value_that_round_trips_is_applied(tmp_path, new):
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.x"] = 0.5
    f = Fix("c1", "config", "k", {"key": "compression.x", "value": new}, "k")
    assert do_apply(f, ledger, bridge, root, skills).state == "trial"
    assert bridge.config["compression.x"] == new


@pytest.mark.parametrize("old", [{"a": 1}, [1, 2], -1, 1e-05, "on", float("nan")])
def test_config_with_unrestorable_old_value_is_refused(tmp_path, old):
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.x"] = old
    f = Fix("c1", "config", "k", {"key": "compression.x", "value": 0.5}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "bridge_error" and "restored" in rec.reason
    assert repr(bridge.config["compression.x"]) == repr(old)
    assert not (root / "versions" / "c1" / "after.json").exists()
    assert not [c for c in bridge.calls if c[0] == "config_set"]


def test_fix_id_cannot_escape_versions_dir(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    with pytest.raises(ValueError):
        do_apply(job_fix(fid="../../x"), ledger, bridge, root, skills)


def test_ledger_round_trip_modes_and_lookup(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    f = job_fix()
    ledger.add(record(f), f)
    ledger.save()
    assert stat.S_IMODE((root / "ledger.json").stat().st_mode) == 0o600
    for d in (root, root / "versions", root / "versions" / "f1"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "versions" / "f1" / "fix.json").stat().st_mode) == 0o600
    again = Ledger.load(root)
    assert again.records == ledger.records
    assert again.fix("f1") == f
    assert again.active_for(JOB).id == "f1"           # shadow_pending counts as active
    assert again.active_for("other") is None
    again.records[0].state = "rolled_back"
    assert again.active_for(JOB) is None


def test_ledger_writes_leave_no_temp_files(tmp_path):
    root, skills, bridge, ledger = make(tmp_path)
    do_apply(job_fix(), ledger, bridge, root, skills)
    names = sorted(p.name for p in root.rglob("*") if p.is_file())
    assert names == ["after.json", "before.json", "fix.json", "ledger.json"]


# --- bridge ---------------------------------------------------------------

def test_bridge_entry_compiles_and_op_table_matches_protocol():
    py_compile.compile(inspect.getsourcefile(bridge_entry), doraise=True)
    protocol = {n for n, v in vars(Bridge).items()
                if callable(v) and not n.startswith("_")}
    assert set(bridge_entry.OPS) == protocol - {"restart_gateway"}
    # HermesBridge exposes the same ops
    assert protocol <= {n for n in dir(HermesBridge) if not n.startswith("_")}


def test_bridge_entry_runs_without_hermes_and_reports_json_error(tmp_path):
    entry = inspect.getsourcefile(bridge_entry)
    r = subprocess.run(["python3", "-I", entry, "scan"], input=json.dumps({"text": "x"}),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "error" in json.loads(r.stdout)
    r = subprocess.run(["python3", "-I", entry, "nope"], input="{}",
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "unknown op" in json.loads(r.stdout)["error"]


def fake_python(tmp_path, body):
    p = tmp_path / "fakepy"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


def test_hermes_bridge_round_trip_and_errors(tmp_path):
    ok = fake_python(tmp_path, 'cat >/dev/null; echo \'{"result": ["a"]}\'\n')
    assert HermesBridge(hermes_py=ok).scan("x") == ["a"]
    bad = fake_python(tmp_path, 'cat >/dev/null; echo boom >&2; exit 3\n')
    with pytest.raises(BridgeError, match="boom"):
        HermesBridge(hermes_py=bad).scan("x")
    junk = fake_python(tmp_path, 'cat >/dev/null; echo not-json\n')
    with pytest.raises(BridgeError):
        HermesBridge(hermes_py=junk).scan("x")
    with pytest.raises(BridgeError):
        HermesBridge(hermes_py=str(tmp_path / "missing")).scan("x")
    slow = fake_python(tmp_path, 'sleep 5\n')
    with pytest.raises(BridgeError, match="timed out"):
        HermesBridge(hermes_py=slow, timeout=0.3).scan("x")
    loud = fake_python(tmp_path, 'cat >/dev/null; yes x | head -c 100000 >&2; exit 1\n')
    with pytest.raises(BridgeError) as e:
        HermesBridge(hermes_py=loud).scan("x")
    assert len(str(e.value)) < 2000


def test_hermes_bridge_sends_op_and_args(tmp_path):
    log = tmp_path / "log"
    py = fake_python(tmp_path, f'echo "$2" > {log}; cat >> {log}; echo \'{{"result": null}}\'\n')
    HermesBridge(hermes_py=py).config_set("compression.threshold", 0.5)
    op, payload = log.read_text().splitlines()
    assert op == "config_set"
    assert json.loads(payload) == {"key": "compression.threshold", "value": "0.5"}


def test_hermes_bridge_create_job_sends_extra(tmp_path):
    log = tmp_path / "log"
    py = fake_python(tmp_path, f'echo "$2" > {log}; cat >> {log}; echo \'{{"result": {{}}}}\'\n')
    HermesBridge(hermes_py=py).create_job("p", "s", "n", "local", ["web"], {"skills": ["a"]})
    op, payload = log.read_text().splitlines()
    assert op == "create_job"
    assert json.loads(payload)["extra"] == {"skills": ["a"]}
    with pytest.raises(ValueError):
        HermesBridge(hermes_py=py).create_job("p", "s", "n", "local", None, {"deliver": "x"})


def test_bridge_entry_create_job_passes_extra_to_hermes(monkeypatch):
    import sys
    import types
    seen = {}
    jobs_mod = types.ModuleType("cron.jobs")
    jobs_mod.create_job = lambda prompt, schedule, **kw: seen.update(kw, prompt=prompt) or {"id": "x"}
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs_mod)
    bridge_entry.create_job("p", "s", "n", "local", ["web"], {"skills": ["a"], "model": "m"})
    assert seen == {"prompt": "p", "name": "n", "deliver": "local", "enabled_toolsets": ["web"],
                    "skills": ["a"], "model": "m"}
    assert bridge_entry.CREATE_JOB_EXTRA_KEYS == bridge_mod.CREATE_JOB_EXTRA_KEYS


def _fake_hermes_env(monkeypatch, tmp_path, calls):
    import sys
    import types
    loader = types.ModuleType("hermes_cli.env_loader")

    def load_hermes_dotenv(*, hermes_home=None, project_env=None):
        calls.append(("dotenv", hermes_home))
        os.environ["TUNER_TEST_DOTENV"] = "loaded"
        return []
    loader.load_hermes_dotenv = load_hermes_dotenv
    consts = types.ModuleType("hermes_constants")
    consts.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.env_loader", loader)
    monkeypatch.setitem(sys.modules, "hermes_constants", consts)
    monkeypatch.delenv("TUNER_TEST_DOTENV", raising=False)


def test_bridge_entry_loads_hermes_dotenv_before_the_op(monkeypatch, tmp_path):
    """F9: Hermes's .env (provider keys) is loaded as Hermes entry points do, before
    any op runs."""
    import io
    calls = []
    _fake_hermes_env(monkeypatch, tmp_path, calls)

    def op(text):
        calls.append(("op", os.environ.get("TUNER_TEST_DOTENV")))
        return []
    monkeypatch.setitem(bridge_entry.OPS, "scan", op)
    out = io.StringIO()
    rc = bridge_entry.main(["entry", "scan"], io.StringIO('{"text": "x"}'), out, io.StringIO())
    assert rc == 0 and json.loads(out.getvalue()) == {"result": []}
    assert calls == [("dotenv", tmp_path), ("op", "loaded")]


@pytest.mark.parametrize("key", ["mcp_servers.google.tools.include", "mcp_servers.a-b_9.tools.include"])
def test_config_set_json_accepts_only_mcp_include_keys(key):
    FakeBridge().config_set_json(key, ["x"])


@pytest.mark.parametrize("key", ["mcp_servers.google.tools.exclude", "model",
                                 "mcp_servers.google.command", "mcp_servers..tools.include",
                                 "mcp_servers.a.b.tools.include", "plugins.enabled",
                                 "mcp_servers.google.tools.include\n"])
def test_config_set_json_refuses_other_keys(tmp_path, key):
    """m4: the JSON setter is for the approved MCP include change only."""
    with pytest.raises(ValueError):
        FakeBridge().config_set_json(key, ["x"])
    log = tmp_path / "log"
    py = fake_python(tmp_path, f'echo called > {log}; echo \'{{"result": null}}\'\n')
    with pytest.raises(ValueError):
        HermesBridge(hermes_py=py).config_set_json(key, ["x"])
    assert not log.exists()
    with pytest.raises(ValueError):
        bridge_entry.config_set_json(key, ["x"])
    assert bridge_entry.CONFIG_JSON_KEY.pattern == bridge_mod.CONFIG_JSON_KEY.pattern


def test_config_value_encoding():
    enc = bridge_mod.encode_config_value
    assert enc(True) == "true" and enc(False) == "false"
    assert enc(5) == "5" and enc(0.5) == "0.5" and enc("auto") == "auto"
    for bad in (None, {}, [1]):
        with pytest.raises(ValueError):
            enc(bad)


def test_restart_gateway_runs_systemctl_and_raises_on_failure(tmp_path):
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["timeout"] = argv, kw.get("timeout")
        return subprocess.CompletedProcess(argv, 1, "", "no unit")

    b = HermesBridge(runner=runner)
    with pytest.raises(BridgeError, match="no unit"):
        b.restart_gateway()
    assert seen["argv"] == ["systemctl", "--user", "restart", "hermes-gateway"]
    assert seen["timeout"] == 60


def test_fake_bridge_records_calls_and_scripts_oneshot():
    b = FakeBridge(oneshot_script=[{"text": "hi", "input_tokens": 1, "output_tokens": 2,
                                    "estimated_cost_usd": 0.0}])
    assert b.oneshot("i", "u", 10)["text"] == "hi"
    assert b.calls == [("oneshot", ("i", "u", 10))]
    with pytest.raises(BridgeError):
        b.oneshot("i", "u", 10)  # script exhausted
    b.restart_gateway()
    assert b.calls[-1] == ("restart_gateway", ())


def test_skill_write_failure_leaves_original_and_no_temp_file(tmp_path, monkeypatch):
    root, skills, bridge, ledger = make(tmp_path)
    p = make_skill(skills)
    original = p.read_bytes()

    real = os.replace

    def boom(src, dst):
        if Path(dst).name == "SKILL.md":
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    f = Fix("s1", "skill", "mine", {"path": "mine/SKILL.md",
                                    "text": "---\nname: mine\n---\nnew\n"}, "k")
    rec = do_apply(f, ledger, bridge, root, skills)
    assert rec.state == "bridge_error"
    assert p.read_bytes() == original
    assert [x.name for x in p.parent.iterdir()] == ["SKILL.md"]


# --- pricing ----------------------------------------------------------------

class _Entry:
    """Stand-in for Hermes's PricingEntry (Decimals in Hermes; any number works)."""
    def __init__(self, inp, out, read=None, write=None, source="provider_models_api",
                 url="https://example.invalid/models", version="v1"):
        from decimal import Decimal
        d = lambda x: None if x is None else Decimal(str(x))  # noqa: E731
        self.input_cost_per_million, self.output_cost_per_million = d(inp), d(out)
        self.cache_read_cost_per_million, self.cache_write_cost_per_million = d(read), d(write)
        self.source, self.source_url, self.pricing_version = source, url, version


def _lookup(table):
    """get_pricing_entry stand-in: (model, provider) -> entry; records each call."""
    calls = []

    def get(model, provider=None, base_url=None, api_key=None):
        calls.append((model, provider, base_url, api_key))
        return table.get((model, provider))
    return get, calls


V4 = _Entry(0.435, 0.87, read=0.003625, source="official_docs_snapshot",
            url="https://api-docs.deepseek.com/quick_start/pricing",
            version="deepseek-pricing-2026-07")


def test_bridge_entry_pricing_lookup_order():
    # 1. the route's own list, when usable, is used as is
    route = _Entry(0.5, 1.0, read=0.05)
    get, calls = _lookup({("deepseek/deepseek-v4-pro", "nous"): route,
                          ("deepseek-v4-pro", "deepseek"): V4})
    got = bridge_entry.lookup_pricing("deepseek/deepseek-v4-pro", "nous", "https://n.invalid",
                                      "k", get, {"deepseek"})
    assert calls == [("deepseek/deepseek-v4-pro", "nous", "https://n.invalid", "k")]
    assert got == {"model": "deepseek/deepseek-v4-pro", "input_per_m": 0.5, "output_per_m": 1.0,
                   "cache_read_per_m": 0.05, "cache_write_per_m": None,
                   "source": "provider_models_api", "source_url": "https://example.invalid/models",
                   "pricing_version": "v1"}
    assert all(type(v) is float for k, v in got.items() if k.endswith("_per_m") and v is not None)
    v4_dict = {"model": "deepseek/deepseek-v4-pro", "input_per_m": 0.435, "output_per_m": 0.87,
               "cache_read_per_m": 0.003625, "cache_write_per_m": None,
               "source": "official_docs_snapshot",
               "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
               "pricing_version": "deepseek-pricing-2026-07"}
    # 2a. route has no list -> the vendor named in the model id
    get, calls = _lookup({("deepseek-v4-pro", "deepseek"): V4})
    assert bridge_entry.lookup_pricing("deepseek/deepseek-v4-pro", "nous", None, "", get,
                                       {"deepseek"}) == v4_dict
    assert calls[1] == ("deepseek-v4-pro", "deepseek", None, None)
    # 2b. an all-zero entry (Hermes's included-route) is not a price list
    for zero in (_Entry(0, 0, 0, 0, source="none", version="included-route"),
                 _Entry(None, None)):
        get, _ = _lookup({("deepseek/deepseek-v4-pro", "nous"): zero,
                          ("deepseek-v4-pro", "deepseek"): V4})
        assert bridge_entry.lookup_pricing("deepseek/deepseek-v4-pro", "nous", None, "", get,
                                           {"deepseek"}) == v4_dict
    # 2c. a bare model id: the provider is the vendor only if Hermes has a snapshot for it
    get, calls = _lookup({("deepseek-v4-pro", "deepseek"): V4})
    got = bridge_entry.lookup_pricing("deepseek-v4-pro", "deepseek", None, "", get, {"deepseek"})
    assert got["input_per_m"] == 0.435 and got["model"] == "deepseek-v4-pro"
    get, calls = _lookup({("deepseek-v4-pro", "deepseek"): V4})
    with pytest.raises(LookupError, match="no pricing for deepseek-v4-pro"):
        bridge_entry.lookup_pricing("deepseek-v4-pro", "nous", None, "", get, {"deepseek"})
    assert len(calls) == 1  # "nous" is not a snapshot vendor: no retry
    # 3. both fail -> error
    get, calls = _lookup({("deepseek-v4-pro", "deepseek"): _Entry(0, 0)})
    with pytest.raises(LookupError, match="no pricing for deepseek/deepseek-v4-pro"):
        bridge_entry.lookup_pricing("deepseek/deepseek-v4-pro", "nous", None, "", get,
                                    {"deepseek"})
    assert len(calls) == 2


def test_fake_bridge_pricing_is_settable_and_not_mutating():
    assert "pricing" not in FakeBridge.MUTATING
    b = FakeBridge()
    assert b.pricing() == bridge_mod.FAKE_PRICE
    b.price = {"model": "m", "input_per_m": 1.0, "output_per_m": 2.0}
    got = b.pricing()
    got["input_per_m"] = 9.0  # a copy: the caller cannot change the fake's list
    assert b.pricing()["input_per_m"] == 1.0
    assert b.calls == [("pricing", ())] * 3
    b.fail.add("pricing")
    with pytest.raises(BridgeError):
        b.pricing()


def test_hermes_bridge_pricing_sends_op(tmp_path):
    log = tmp_path / "log"
    py = fake_python(tmp_path, f'echo "$2" > {log}; cat >/dev/null; '
                               'echo \'{"result": {"model": "m"}}\'\n')
    assert HermesBridge(hermes_py=py).pricing() == {"model": "m"}
    assert log.read_text().strip() == "pricing"


def test_bridge_entry_oneshot_returns_token_breakdown(monkeypatch):
    """input_tokens stays Hermes's fresh input; cache reads and writes come separately."""
    import sys
    import types
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Usage:  # the fields and property of Hermes's CanonicalUsage
        input_tokens: int = 0
        output_tokens: int = 0
        cache_read_tokens: int = 0
        cache_write_tokens: int = 0

        @property
        def prompt_tokens(self):
            return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    resp = types.SimpleNamespace(usage=object(), choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content="hi"))])
    aux = types.ModuleType("agent.auxiliary_client")
    aux.call_llm = lambda **kw: resp
    up = types.ModuleType("agent.usage_pricing")
    up.normalize_usage = lambda u, provider=None: Usage(5, 7, 300, 20)
    up.estimate_usage_cost = lambda *a, **k: types.SimpleNamespace(amount_usd=None,
                                                                   status="included")
    cfg = types.ModuleType("hermes_cli.config")
    cfg.load_config = lambda: {"model": {"default": "m", "provider": "p"}}
    for name, mod in (("agent", types.ModuleType("agent")), ("agent.auxiliary_client", aux),
                      ("agent.usage_pricing", up), ("hermes_cli", types.ModuleType("hermes_cli")),
                      ("hermes_cli.config", cfg)):
        monkeypatch.setitem(sys.modules, name, mod)
    got = bridge_entry.oneshot("i", "u", 10)
    assert (got["input_tokens"], got["cache_read_tokens"], got["cache_write_tokens"],
            got["output_tokens"]) == (5, 300, 20, 7)
    assert got["text"] == "hi"
