"""Replay of recorded runs: which result strings the model reused verbatim.

Text read here stays inside the tuner process; callers report counts and booleans.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from . import policy
from .detect import carried_usd
from .history import ReplayCall, ReplayRun, _key_ref
from .model import Pricing
from .reducers import _URL_RE, apply_reducer, text_view
from .trial import call_failed


def norm(text: str) -> str:
    """Lowercase; every whitespace run becomes one space; no leading/trailing space."""
    return " ".join(text.lower().split())


def _automaton(a: str) -> tuple[list[dict[str, int]], list[int], list[int]]:
    """Suffix automaton of `a`: transitions per state, plus link and len lists."""
    nxt: list[dict[str, int]] = [{}]
    link = [-1]
    length = [0]
    last = 0
    for ch in a:
        cur = len(nxt)
        nxt.append({})
        link.append(-1)
        length.append(length[last] + 1)
        p = last
        while p != -1 and ch not in nxt[p]:
            nxt[p][ch] = cur
            p = link[p]
        if p == -1:
            link[cur] = 0
        else:
            q = nxt[p][ch]
            if length[p] + 1 == length[q]:
                link[cur] = q
            else:
                clone = len(nxt)
                nxt.append(dict(nxt[q]))
                link.append(link[q])
                length.append(length[p] + 1)
                while p != -1 and nxt[p].get(ch) == q:
                    nxt[p][ch] = clone
                    p = link[p]
                link[q] = clone
                link[cur] = clone
        last = cur
    return nxt, link, length


def _maximal_matches(sam, b: str, min_len: int):
    """Substrings of `b` (len >= min_len) in the automaton's text, maximal within `b`."""
    nxt, link, length = sam
    state, cur, prev = 0, 0, 0
    for k, ch in enumerate(b):
        while state != 0 and ch not in nxt[state]:
            state = link[state]
            cur = length[state]
        if ch in nxt[state]:
            state = nxt[state][ch]
            cur += 1
        else:
            cur = 0
        # The match of length `prev` ending at k-1 was not extended at k.
        if cur != prev + 1 and prev >= min_len:
            yield b[k - prev:k]
        prev = cur
    if prev >= min_len:
        yield b[len(b) - prev:]


def used_strings(result: str, later: Sequence[str], exclude: Sequence[str]) -> frozenset[str]:
    """Strings of `result` the model reused verbatim in `later` messages.

    For each normalised later message, the maximal substrings that occur in the
    normalised text view of `result`, stripped of edge spaces, of length >=
    USED_MIN_CHARS after stripping. Strings found
    in any normalised `exclude` text (the prompt) are dropped. Only the first
    USED_MAX_CHARS normalised chars of the result and of each message are compared.
    """
    cap = policy.USED_MAX_CHARS
    sam = _automaton(norm(text_view(result))[:cap])
    found: set[str] = set()
    for msg in later:
        for m in _maximal_matches(sam, norm(msg)[:cap], policy.USED_MIN_CHARS):
            # A match can start or end with a space; the reduced text need not keep it.
            m = m.strip()
            if len(m) >= policy.USED_MIN_CHARS:
                found.add(m)
    excl = [norm(e) for e in exclude]
    return frozenset(s for s in found if not any(s in e for e in excl))


def survives(s: str, reduced: str) -> bool:
    """True when used string `s` is still in the reduced result's text view."""
    return s in norm(text_view(reduced))


def boilerplate(used: Iterable[str], results: Sequence[str]) -> frozenset[str]:
    """Strings of `used` present in more than BOILERPLATE_SHARE of a tool's results.

    Presence is a substring of the result's normalised text view. With fewer than
    BOILERPLATE_MIN_RESULTS results nothing is boilerplate.
    """
    if len(results) < policy.BOILERPLATE_MIN_RESULTS:
        return frozenset()
    views = [norm(text_view(r)) for r in results]
    limit = policy.BOILERPLATE_SHARE * len(results)
    return frozenset(s for s in set(used) if sum(s in v for v in views) > limit)


@dataclass(frozen=True)
class ReducerResult:
    spec: dict | None
    reason: str          # "ok" | "no_runs" | "too_few_sessions" | "cut_below_min" | "used_lost"
    sessions: int        # runs with at least one result for the tool
    results: int
    bytes_before: int
    bytes_after: int
    used_total: int      # (result, used string) pairs that must survive
    used_kept: int       # of used_total, still in the same result under the final steps
    est_saving_per_run: float   # carried_usd(before) - carried_usd(after), summed, / sessions


_SCHEMELESS_RE = re.compile(r"^((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]*[a-z][a-z0-9-]*)/")


