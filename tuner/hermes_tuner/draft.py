"""Drafting: build the model request, parse the reply into a Fix (spec 4.3).

The request carries statistics, the job prompt, skill text, tool schemas and the
call sequence by tool name and argument KEYS only. No email text, tool result or
argument value reaches the model. The parser is strict and applies no policy;
`limits.check_fix` does that.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

from . import policy
from .bridge import Bridge
from .detect import Candidate
from .measure import priced_breakdown
from .model import Fix, Pricing, ToolCall

KINDS = ("composed_tool", "job_prompt", "config", "skill", "new_reach")
JOB_FIELDS = ("id", "name", "schedule", "prompt", "enabled_toolsets")
CANDIDATE_FIELDS = ("kind", "key", "job_id", "sessions", "occurrences", "result_chars",
                    "est_monthly_usd")

INSTRUCTIONS = f"""You draft one fix that reduces wasted work in a scheduled agent. The user message is a JSON document: statistics for one detected pattern (candidate), the job it occurs in (job), optionally the text of a skill (skill_text), the schemas of tools involved (tool_schemas), the settings a config fix may change with their current values (settings), and call_shape, the sequence of tool calls as [tool name, [argument keys]], and turn_layout. It contains no tool results and no argument values.

Reply with a single JSON object and nothing else: no prose, no code fence. Shape: {{"kind": ..., "target": ..., "payload": {{...}}}}. Choose exactly one kind.

1. "composed_tool": one new tool that runs the observed call sequence in a single step. target is the job id from the request. payload: {{"name": str, "description": str, "parameters": object (JSON schema), "calls": [str], "mutating": bool, "program": str, "job_prompt": str or null, "replay_inputs": [{{"param": str, "from_call": str, "arg": str, "collect": "list" or "first"}}]}}.
   - name must start with "{policy.TOOL_PREFIX}". The tool is placed in the "{policy.TOOLSET}" toolset.
   - description says what the tool does, derived from its structure (which tools it calls and what it returns), not from any email content.
   - program is Python source defining `def main(args: dict, call) -> object`. `call(name, args)` runs a tool and returns its result parsed from JSON (or the raw string if it is not JSON). The return value must be JSON-serialisable. The program runs in a sandbox with no network, so every external effect goes through call().
   - calls must list every tool the program calls. The run is refused any other tool. Composed tools may not call composed tools.
   - Allowed tools: {", ".join(sorted(policy.CALLABLE_TOOLS))}.
   - web_search and web_extract are refused after any other call in the same run, so call them first or not at all.
   - If the program calls a mutating tool ({", ".join(sorted(policy.MUTATING_TOOLS & policy.CALLABLE_TOOLS))}), set mutating to true and add a boolean "dry_run" argument to parameters; the program must not perform effects when it is set (calls are recorded instead).
   - job_prompt is optional new job prompt text telling the agent to use the tool, or null.
   - The program's JSON output must stay under {policy.MAX_OUTPUT_BYTES} bytes, at most {policy.MAX_CALLS} tool calls and {policy.RUN_TIMEOUT_S} seconds; return only what the job needs, not whole tool results.
   - turn_layout lists how many of the observed calls the agent already sends per turn; merging calls that already share a turn saves nothing.
   - replay_inputs says how the tool's arguments are built from the observed calls, so the tool can be tested on recorded runs before it is used. Each entry fills the parameter named param (a key of parameters.properties) from argument arg of the observed calls to from_call (one of calls): "list" collects every value, "first" takes the first. List at least one entry.
2. "job_prompt": rewrite a job's prompt. target is the job id from the request. payload: {{"prompt": str, "add_toolsets": [str]}}. add_toolsets may only be ["{policy.TOOLSET}"] or [].
3. "config": change one setting. target is the key. payload: {{"key": str, "value": string, number or boolean}}. The request's "settings" object maps every setting you may change to its current value. key must be one of its keys, copied exactly; any other key is refused. Keep the type of the current value. If "settings" is empty, do not choose this kind.
4. "skill": shrink a skill that is loaded often and is large. target is the skill path. payload: {{"path": str, "text": str}}. A skill fix may only rewrite an existing SKILL.md that is not bundled with the agent, and must keep its frontmatter block (the leading --- block) byte-identical. Shrink the body, keep the decision guidance, and point at the tool for mechanics.
5. "new_reach": use only when the fix truly needs a tool outside the allowed list. payload: {{"change": {{"type": "mcp_include_add", "server": str, "tool": str}} or {{"type": "callable_add", "tool": str}}, "why": str}}. target is the tool name. The owner must approve it.

