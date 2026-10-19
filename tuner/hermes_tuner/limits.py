"""Deterministic gate on model-drafted fixes (spec 5). Empty list = allowed."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import policy
from .fingerprint import CopyIndex
from .model import Fix


@dataclass(frozen=True)
class LimitContext:
    copy_index: CopyIndex
    scan: Callable[[str], list[str]]
    bundled_skills: frozenset[str]
    skills_root: Path
    approved_callables: frozenset[str]
    existing_tools: frozenset[str]


def _strings(obj: object, path: str = "payload") -> Iterator[tuple[str, str]]:
    """Every string in obj (values and dict keys), iteratively."""
    stack = [(path, obj)]
    while stack:
        p, o = stack.pop()
        if isinstance(o, str):
            yield p, o
        elif isinstance(o, dict):
            for k, v in o.items():
                if isinstance(k, str):
                    yield f"{p}.<key>", k
                stack.append((f"{p}.{k}", v))
        elif isinstance(o, (list, tuple)):
            stack.extend((f"{p}[{i}]", v) for i, v in enumerate(o))


def _check_config(payload: dict) -> list[str]:
    key = payload.get("key")
    if not isinstance(key, str) or "value" not in payload:
        return ["config payload needs a string key and a value"]
    if any(key.startswith(p) for p in policy.CONFIG_FORBIDDEN_PREFIXES):
        return [f"config key {key!r} is forbidden"]
    if not any(key.startswith(p) for p in policy.CONFIG_ALLOW_PREFIXES):
        return [f"config key {key!r} is not on the allowlist"]
    return []


def _check_job_target(fix: Fix) -> list[str]:
    if fix.target in policy.NEVER_TOUCH_JOBS:
        return [f"job {fix.target} is never edited"]
    if fix.target not in policy.EDITABLE_JOBS:
        return [f"job {fix.target} is not editable"]
    return []


def _check_job_prompt(fix: Fix) -> list[str]:
    reasons = _check_job_target(fix)
    extra = fix.payload.get("add_toolsets", [])
    if not isinstance(extra, list) or any(t != policy.TOOLSET for t in extra):
        reasons.append(f"add_toolsets may only contain {policy.TOOLSET!r}")
    return reasons


def _check_composed(fix: Fix, ctx: LimitContext) -> list[str]:
    reasons = _check_job_target(fix)
    p = fix.payload
    name = p.get("name")
    if not isinstance(name, str) or not policy.TOOL_NAME_RE.fullmatch(name):
        reasons.append(f"tool name must match {policy.TOOL_NAME_RE.pattern}")
    if isinstance(name, str) and name in ctx.existing_tools \
            and not name.startswith(policy.TOOL_PREFIX):
        reasons.append(f"tool name {name!r} would shadow an existing tool")
    calls = p.get("calls")
    if not isinstance(calls, list) or not all(isinstance(c, str) for c in calls):
        return reasons + ["calls must be a list of tool names"]
    allowed = policy.CALLABLE_TOOLS | ctx.approved_callables
    for c in calls:
        if c.startswith(policy.TOOL_PREFIX):
            reasons.append(f"call {c!r}: composed tools may not call composed tools")
        elif c not in allowed:
            reasons.append(f"call {c!r} is not on the callable allowlist")
    if any(c in policy.MUTATING_TOOLS for c in calls) and p.get("mutating") is not True:
        reasons.append("tool calls a mutating tool but is not declared mutating")
    return reasons


_CLOSE = re.compile(r"\n---\s*\n")


def _frontmatter_block(text: str) -> str | None:
    """Frontmatter exactly as Hermes delimits it (agent/skill_utils.py parse_frontmatter):
    optional BOM, leading '---', through the closing '---' line. None if absent."""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    if not body.startswith("---"):
        return None
    m = _CLOSE.search(body[3:])
    if not m:
        return None
    return bom + body[: m.end() + 3]


def _block_names_bundled(block: str | None, bundled: frozenset[str]) -> str | None:
    """Fail closed: any line (nested or not) naming a bundled skill."""
    for line in (block or "").splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() == "name" and val.strip().strip("\"'") in bundled:
            return val.strip().strip("\"'")
    return None


def _check_skill(fix: Fix, ctx: LimitContext) -> list[str]:
    path = fix.payload.get("path")
    text = fix.payload.get("text")
    if not isinstance(path, str) or not isinstance(text, str):
        return ["skill payload needs a string path and text"]
    try:
        root = ctx.skills_root.resolve()
        resolved = (ctx.skills_root / path).resolve()
        if not resolved.is_relative_to(root):
            return [f"skill path {path!r} escapes the skills directory"]
        if resolved.name != "SKILL.md":
            return ["skill path must end in SKILL.md"]
        if resolved.parent == root:
            return ["skill must live in its own directory"]
        if not resolved.is_file():
            return ["skill fixes may only rewrite an existing skill file"]
        old = resolved.read_text(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        return [f"skill path {path!r} cannot be resolved"]
    # Only the skill the candidate is about, whose text the model was shown.
    cand_kind, _, cand_name = fix.candidate_key.partition(":")
    if cand_kind != "skill_load" or resolved.parent.name != cand_name:
        return [f"skill fix must rewrite the candidate's skill, not {path!r}"]
    parts = resolved.relative_to(root).parts[:-1]
    hit = [p for p in parts if p in ctx.bundled_skills]
    if hit:
        return [f"skill {hit[0]!r} is bundled with Hermes"]
    old_block = _frontmatter_block(old)
    named = _block_names_bundled(old_block, ctx.bundled_skills)
    if named is not None:
        return [f"existing skill is named {named!r}, which is bundled with Hermes"]
    if _frontmatter_block(text) != old_block:
        return ["skill fixes must leave the frontmatter byte-identical"]
    return []


def _check_new_reach(fix: Fix) -> list[str]:
    change = fix.payload.get("change")
    kind = change.get("type") if isinstance(change, dict) else None
    if kind not in ("mcp_include_add", "callable_add"):
        return [f"new_reach change type {kind!r} is not supported"]
    return []


def check_fix(fix: Fix, ctx: LimitContext) -> list[str]:
    if not isinstance(fix.payload, dict):
        return ["payload must be an object"]
    if not isinstance(fix.target, str):
        return ["target must be a string"]
    if fix.kind == "config":
        reasons = _check_config(fix.payload)
    elif fix.kind == "job_prompt":
        reasons = _check_job_prompt(fix)
    elif fix.kind == "composed_tool":
        reasons = _check_composed(fix, ctx)
    elif fix.kind == "skill":
        reasons = _check_skill(fix, ctx)
    elif fix.kind == "new_reach":
        reasons = _check_new_reach(fix)
    else:
        return [f"unknown fix kind {fix.kind!r}"]
    for where, s in _strings(fix.payload):
        for finding in ctx.scan(s):
            reasons.append(f"{where}: scanner finding {finding}")
        if ctx.copy_index.contains_copy(s):
            reasons.append(f"{where}: contains text copied from untrusted tool results")
    return reasons