def _used_hosts(used: Iterable[str]) -> list[str]:
    """Lowercased hostnames of URLs in used strings, sorted.

    Also takes `host/path` tokens with no scheme when the host is a dotted name.
    """
    hosts: set[str] = set()
    for s in used:
        for m in _URL_RE.finditer(s):
            try:
                h = urlsplit(m.group(0)).hostname
            except ValueError:
                h = None
            if h:
                hosts.add(h.lower())
        for tok in s.split():
            if "://" in tok:
                continue
            m = _SCHEMELESS_RE.match(tok.lower())
            if m:
                hosts.add(m.group(1))
    return sorted(hosts)


def _top_keys(texts: Sequence[str]) -> list[str]:
    keys: set[str] = set()
    for t in texts:
        try:
            obj = json.loads(t)
        except ValueError:
            continue
        if isinstance(obj, dict):
            keys.update(obj)
    return sorted(keys)


def _with(steps: list[dict], cand: dict) -> list[dict]:
    """`steps` plus `cand`; a drop_fields candidate merges into an existing one."""
    if cand["op"] == "drop_fields":
        for i, st in enumerate(steps):
            if st["op"] == "drop_fields":
                merged = {"op": "drop_fields", "fields": sorted(set(st["fields"]) | set(cand["fields"]))}
                return steps[:i] + [merged] + steps[i + 1:]
    return steps + [cand]


def _evaluate(steps: list[dict], texts: Sequence[str], used: Sequence[frozenset[str]]):
    """Total reduced bytes and reduced texts, or None if any used string is lost."""
    spec = {"steps": steps}
    out: list[str] = []
    for text, need in zip(texts, used):
        red = apply_reducer(spec, text)
        if need:
            view = norm(text_view(red))      # survives(), with the view computed once
            if not all(s in view for s in need):
                return None
        out.append(red)
    return sum(len(t) for t in out), out


def search_reducer(runs: Sequence[ReplayRun], tool: str, job: str, exclude: Sequence[str],
                   pricing: Pricing, *,
                   final_steps: Callable[[list[dict]], list[dict]] | None = None
                   ) -> ReducerResult:
    """Greedy search for the reducer that cuts most bytes of `tool`'s results.

    Every used string (minus boilerplate) of every replayed result must survive.
    After the search, the final steps are applied again to each result and the used
    strings still in that same result are counted (used_kept); the spec is kept only
    when that is every one (else reason "used_lost"). `final_steps` (tests only) maps
    the searched steps to the stored ones. Reads text in-process only; the result
    holds a spec of op names, field names and hosts, plus counts.
    """
    calls: list[ReplayCall] = []
    used: list[frozenset[str]] = []
    sessions = 0
    for run in runs:
        mine = [c for c in run.calls if c.name == tool]
        if mine:
            sessions += 1
        for c in mine:
            calls.append(c)
            used.append(used_strings(c.text, [t for p, t in run.said if p > c.pos], exclude))
    texts = [c.text for c in calls]
    before = sum(len(t) for t in texts)
    if not calls:
        return ReducerResult(None, "no_runs", 0, 0, 0, 0, 0, 0, 0.0)

    bp = boilerplate(set().union(*used), texts)
    used = [u - bp for u in used]
    used_total = sum(len(u) for u in used)

    cands: list[dict] = [{"op": "drop_fields", "fields": [k]} for k in _top_keys(texts)]
    cands += [{"op": "html_to_text"},
              {"op": "shorten_urls", "keep_hosts": _used_hosts(set().union(*used))},
              {"op": "drop_quoted"}, {"op": "drop_footer"}, {"op": "collapse_ws"}]
    cands += [{"op": "cap", "chars": n} for n in policy.REDUCER_CAP_LADDER]

    steps: list[dict] = []
    total, reduced = before, list(texts)
    step_min = policy.REDUCER_STEP_MIN * before
    while True:
        best = None
        for cand in cands:
            got = _evaluate(_with(steps, cand), texts, used)
            if got is not None and (best is None or got[0] < best[1][0]):
                best = (cand, got)
        if best is None or total - best[1][0] < step_min:
            break
        cand, (total, reduced) = best
        steps = _with(steps, cand)
        if cand["op"] == "drop_fields":
            cands = [c for c in cands if c is not cand]
        else:
            cands = [c for c in cands if c["op"] != cand["op"]]

    if final_steps is not None:
        steps = final_steps(list(steps))
    # Recount on the stored steps, independently of the search's own checks.
    reduced = [apply_reducer({"steps": steps}, t) for t in texts]
    total = sum(len(r) for r in reduced)
    used_kept = sum(sum(1 for u in need if survives(u, red))
                    for need, red in zip(used, reduced))
    saving = sum(carried_usd(c.chars, c.later_turns, pricing)
                 - carried_usd(len(r), c.later_turns, pricing) for c, r in zip(calls, reduced))
    if sessions < policy.REPLAY_MIN_SESSIONS:
        reason = "too_few_sessions"
    elif before == 0 or (before - total) < policy.REDUCER_MIN_CUT * before:
        reason = "cut_below_min"
    elif used_kept != used_total:
        reason = "used_lost"
    else:
        reason = "ok"
    spec = {"tool": tool, "job": job, "steps": steps} if reason == "ok" else None
    return ReducerResult(spec, reason, sessions, len(calls), before, total,
                         used_total, used_kept, saving / sessions)


