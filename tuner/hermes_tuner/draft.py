"""Drafting: build the model request, parse the reply into a Fix (spec 4.3).

The request carries statistics, the job prompt, skill text, tool schemas and the
call sequence by tool name and argument KEYS only. No email text, tool result or
argument value reaches the model. The parser is strict and applies no policy;
`limits.check_fix` does that.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from . import policy
from .bridge import Bridge
from .detect import Candidate
from .model import Fix

KINDS = ("composed_tool", "job_prompt", "config", "skill", "new_reach")
JOB_FIELDS = ("id", "name", "schedule", "prompt", "enabled_toolsets")
CANDIDATE_FIELDS = ("kind", "key", "job_id", "sessions", "occurrences", "result_chars",
                    "est_monthly_usd")

INSTRUCTIONS = f"""You draft one fix that reduces wasted work in a scheduled agent. The user message is a JSON document: statistics for one detected pattern (candidate), the job it occurs in (job), optionally the text of a skill (skill_text), the schemas of tools involved (tool_schemas), and call_shape, the sequence of tool calls as [tool name, [argument keys]]. It contains no tool results and no argument values.

Reply with a single JSON object and nothing else: no prose, no code fence. Shape: {{"kind": ..., "target": ..., "payload": {{...}}}}. Choose exactly one kind.

1. "composed_tool": one new tool that runs the observed call sequence in a single step. target is the job id from the request. payload: {{"name": str, "description": str, "parameters": object (JSON schema), "calls": [str], "mutating": bool, "program": str, "job_prompt": str or null}}.
   - name must start with "{policy.TOOL_PREFIX}". The tool is placed in the "{policy.TOOLSET}" toolset.
   - description says what the tool does, derived from its structure (which tools it calls and what it returns), not from any email content.
   - program is Python source defining `def main(args: dict, call) -> object`. `call(name, args)` runs a tool and returns its result parsed from JSON (or the raw string if it is not JSON). The return value must be JSON-serialisable. The program runs in a sandbox with no network, so every external effect goes through call().
   - calls must list every tool the program calls. The run is refused any other tool. Composed tools may not call composed tools.
   - Allowed tools: {", ".join(sorted(policy.CALLABLE_TOOLS))}.
   - web_search and web_extract are refused after any other call in the same run, so call them first or not at all.
   - If the program calls a mutating tool ({", ".join(sorted(policy.MUTATING_TOOLS & policy.CALLABLE_TOOLS))}), set mutating to true and add a boolean "dry_run" argument to parameters; the program must not perform effects when it is set (calls are recorded instead).
   - job_prompt is optional new job prompt text telling the agent to use the tool, or null.
2. "job_prompt": rewrite a job's prompt. target is the job id from the request. payload: {{"prompt": str, "add_toolsets": [str]}}. add_toolsets may only be ["{policy.TOOLSET}"] or [].
3. "config": change one setting. target is the key. payload: {{"key": str, "value": string, number or boolean}}. Keys are allowed only under these prefixes: {", ".join(policy.CONFIG_ALLOW_PREFIXES)}.
4. "skill": shrink a skill that is loaded often and is large. target is the skill path. payload: {{"path": str, "text": str}}. A skill fix may only rewrite an existing SKILL.md that is not bundled with the agent, and must keep its frontmatter block (the leading --- block) byte-identical. Shrink the body, keep the decision guidance, and point at the tool for mechanics.
5. "new_reach": use only when the fix truly needs a tool outside the allowed list. payload: {{"change": {{"type": "mcp_include_add", "server": str, "tool": str}} or {{"type": "callable_add", "tool": str}}, "why": str}}. target is the tool name. The owner must approve it.

