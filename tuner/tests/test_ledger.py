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


def test_rollback_compares_read_backs_strictly_after_value(tmp_path):
    """Catches: a normalising compare treating the owner's YAML `off` (False) as the
    tuner's "off"."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    rec = do_apply(cfg_fix(TS, "off"), ledger, bridge, root, skills)
    bridge.config[TS] = False  # owner hand-edits `enabled: off`
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "owner_changed" and bridge.config[TS] is False
    assert not [c for c in bridge.calls if c[0] == "config_set"][1:]


def test_rollback_compares_read_backs_strictly_before_value(tmp_path):
    """Catches: the already-restored branch matching the owner's True against "on"."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    bridge.config[TS] = "on"
    rec = do_apply(cfg_fix(TS, "auto"), ledger, bridge, root, skills)
    bridge.config[TS] = True
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "owner_changed" and bridge.config[TS] is True


def test_owner_edit_checked_before_restorability_after_default_type_change(tmp_path):
    """Catches: reporting "rollback failed" for an owner-edited key when Hermes's
    default type changed after apply."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    bridge.config[TS] = "on"
    rec = do_apply(cfg_fix(TS, "auto"), ledger, bridge, root, skills)
    bridge.string_default_keys.clear()  # Hermes upgrade: "on" no longer restorable
    bridge.config[TS] = "owner value"
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "owner_changed" and bridge.config[TS] == "owner value"


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


# --- per-key coercion model and read-back verification ----------------------------

TS = "tools.tool_search.enabled"


def cfg_fix(key, value, fid="c1"):
    return Fix(fid, "config", "k", {"key": key, "value": value}, "k")


def test_string_default_key_accepts_on_off(tmp_path):
    """Catches: coercion applied regardless of the key's default type (the M2 bug)."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    rec = do_apply(cfg_fix(TS, "off"), ledger, bridge, root, skills)
    assert rec.state == "trial", rec.reason
    assert bridge.config[TS] == "off" and type(bridge.config[TS]) is str
    assert vfile(root, "c1", "after.json") == {"key": TS, "value": "off"}
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "rolled_back"
    assert bridge.config[TS] == "auto" and type(bridge.config[TS]) is str