# --- composed-tool replay ------------------------------------------------------------

# Error messages relay.run_program returns when a run hits a limit (relay.py: the
# output cap, the call limit, the deadline).
LIMIT_PREFIXES = ("output exceeds", "tool call limit", "program timed out")


@dataclass(frozen=True)
class ComposedReplay:
    sessions: int        # runs replayed (a stub miss skips a run; it is not counted)
    limit_hits: int
    errors: int          # failed outputs that are not limit hits (trial.call_failed)
    in_bytes: int        # UTF-8 bytes of the replaced calls' recorded results
    out_bytes: int       # UTF-8 bytes of the program outputs
    reason: str          # "ok" | "limit_hit" | "program_error" | "too_few_sessions" | "no_time"
    eligible: int = 0    # runs given with at least one call to a tool in payload calls
    saving_per_run: float = 0.0   # mean carried-bytes saving over replayed runs, USD
    out_of_time: bool = False     # the time budget stopped a run from starting
    elapsed_s: float = 0.0        # by the injected clock


def limit_hit(out: str) -> bool:
    """True when `out` is relay's {"error": ...} for a limit."""
    try:
        obj = json.loads(out)
    except ValueError:
        return False
    msg = obj.get("error") if isinstance(obj, dict) else None
    return isinstance(msg, str) and msg.startswith(LIMIT_PREFIXES)


class _Miss(LookupError):
    pass


class _Stub:
    """Recorded results by (name, exact args), else by (name, ref). Relay hands any
    exception to the program as an error result; `missed` tells the replay.
    `served` holds each recorded call returned at least once, by position."""

    def __init__(self, calls: Sequence[ReplayCall]):
        self.exact: dict[tuple[str, str], ReplayCall] = {}
        self.by_ref: dict[tuple[str, str], ReplayCall] = {}
        self.missed = False
        self.served: dict[int, ReplayCall] = {}
        for c in calls:
            self.exact.setdefault((c.name, json.dumps(c.args, sort_keys=True, default=str)), c)
            if c.ref is not None:
                self.by_ref.setdefault((c.name, c.ref), c)

    def __call__(self, name: str, args: dict) -> str:
        hit = self.exact.get((name, json.dumps(args, sort_keys=True, default=str)))
        if hit is None:
            ref = _key_ref(args)
            hit = self.by_ref.get((name, ref)) if ref is not None else None
        if hit is None:
            self.missed = True
            raise _Miss("no recorded result")
        self.served.setdefault(hit.pos, hit)
        return hit.text


def _replay_args(payload: dict, calls: Sequence[ReplayCall]) -> dict | None:
    """The tool's input for one run, from all its replaced calls; None when any input
    collects no value. A "list" input keeps each distinct value once, in order."""
    out: dict = {}
    for e in payload["replay_inputs"]:
        vals = [c.args[e["arg"]] for c in calls if c.name == e["from_call"] and e["arg"] in c.args]
        if not vals:
            return None
        if e["collect"] == "first":
            out[e["param"]] = vals[0]
            continue
        seen: set[str] = set()
        uniq = []
        for v in vals:
            k = json.dumps(v, sort_keys=True, default=str)
            if k not in seen:
                seen.add(k)
                uniq.append(v)
        out[e["param"]] = uniq
    props = payload.get("parameters", {}).get("properties", {})
    if isinstance(props, dict) and "dry_run" in props:
        out["dry_run"] = True
    return out


def _intent_served(replaced: Sequence[ReplayCall], intents: list) -> dict[int, ReplayCall]:
    """Replaced calls a dry-run intent stands for: the relay records a mutating call as
    an intent and never forwards it to the stub. An intent serves the first replaced
    call of the same tool and the same ref (message_id, else query), by position."""
    by_ref: dict[tuple[str, str], ReplayCall] = {}
    for c in replaced:
        if c.ref is not None:
            by_ref.setdefault((c.name, c.ref), c)
    out: dict[int, ReplayCall] = {}
    for it in intents:
        tool = it.get("tool") if isinstance(it, dict) else None
        args = it.get("args") if isinstance(it, dict) else None
        ref = _key_ref(args) if isinstance(args, dict) else None
        hit = by_ref.get((tool, ref)) if isinstance(tool, str) and ref is not None else None
        if hit is not None:
            out.setdefault(hit.pos, hit)
    return out


