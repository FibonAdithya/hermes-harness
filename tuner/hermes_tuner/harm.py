"""Harm signals: does a run's own tool results show a fix hurt it?

Pure functions over a run's (tool name, result text) pairs. Callers build
`RunEvidence` and `Baseline`; nothing here touches the bridge or the ledger.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from statistics import mean

from . import history, policy

SIGNALS = ("unknown_tool", "server_tripped", "self_inflicted_errors", "run_failed")
SELF_ERR_MIN = 3
SELF_ERR_FACTOR = 2.0
SELF_ERR_MARGIN = 0.05
OUTAGE_WINDOW_S = 6 * 3600

LOOP_SUFFIX = re.compile(
    r"\n\n\[Tool loop (?:warning|hard stop): [a-z_]+; count=\d+; [^\]\n]*\]\Z")
_UNREACHABLE = re.compile(r"^MCP server '([^']+)' is unreachable after")
_UNKNOWN = (
    re.compile(r"^'([^']+)' is not a deferrable tool\."),
    re.compile(r"^Unknown tool: (\S+)$"),
    re.compile(r"^(\S+) is no longer available$"),
)
_UPSTREAM = re.compile(r"^Error calling tool '[^']*': API error in ")
_TOOL_ERROR = (
    re.compile(r"^Error calling tool '"),
    re.compile(r"^\d+ validation errors? for call\["),
    re.compile(r"^tool_call to '[^']+' is missing required argument"),
)


@dataclass(frozen=True)
class RunEvidence:
    results: tuple[tuple[str, str], ...]
    status: str | None = None
    delivery_failed: bool = False
    fix_kind: str = ""
    tools: frozenset[str] = frozenset()
    fix_text: str = ""


@dataclass(frozen=True)
class Baseline:
    error_rates: tuple[float, ...] = ()
    outage: bool = False


def classify(name: str, text: str) -> tuple[str, str] | None:
    body = history._unwrap(text, frozenset({name})).strip()
    body = LOOP_SUFFIX.sub("", body, count=1).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return None
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, str):
        return None
    if m := _UNREACHABLE.match(err):
        return "server_unreachable", m.group(1)
    for pat in _UNKNOWN:
        if m := pat.match(err):
            return "unknown_tool", m.group(1)
    if _UPSTREAM.match(err):
        return "upstream_error", name
    if any(p.match(err) for p in _TOOL_ERROR):
        return "tool_error", name
    return None


def error_rate(results: Iterable[tuple[str, str]]) -> float:
    classes = [c[0] if (c := classify(n, t)) else None for n, t in results]
    return classes.count("tool_error") / len(classes) if classes else 0.0


def names_tool(fix_text: str, tool: str) -> bool:
    for cand in {tool, tool.split("__")[-1]}:
        if cand and re.search(rf"(?<!\w){re.escape(cand)}(?!\w)", fix_text):
            return True
    return False


def _server(name: str) -> str | None:
    parts = name.split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else None


def _unknown_tool(run: RunEvidence, base: Baseline) -> bool:
    for name, text in run.results:
        c = classify(name, text)
        if c and c[0] == "unknown_tool" and c[1] not in run.tools and names_tool(run.fix_text, c[1]):
            return True
    return False


def _server_tripped(run: RunEvidence, base: Baseline) -> bool:
    erred: set[str] = set()
    for name, text in run.results:
        c = classify(name, text)
        if not c:
            continue
        if c[0] == "tool_error" and (srv := _server(name)):
            erred.add(srv)
        elif c[0] == "server_unreachable" and c[1] in erred:
            return True
    return False


def _self_inflicted_errors(run: RunEvidence, base: Baseline) -> bool:
    errors = sum(1 for n, t in run.results if (c := classify(n, t)) and c[0] == "tool_error")
    if errors < SELF_ERR_MIN:
        return False
    limit = SELF_ERR_FACTOR * mean(base.error_rates or (0.0,)) + SELF_ERR_MARGIN
    return errors / len(run.results) > limit


def _run_failed(run: RunEvidence, base: Baseline) -> bool:
    return (run.status == "error" or run.delivery_failed) and not base.outage


CHECKS: dict[str, Callable[[RunEvidence, Baseline], bool]] = {
    "unknown_tool": _unknown_tool,
    "server_tripped": _server_tripped,
    "self_inflicted_errors": _self_inflicted_errors,
    "run_failed": _run_failed,
}


def signals(run: RunEvidence, base: Baseline) -> list[str]:
    if not policy.HARM_CHECKS:
        return []
    return [s for s in SIGNALS if CHECKS[s](run, base)]