job_prompt and composed_tool fixes target only the job in the request. Do not include keys that are not listed. Do not include secrets, message ids or any text other than your own design.
"""


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool))


def _clean_composed(p: dict) -> dict | None:
    jp = p.get("job_prompt")
    if not (isinstance(p.get("name"), str) and isinstance(p.get("description"), str)
            and isinstance(p.get("parameters"), dict) and _is_str_list(p.get("calls"))
            and isinstance(p.get("mutating"), bool) and isinstance(p.get("program"), str)
            and (jp is None or isinstance(jp, str))):
        return None
    return {k: p[k] for k in ("name", "description", "parameters", "calls", "mutating",
                              "program")} | {"job_prompt": jp}


def _clean_job_prompt(p: dict) -> dict | None:
    if isinstance(p.get("prompt"), str) and _is_str_list(p.get("add_toolsets")):
        return {"prompt": p["prompt"], "add_toolsets": p["add_toolsets"]}
    return None


def _clean_config(p: dict) -> dict | None:
    if isinstance(p.get("key"), str) and "value" in p and _is_scalar(p["value"]):
        return {"key": p["key"], "value": p["value"]}
    return None


def _clean_skill(p: dict) -> dict | None:
    if isinstance(p.get("path"), str) and isinstance(p.get("text"), str):
        return {"path": p["path"], "text": p["text"]}
    return None


def _clean_new_reach(p: dict) -> dict | None:
    change, why = p.get("change"), p.get("why")
    if not isinstance(change, dict) or not isinstance(why, str):
        return None
    if change.get("type") == "callable_add" and isinstance(change.get("tool"), str):
        return {"change": {"type": "callable_add", "tool": change["tool"]}, "why": why}
    if (change.get("type") == "mcp_include_add" and isinstance(change.get("server"), str)
            and isinstance(change.get("tool"), str)):
        return {"change": {"type": "mcp_include_add", "server": change["server"],
                           "tool": change["tool"]}, "why": why}
    return None


_CLEANERS = {"composed_tool": _clean_composed, "job_prompt": _clean_job_prompt,
             "config": _clean_config, "skill": _clean_skill, "new_reach": _clean_new_reach}


def build_request(candidate: Candidate, job: dict | None, skill_text: str | None,
                  tool_schemas: dict[str, dict],
                  call_shape: list[tuple[str, tuple[str, ...]]]) -> tuple[str, str]:
    doc: dict[str, Any] = {"candidate": {f: getattr(candidate, f) for f in CANDIDATE_FIELDS}}
    if job is not None:
        doc["job"] = {f: job[f] for f in JOB_FIELDS if f in job}
    if skill_text is not None:
        doc["skill_text"] = skill_text
    doc["tool_schemas"] = tool_schemas
    doc["call_shape"] = [[name, list(keys)] for name, keys in call_shape]
    return INSTRUCTIONS, json.dumps(doc, sort_keys=True)


def parse_draft(text: str, candidate: Candidate, now: float) -> Fix | None:
    """The reply must be exactly one JSON object. Unknown payload keys are dropped."""
    text = text.strip()
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if text[end:].strip() or not isinstance(obj, dict):
        return None
    kind, target, payload = obj.get("kind"), obj.get("target"), obj.get("payload")
    if kind not in _CLEANERS or not isinstance(target, str) or not isinstance(payload, dict):
        return None
    clean = _CLEANERS[kind](payload)
    if clean is None:
        return None
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    digest = hashlib.sha256((candidate.key + json.dumps(clean, sort_keys=True)).encode()).hexdigest()
    return Fix(id=f"{day}-{digest[:8]}", kind=kind, target=target, payload=clean,
               candidate_key=candidate.key)


def _spend(reply: dict) -> float:
    cost = reply.get("estimated_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
        return float(cost)
    tokens = (reply.get("input_tokens") or 0) + (reply.get("output_tokens") or 0)
    return tokens * policy.FALLBACK_USD_PER_MTOK / 1e6


def draft(candidate: Candidate, job: dict | None, skill_text: str | None,
          tool_schemas: dict[str, dict], call_shape: list[tuple[str, tuple[str, ...]]],
          bridge: Bridge, now: float, max_tokens: int = 4000) -> tuple[Fix | None, float]:
    """One model call. A BridgeError propagates; spend is reported for bad replies too."""
    instructions, user_input = build_request(candidate, job, skill_text, tool_schemas, call_shape)
    reply = bridge.oneshot(instructions, user_input, max_tokens)
    text = reply.get("text")
    fix = parse_draft(text, candidate, now) if isinstance(text, str) else None
    return fix, _spend(reply)