def replay_composed(payload: dict, runs: Sequence[ReplayRun],
                    run_program: Callable[..., str], *, pricing: Pricing,
                    argv_for: Callable[[str], list[str]],
                    clock: Callable[[], float] | None = None,
                    budget_s: float | None = None) -> ComposedReplay:
    """Replay a composed tool (payload already passed limits.check_fix) in the sandbox
    on the newest REPLAY_MAX_SESSIONS replayable runs, each fed every replaced call of
    its run: the worst case. Stops at the first limit hit. `run_program` has
    relay.run_program's signature; `argv_for(container_name)` gives the runner argv.
    A run starts only while REPLAY_TIMEOUT_S + 30 s fit in `budget_s` (seconds left,
    measured by `clock`; no limit when either is None). A run counts as a session only
    if a call was served and no lookup missed, unless it hit a limit. A mutating call
    is served by a dry-run intent for the same tool and ref (_intent_served). Its saving
    credits only the served calls. Result text stays in this process and the sandbox;
    the result holds counts."""
    tools = frozenset(payload["calls"])
    eligible = [r for r in runs if any(c.name in tools for c in r.calls)]
    picked = []
    for run in sorted(eligible, key=lambda r: (r.started_at, r.session_id), reverse=True):
        replaced = [c for c in run.calls if c.name in tools]
        args = _replay_args(payload, replaced)
        if args is not None:
            picked.append((replaced, args))
        if len(picked) == policy.REPLAY_MAX_SESSIONS:
            break
    sessions = hits = errors = in_bytes = out_bytes = 0
    saving = 0.0
    out_of_time = False
    start = clock() if clock is not None else 0.0
    for replaced, args in picked:
        if clock is not None and budget_s is not None \
                and budget_s - (clock() - start) < policy.REPLAY_TIMEOUT_S + 30:
            out_of_time = True
            break
        stub = _Stub(replaced)
        intents: list[dict] = []
        name = f"tuner-replay-{uuid.uuid4().hex[:8]}"
        out = run_program(payload["program"], args, call=stub, runner_argv=argv_for(name),
                          dry_run=True, intents=intents, timeout_s=policy.REPLAY_TIMEOUT_S,
                          max_calls=policy.MAX_CALLS, max_output=policy.MAX_OUTPUT_BYTES,
                          allowed=tools, container_name=name)
        by_pos = dict(stub.served) | _intent_served(replaced, intents)
        hit = limit_hit(out)
        if not hit and (stub.missed or not by_pos):
            continue
        served = [by_pos[p] for p in sorted(by_pos)]
        first = served[0] if served else replaced[0]
        sessions += 1
        in_bytes += sum(len(c.text.encode()) for c in served)
        out_bytes += len(out.encode())
        saving += (sum(carried_usd(c.chars, c.later_turns, pricing) for c in served)
                   - carried_usd(len(out), first.later_turns, pricing))
        if hit:
            hits += 1
            break
        if call_failed(out):
            errors += 1
    if hits:
        reason = "limit_hit"
    elif errors:
        reason = "program_error"
    elif sessions < policy.REPLAY_MIN_SESSIONS:
        reason = "no_time" if out_of_time else "too_few_sessions"
    else:
        reason = "ok"
    elapsed = clock() - start if clock is not None else 0.0
    return ComposedReplay(sessions, hits, errors, in_bytes, out_bytes, reason, len(eligible),
                          saving / sessions if sessions else 0.0, out_of_time, elapsed)


def composed_monthly(rep: ComposedReplay, turns_usd: float) -> float:
    """USD per month: the mean carried-bytes saving per replayed run, times the runs in
    the REPLAY_WINDOW_DAYS window that call a replaced tool, scaled to 30 days, plus
    `turns_usd`, the candidate's turns-removed estimate (already monthly)."""
    return rep.saving_per_run * rep.eligible * 30 / policy.REPLAY_WINDOW_DAYS + turns_usd


def composed_gate(rep: ComposedReplay, monthly: float) -> str | None:
    """The rejection reason (counts only), or None when the draft may go to shadow."""
    m = rep.sessions
    if rep.limit_hits > 0:
        return f"replay: limit hit in {rep.limit_hits} of {m} sessions"
    if rep.errors > 0:
        return f"replay: program error in {rep.errors} of {m} sessions"
    if m < policy.REPLAY_MIN_SESSIONS:
        return "replay: no time left" if rep.out_of_time else f"replay: too few sessions ({m})"
    if monthly <= policy.MIN_DRAFT_MONTHLY_USD:
        return "replay: saving below floor"
    return None