def test_string_default_key_accepts_restorable_on_old_value(tmp_path):
    """Catches: the old-value check ignoring the key's default type."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    bridge.config[TS] = "on"
    rec = do_apply(cfg_fix(TS, "auto"), ledger, bridge, root, skills)
    assert rec.state == "trial", rec.reason
    assert do_rollback(rec, ledger, bridge, root, skills).state == "rolled_back"
    assert bridge.config[TS] == "on" and type(bridge.config[TS]) is str


def test_non_string_default_key_still_refuses_coercible_string(tmp_path):
    """Catches: dropping the coercion model entirely."""
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.x"] = 0.5
    with pytest.raises(ValueError):
        do_apply(cfg_fix("compression.x", "on"), ledger, bridge, root, skills, add=False)
    assert ledger.records == [] and bridge.config["compression.x"] == 0.5
    assert not [c for c in bridge.calls if c[0] in FakeBridge.MUTATING]
    assert ("config_default_is_str", ("compression.x",)) in bridge.calls


def test_non_string_default_key_refuses_unrestorable_old(tmp_path):
    """Catches: the old-value check removed."""
    root, skills, bridge, ledger = make(tmp_path)
    bridge.config["compression.x"] = -1
    rec = do_apply(cfg_fix("compression.x", 0.5), ledger, bridge, root, skills)
    assert rec.state == "bridge_error" and "restored" in rec.reason
    assert bridge.config["compression.x"] == -1
    assert not [c for c in bridge.calls if c[0] == "config_set"]


def test_default_lookup_failure_refuses(tmp_path):
    """Catches: guessing a type when Hermes cannot be asked."""
    root, skills, bridge, ledger = make(tmp_path, fail={"config_default_is_str"})
    rec = do_apply(cfg_fix(TS, "off"), ledger, bridge, root, skills)
    assert rec.state == "bridge_error" and "config_default_is_str" in rec.reason
    assert bridge.config[TS] == "auto"
    assert not [c for c in bridge.calls if c[0] == "config_set"]
    assert not (root / "versions" / "c1" / "after.json").exists()


class Writes(FakeBridge):
    """A bridge whose config_set stores `store(key, value)` instead of the value."""

    def __init__(self, store, **kw):
        super().__init__(**kw)
        self.store = store

    def config_set(self, key, value):
        self._enter("config_set", key, value)
        self.config[key] = self.store(key, value)


@pytest.mark.parametrize("key,old,new,stored", [
    ("compression.x", 0.5, 0.7, 0.9),    # a different value
    (TS, "auto", "off", False),          # "off" read back False: only a strict compare sees it
    ("compression.x", 1, 2, 2.0),        # same number, different type
    ("compression.x", 1, 2, "2"),  # string where a number was written
])
def test_readback_mismatch_restores_and_fails(tmp_path, key, old, new, stored):
    """Catches: trusting the write without reading back (or comparing normalised)."""
    root, skills, _, ledger = make(tmp_path)
    calls = []

    def store(k, v):
        calls.append(v)
        return stored if v == new else v  # the restore of the old value sticks

    bridge = Writes(store, config={key: old}, string_default_keys={TS})
    rec = do_apply(cfg_fix(key, new), ledger, bridge, root, skills)
    assert rec.state == "bridge_error"
    assert f"config {key} did not store as written" in rec.reason
    assert calls == [new, old]
    assert bridge.config[key] == old and type(bridge.config[key]) is type(old)
    assert not (root / "versions" / "c1" / "after.json").exists()
    assert ledger.active_for("k") is None


def test_readback_mismatch_with_failed_restore_names_value_left(tmp_path):
    """Catches: reporting a clean restore when the restore did not stick (managed no-op)."""
    root, skills, _, ledger = make(tmp_path)
    bridge = Writes(lambda k, v: 0.9, config={"compression.x": 0.5})  # nothing sticks
    rec = do_apply(cfg_fix("compression.x", 0.7), ledger, bridge, root, skills)
    assert rec.state == "bridge_error"
    assert "did not store as written" in rec.reason
    assert "restore" in rec.reason and "0.9" in rec.reason


def test_rollback_readback_mismatch_keeps_trial(tmp_path):
    """Catches: reporting rolled_back on a restore that did not stick."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    rec = do_apply(cfg_fix(TS, "off"), ledger, bridge, root, skills)
    assert rec.state == "trial"
    stuck = Writes(lambda k, v: "off", config=dict(bridge.config), string_default_keys={TS})
    out = do_rollback(rec, ledger, stuck, root, skills)
    assert out.state == "trial" and out.reason.startswith("rollback failed")
    assert Ledger.load(root).records[0].state == "trial"
    assert ledger.active_for("k") is out
    good = FakeBridge(config=dict(stuck.config), string_default_keys={TS})  # retry works
    assert do_rollback(out, ledger, good, root, skills).state == "rolled_back"
    assert good.config[TS] == "auto"


def test_rollback_default_lookup_failure_keeps_trial(tmp_path):
    """Catches: rolling back a config key without asking Hermes about its type."""
    root, skills, bridge, ledger = make(tmp_path, string_default_keys={TS})
    rec = do_apply(cfg_fix(TS, "off"), ledger, bridge, root, skills)
    bridge.fail.add("config_default_is_str")
    out = do_rollback(rec, ledger, bridge, root, skills)
    assert out.state == "trial" and out.reason.startswith("rollback failed")
    assert bridge.config[TS] == "off"


def test_hermes_coerce_mirrors_set_config_value():
    """Catches: the coercion model drifting from config.py:9277-9286."""
    from hermes_tuner.ledger import _hermes_coerce
    for s, want in [("on", True), ("YES", True), ("true", True), ("off", False), ("No", False),
                    ("5", 5), ("0.5", 0.5), ("-1", "-1"), ("1e-05", "1e-05"), ("auto", "auto")]:
        got = _hermes_coerce(s, False)
        assert got == want and type(got) is type(want), s
        assert _hermes_coerce(s, True) == s and type(_hermes_coerce(s, True)) is str


