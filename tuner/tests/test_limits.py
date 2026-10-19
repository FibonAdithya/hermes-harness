from __future__ import annotations

import random
import string
from pathlib import Path

import pytest

from hermes_tuner import policy
from hermes_tuner.fingerprint import CopyIndex
from hermes_tuner.limits import LimitContext, check_fix
from hermes_tuner.model import Fix

GET = "mcp__google__get_gmail_message_content"
LABEL = "mcp__google__manage_gmail_label"


def rtext(seed, n=200):
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters) for _ in range(n))


LEAK = rtext(99)  # stands in for untrusted tool-result text


def ctx(tmp_path, **kw):
    base = dict(
        copy_index=CopyIndex.build([LEAK]),
        scan=lambda s: [],
        bundled_skills=frozenset({"arxiv"}),
        skills_root=tmp_path / "skills",
        approved_callables=frozenset(),
        existing_tools=frozenset({"web_search", "tuned_old"}),
    )
    base.update(kw)
    return LimitContext(**base)


def composed(**over):
    p = {"name": "tuned_x", "description": "does x", "parameters": {"type": "object"},
         "calls": [GET], "mutating": False, "program": "print(1)", "job_prompt": None}
    p.update(over)
    return Fix("f1", "composed_tool", "3bc4e11d29b1", p, "k")


def jp(target="cc68e24f21cd", **over):
    p = {"prompt": "do the thing", "add_toolsets": []}
    p.update(over)
    return Fix("f2", "job_prompt", target, p, "k")


def cfg(key, value="x"):
    return Fix("f3", "config", key, {"key": key, "value": value}, "k")


def skill(path, text="guidance"):
    return Fix("f4", "skill", path, {"path": path, "text": text}, "k")


def reach(change, why="needed"):
    return Fix("f5", "new_reach", "x", {"change": change, "why": why}, "k")


def test_config_key_must_be_allowlisted(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(cfg("tools.tool_search.enabled", True), c) == []
    assert check_fix(cfg("display.foo"), c)
    assert check_fix(cfg("mcp_servers.google.tools.include"), c)
    assert check_fix(cfg("terminal.backend"), c)


def test_forbidden_beats_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "CONFIG_ALLOW_PREFIXES", ("",))
    assert check_fix(cfg("mcp_servers.x"), ctx(tmp_path))
    assert check_fix(cfg("display.foo"), ctx(tmp_path)) == []