history lists earlier drafts for this candidate and why they failed; fields named for a copy rejection must not contain literal text from tool results (no label names, addresses or subjects).

job_prompt and composed_tool fixes target only the job in the request. Do not include keys that are not listed. Do not include secrets, message ids or any text other than your own design.
"""


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool))


REPLAY_INPUT_KEYS = ("param", "from_call", "arg", "collect")


def _clean_composed(p: dict) -> dict | None:
    """replay_inputs must be a list of objects; each keeps only REPLAY_INPUT_KEYS.
    Their values are checked by limits.check_fix."""
    jp, ri = p.get("job_prompt"), p.get("replay_inputs")
    if not (isinstance(p.get("name"), str) and isinstance(p.get("description"), str)
            and isinstance(p.get("parameters"), dict) and _is_str_list(p.get("calls"))
            and isinstance(p.get("mutating"), bool) and isinstance(p.get("program"), str)
            and (jp is None or isinstance(jp, str))
            and isinstance(ri, list) and all(isinstance(e, dict) for e in ri)):
        return None
    return {k: p[k] for k in ("name", "description", "parameters", "calls", "mutating",
                              "program")} | {"job_prompt": jp, "replay_inputs": [
        {k: e[k] for k in REPLAY_INPUT_KEYS if k in e} for e in ri]}


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


# Tool names and argument keys come from the agent's tool calls, which its model
# writes, so mail can steer them. Only identifier-shaped strings reach the prompt.
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ARG_KEY = re.compile(r"[A-Za-z0-9_]{1,40}")
MAX_SHAPE_CALLS = 200
# Drafting history (backoff.draft_history): its reason classes, and its field paths as
# limits._strings builds them (payload keys joined by ".", list indices as [i], "<key>"
# for a dict key itself), or "?". Model-chosen keys could hold copied text, so only
# identifier-shaped segments pass.
FIELD_RE = re.compile(r"\?|[A-Za-z0-9_]{1,40}(?:\[\d{1,6}\])*"
                    r"(?:\.(?:[A-Za-z0-9_]{1,40}|<key>)(?:\[\d{1,6}\])*){0,8}")
REASON_CLASSES = frozenset({"copy", "replay_limit", "replay", "timeout", "unparseable",
                            "shadow", "limits", "trial", ""})
HISTORY_LEN = 3
MAX_HISTORY_FIELDS = 20


def _safe(value: Any, pattern: re.Pattern) -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else "?"


def _safe_key(key: str) -> str:
    """A candidate key ("seq:a>b", "skill_load:name") with each name made safe."""
    kind, sep, rest = key.partition(":")
    return kind + sep + ">".join(_safe(n, _NAME) for n in rest.split(">")) if sep else kind


def turn_layout(calls: Iterable[ToolCall]) -> list[int]:
    """Calls per model turn, in turn order. Calls whose turn is unknown (0) are left out."""
    counts: dict[int, int] = {}
    for c in calls:
        if c.turn > 0:
            counts[c.turn] = counts.get(c.turn, 0) + 1
    return [counts[t] for t in sorted(counts)]


def _history(history: list[dict] | None) -> list[dict]:
    """At most HISTORY_LEN entries of the four keys, every value identifier-shaped:
    a ledger reason string never reaches the model (backoff.draft_history)."""
    out = []
    for e in (history or [])[:HISTORY_LEN]:
        if not isinstance(e, dict):
            continue
        fields = e.get("fields")
        out.append({"kind": _safe(e.get("kind"), _NAME), "state": _safe(e.get("state"), _NAME),
                     "reason_class": e.get("reason_class") if e.get("reason_class") in
                     REASON_CLASSES else "?",
                     "fields": [_safe(f, FIELD_RE) for f in fields][:MAX_HISTORY_FIELDS]
                     if isinstance(fields, list) else []})
    return out


def build_request(candidate: Candidate, job: dict | None, skill_text: str | None,
                  tool_schemas: dict[str, dict],
                  call_shape: list[tuple[str, tuple[str, ...]]],
                  settings: dict[str, Any] | None = None,
                  turn_layout: list[int] | None = None,
                  history: list[dict] | None = None) -> tuple[str, str]:
    """`settings`: the changeable settings (limits.changeable_settings), sent so the
    model names a real key instead of guessing one under an allowed prefix.
    `turn_layout`: calls per turn of the call_shape session (positive ints only).
    `history`: up to 3 earlier outcomes for this candidate, newest first, each
    {"kind", "state", "reason_class", "fields"} (backoff.draft_history)."""
    doc: dict[str, Any] = {"candidate": {f: getattr(candidate, f) for f in CANDIDATE_FIELDS}}
    doc["candidate"]["key"] = _safe_key(candidate.key)
    if job is not None:
        doc["job"] = {f: job[f] for f in JOB_FIELDS if f in job}
    if skill_text is not None:
        doc["skill_text"] = skill_text
    doc["tool_schemas"] = tool_schemas
    doc["settings"] = dict(settings or {})
    doc["call_shape"] = [[_safe(name, _NAME), [_safe(k, _ARG_KEY) for k in keys]]
                         for name, keys in call_shape[:MAX_SHAPE_CALLS]]
    doc["turn_layout"] = [n for n in (turn_layout or [])
                          if isinstance(n, int) and not isinstance(n, bool) and n > 0
                          ][:MAX_SHAPE_CALLS]
    doc["history"] = _history(history)
    return INSTRUCTIONS, json.dumps(doc, sort_keys=True)


def parse_draft(text: str, candidate: Candidate, now: float) -> Fix | None:
    """The reply must be exactly one JSON object. Unknown payload keys are dropped.
    Any failure to parse (including RecursionError on deep nesting) gives None."""
    try:
        return _parse_draft(text, candidate, now)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return None


def _parse_draft(text: str, candidate: Candidate, now: float) -> Fix | None:
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
    # A global fix's target is what it changes, never the model's choice: a target
    # equal to a job id would make ledger.active_for(job) block that job.
    if kind == "config":
        target = clean["key"]
    elif kind == "skill":
        target = clean["path"]
    elif kind == "new_reach":
        target = clean["change"]["tool"]
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    digest = hashlib.sha256((candidate.key + json.dumps(clean, sort_keys=True)).encode()).hexdigest()
    return Fix(id=f"{day}-{digest[:8]}", kind=kind, target=target, payload=clean,
               candidate_key=candidate.key)


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _spend(reply: dict, pricing: Pricing) -> float:
    """The oneshot priced from its token breakdown at `pricing`'s list prices, like
    every run (measure.priced_breakdown); the reply's estimated_cost_usd is ignored."""
    tokens = [_count(reply.get(k)) for k in ("input_tokens", "cache_read_tokens",
                                             "cache_write_tokens", "output_tokens")]
    return priced_breakdown(tokens, pricing)


def draft(candidate: Candidate, job: dict | None, skill_text: str | None,
          tool_schemas: dict[str, dict], call_shape: list[tuple[str, tuple[str, ...]]],
          bridge: Bridge, now: float, pricing: Pricing,
          max_tokens: int = 4000,
          settings: dict[str, Any] | None = None,
          turn_layout: list[int] | None = None,
          history: list[dict] | None = None) -> tuple[Fix | None, float]:
    """One model call. A BridgeError propagates; spend is reported for bad replies too."""
    instructions, user_input = build_request(candidate, job, skill_text, tool_schemas,
                                             call_shape, settings, turn_layout, history)
    reply = bridge.oneshot(instructions, user_input, max_tokens)
    text = reply.get("text")
    fix = parse_draft(text, candidate, now) if isinstance(text, str) else None
    return fix, _spend(reply, pricing)