def test_config_default_is_str_op_is_read_only_everywhere():
    assert "config_default_is_str" not in FakeBridge.MUTATING
    b = FakeBridge(string_default_keys={TS})
    assert b.config_default_is_str(TS) is True and b.config_default_is_str("x") is False
    assert b.calls == [("config_default_is_str", (TS,)), ("config_default_is_str", ("x",))]
    assert "config_default_is_str" in bridge_entry.OPS


def test_hermes_bridge_default_is_str_sends_op(tmp_path):
    py = tmp_path / "py"
    py.write_text('#!/bin/sh\ncat > "$0.in"\necho "$2" > "$0.op"\necho \'{"result": true}\'\n')
    py.chmod(0o755)
    b = HermesBridge(hermes_py=str(py))
    assert b.config_default_is_str(TS) is True
    assert (tmp_path / "py.op").read_text().strip() == "config_default_is_str"
    assert json.loads((tmp_path / "py.in").read_text()) == {"key": TS}


def test_bridge_entry_default_is_str_uses_hermes_default_lookup(monkeypatch):
    import sys
    import types
    cfg = types.ModuleType("hermes_cli.config")
    cfg._default_value_for_key = lambda k: {"s": "auto", "b": True, "n": None}.get(k)
    pkg = types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg)
    assert bridge_entry.config_default_is_str("s") is True
    assert bridge_entry.config_default_is_str("b") is False
    assert bridge_entry.config_default_is_str("n") is False


def test_bridge_entry_default_is_str_fails_closed_without_hermes(monkeypatch):
    import io
    import sys
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # import raises ImportError
    out = io.StringIO()
    rc = bridge_entry.main(["x", "config_default_is_str"], io.StringIO('{"key": "k"}'), out,
                           io.StringIO())
    assert rc == 1 and "error" in json.loads(out.getvalue())

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


def test_bridge_entry_pricing_infers_maker_for_bare_ids():
    fireworks = _Entry(9.0, 9.0)
    table = {("deepseek-v4-pro", "deepseek"): V4, ("deepseek-v4-pro", "fireworks"): fireworks}
    vendors = {"deepseek", "fireworks"}
    # route has no list, or an all-zero one -> Hermes's own maker inference
    for route in (None, _Entry(0, 0, 0, 0, source="none", version="included-route")):
        get, calls = _lookup(table | {("deepseek-v4-pro", "nous"): route})
        got = bridge_entry.lookup_pricing("deepseek-v4-pro", "nous", None, "", get, vendors,
                                          detect_vendor=lambda m: "deepseek")
        assert got["input_per_m"] == 0.435 and got["model"] == "deepseek-v4-pro"
        assert calls[-1][:2] == ("deepseek-v4-pro", "deepseek") and len(calls) == 2
    # a maker with no docs snapshot is not accepted, and no other vendor is tried
    get, calls = _lookup(table)
    with pytest.raises(LookupError, match="no pricing for deepseek-v4-pro"):
        bridge_entry.lookup_pricing("deepseek-v4-pro", "nous", None, "", get, vendors,
                                    detect_vendor=lambda m: "someone-else")
    assert len(calls) == 1
    # no inference result -> error
    get, calls = _lookup(table)
    with pytest.raises(LookupError):
        bridge_entry.lookup_pricing("deepseek-v4-pro", "nous", None, "", get, vendors,
                                    detect_vendor=lambda m: None)
    # a prefixed id never asks detect_vendor
    def boom(m):
        raise AssertionError("detect_vendor called for a prefixed id")
    get, _ = _lookup({("deepseek-v4-pro", "deepseek"): V4})
    got = bridge_entry.lookup_pricing("deepseek/deepseek-v4-pro", "nous", None, "", get, vendors,
                                      detect_vendor=boom)
    assert got["input_per_m"] == 0.435


def test_fake_bridge_pricing_is_settable_and_not_mutating():
    assert "pricing" not in FakeBridge.MUTATING
    b = FakeBridge()
    assert b.pricing() == bridge_mod.FAKE_PRICE
    from factory import FLAT  # the tests' arithmetic assumes the fake's list is FLAT
    from hermes_tuner.model import Pricing
    assert Pricing.from_dict(bridge_mod.FAKE_PRICE) == FLAT
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