def test_composed_tool_calls_must_be_callable(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(composed(), c) == []
    assert check_fix(composed(calls=["terminal"]), c)
    assert check_fix(composed(calls=["tuned_other"]), c)
    assert check_fix(composed(calls=["delegate_task"]), c)
    assert check_fix(composed(name="x"), c)


def test_composed_tool_cannot_shadow_builtin(tmp_path):
    assert check_fix(composed(name="web_search"), ctx(tmp_path))
    assert check_fix(composed(name="tuned_old"), ctx(tmp_path)) == []


def test_composed_tool_must_declare_mutating(tmp_path):
    c = ctx(tmp_path, approved_callables=frozenset())
    mut = "mcp__google__modify_gmail_message_labels"
    assert check_fix(composed(calls=[mut], mutating=False), c)
    assert check_fix(composed(calls=[mut], mutating=True), c) == []


def test_approved_callable_extends_allowlist(tmp_path):
    assert check_fix(composed(calls=[LABEL]), ctx(tmp_path))
    c = ctx(tmp_path, approved_callables=frozenset({LABEL}))
    assert check_fix(composed(calls=[LABEL]), c) == []


def test_job_prompt_only_for_editable_jobs(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(jp("cc68e24f21cd"), c) == []
    assert check_fix(jp("6fcdee8dee69"), c)
    assert check_fix(jp("8da9a6857256"), c)


def test_composed_tool_only_for_editable_jobs(tmp_path):
    f = composed()
    for target in ("6fcdee8dee69", "8da9a6857256"):
        bad = Fix(f.id, f.kind, target, f.payload, f.candidate_key)
        assert check_fix(bad, ctx(tmp_path))
    assert check_fix(f, ctx(tmp_path)) == []


def test_job_prompt_cannot_add_foreign_toolsets(tmp_path):
    assert check_fix(jp(add_toolsets=["terminal"]), ctx(tmp_path))
    assert check_fix(jp(add_toolsets=[policy.TOOLSET]), ctx(tmp_path)) == []


def mk(tmp_path, rel, text="---\nname: mine\n---\nold"):
    p = tmp_path / "skills" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return str(p)


def test_bundled_skill_rejected(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(skill(mk(tmp_path, "research/arxiv/SKILL.md")), c)
    assert check_fix(skill(mk(tmp_path, "mine/SKILL.md", "old"), "new"), c) == []
    assert check_fix(skill("../../.env"), c)
    mk(tmp_path, "mine/other.md")
    assert check_fix(skill(str(tmp_path / "skills/mine/other.md")), c)
    assert check_fix(skill("/etc/passwd/SKILL.md"), c)
    assert check_fix(skill(mk(tmp_path, "SKILL.md")), c)


def test_nested_under_bundled_rejected(tmp_path):
    p = mk(tmp_path, "research/arxiv/sub/SKILL.md", "old")
    assert check_fix(skill(p, "new"), ctx(tmp_path))


def test_new_skill_file_rejected(tmp_path):
    assert check_fix(skill(str(tmp_path / "skills/fresh/SKILL.md")), ctx(tmp_path))


FM = "---\nname: mine\n---\n"


def test_frontmatter_changes_rejected(tmp_path):
    p = mk(tmp_path, "mine/SKILL.md", FM + "old")
    c = ctx(tmp_path)
    assert check_fix(skill(p, FM + "shrunk"), c) == []
    assert check_fix(skill(p, "---\nname: arxiv\n---\nx"), c)
    assert check_fix(skill(p, "---\nname: other\n---\nx"), c)
    assert check_fix(skill(p, "no frontmatter"), c)
    assert check_fix(skill(p, "---\nname: mine\n---\n\nx"), c)  # whitespace change
    assert check_fix(skill(p, "---\nname:  mine\n---\nx"), c)
    nested = "---\nmetadata:\n  name: mine\nname: arxiv\n---\nx"
    assert check_fix(skill(p, nested), c)


def test_unterminated_or_added_frontmatter_rejected(tmp_path):
    p = mk(tmp_path, "mine/SKILL.md", "plain body")
    c = ctx(tmp_path)
    assert check_fix(skill(p, "shorter body"), c) == []
    assert check_fix(skill(p, "---\nname: arxiv\n---\nx"), c)
    p2 = mk(tmp_path, "mine2/SKILL.md", "---\nunterminated")
    assert check_fix(skill(p2, "---\nunterminated"), c) == []
    assert check_fix(skill(p2, "---\nunterminated\n---\nx"), c)


def test_bom_handled_like_hermes(tmp_path):
    p = mk(tmp_path, "mine/SKILL.md", "\ufeff" + FM + "old")
    c = ctx(tmp_path)
    assert check_fix(skill(p, "\ufeff" + FM + "new"), c) == []
    assert check_fix(skill(p, FM + "new"), c)


def test_existing_skill_named_bundled_rejected(tmp_path):
    c = ctx(tmp_path)
    top = mk(tmp_path, "a/SKILL.md", "---\nname: arxiv\n---\nold")
    assert check_fix(skill(top, "---\nname: arxiv\n---\nnew"), c)
    nest = mk(tmp_path, "b/SKILL.md", "---\nmetadata:\n  name: arxiv\n---\nold")
    assert check_fix(skill(nest, "---\nmetadata:\n  name: arxiv\n---\nnew"), c)


def test_dirname_fallback_rewrite_ok(tmp_path):
    p = mk(tmp_path, "mine/SKILL.md", "old text")
    assert check_fix(skill(p, "new text"), ctx(tmp_path)) == []


def test_bad_drafts_never_raise(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(skill("a\x00b/SKILL.md"), c)
    assert check_fix(Fix("f", "config", "x", ["a"], "k"), c)  # type: ignore[arg-type]
    assert check_fix(Fix("f", "config", ["x"], {"key": "a", "value": 1}, "k"), c)  # type: ignore[arg-type]
    assert check_fix(Fix("f", "skill", "x", {"path": 1, "text": 2}, "k"), c)


def test_skill_symlink_escape_rejected(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_text("x")
    (root / "evil").symlink_to(outside)
    assert check_fix(skill(str(root / "evil/SKILL.md")), ctx(tmp_path))


def test_scanner_findings_reject(tmp_path):
    c = ctx(tmp_path, scan=lambda s: ["prompt_injection"])
    assert check_fix(jp(), c)


def test_copied_text_rejects_every_text_field(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(jp(prompt="intro " + LEAK), c)
    assert check_fix(composed(description=LEAK), c)
    assert check_fix(composed(program=LEAK), c)
    assert check_fix(skill(str(tmp_path / "skills/mine/SKILL.md"), text=LEAK), c)
    ch = {"type": "callable_add", "tool": "web_fetch"}
    assert check_fix(reach(ch, why=LEAK), c)


def test_copy_in_nested_payload_rejected(tmp_path):
    params = {"type": "object", "properties": {"q": {"description": LEAK}},
              "required": [["a", LEAK]]}
    assert check_fix(composed(parameters=params), ctx(tmp_path))
    assert check_fix(composed(job_prompt=LEAK), ctx(tmp_path))
    assert check_fix(cfg("compression.x", LEAK), ctx(tmp_path))


def test_scanner_sees_nested_strings(tmp_path):
    seen = []
    c = ctx(tmp_path, scan=lambda s: seen.append(s) or [])
    check_fix(composed(parameters={"properties": {"q": {"description": "NESTED"}}}), c)
    assert "NESTED" in seen


def test_new_reach_types(tmp_path):
    c = ctx(tmp_path)
    assert check_fix(reach({"type": "mcp_include_add", "server": "google", "tool": "t"}), c) == []
    assert check_fix(reach({"type": "callable_add", "tool": "terminal"}), c) == []
    assert check_fix(reach({"type": "config_set", "tool": "t"}), c)
    assert check_fix(reach("callable_add"), c)


def test_unknown_kind_rejected(tmp_path):
    assert check_fix(Fix("f", "bogus", "x", {}, "k"), ctx(tmp_path))  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [{}, {"key": 3}, {"value": 1}])
def test_malformed_config_rejected_not_raised(tmp_path, payload):
    assert check_fix(Fix("f", "config", "x", payload, "k"), ctx(tmp_path))


def test_approval_cannot_admit_composed_tool_call(tmp_path):
    c = ctx(tmp_path, approved_callables=frozenset({"tuned_other"}))
    assert check_fix(composed(calls=["tuned_other"]), c)


def test_never_touch_beats_editable(tmp_path, monkeypatch):
    monkeypatch.setitem(policy.EDITABLE_JOBS, "8da9a6857256", "x")
    assert check_fix(jp("8da9a6857256"), ctx(tmp_path))


def test_unhashable_target_does_not_raise(tmp_path):
    bad = Fix("f", "job_prompt", ["x"], {"prompt": "p", "add_toolsets": []}, "k")  # type: ignore[arg-type]
    assert check_fix(bad, ctx(tmp_path))
