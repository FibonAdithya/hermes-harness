"""Deterministic detector for repeated wasted work in Hermes's session history.

Works from tool names, argument keys, hashed or raw refs, counts and sizes only.
No model call, no randomness, no clock reads (`now` is a parameter).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .measure import priced_cost
from .model import Session

DAY = 86400.0
WINDOW_DAYS = 28
MONTH_DAYS = 30
NGRAM_SIZES = (3, 4, 5)
MIN_SEQUENCE_SESSIONS = 10
MIN_SCHEMA_SESSIONS = 20
MIN_SKILL_CHARS = 200_000
GMAIL_GET = "mcp__google__get_gmail_message_content"
SCHEMA_TOOL = "tool_describe"
SKILL_TOOL = "skill_view"


@dataclass(frozen=True)
class Candidate:
    kind: Literal["sequence", "reread", "schema_lookup", "skill_load"]
    key: str
    job_id: str | None
    sessions: int
    occurrences: int
    result_chars: int
    est_monthly_usd: float


def _total_chars(s: Session) -> int:
    return sum(c.result_chars for c in s.calls)


def _money(per_session: Mapping[str, int], by_id: Mapping[str, Session]) -> float:
    """Sum of priced_cost x (candidate chars / all chars) over sessions, scaled to
    30 days. Priced from tokens: the raw billing figure reads $0 on a subscription."""
    usd = 0.0
    for sid, chars in per_session.items():
        total = _total_chars(by_id[sid])
        if total > 0:
            usd += priced_cost(by_id[sid]) * chars / total
    return usd * MONTH_DAYS / WINDOW_DAYS


def _sequences(sessions: list[Session], by_id: Mapping[str, Session]) -> list[Candidate]:
    # (job, gram) -> {session id: (covered chars, matches)}
    seen: dict[tuple[str | None, tuple[str, ...]], dict[str, tuple[int, int]]] = defaultdict(dict)
    for s in sessions:
        names = [c.name for c in s.calls]
        for n in NGRAM_SIZES:
            covered: dict[tuple[str, ...], set[int]] = defaultdict(set)
            matches: dict[tuple[str, ...], int] = defaultdict(int)
            for i in range(len(names) - n + 1):
                g = tuple(names[i:i + n])
                matches[g] += 1
                covered[g].update(range(i, i + n))
            for g, idx in covered.items():
                chars = sum(s.calls[i].result_chars for i in idx)
                seen[(s.job_id, g)][s.id] = (chars, matches[g])

    best: dict[tuple[str | None, frozenset[str]], tuple[tuple, tuple[str | None, tuple[str, ...]]]] = {}
    for (job, g), per in seen.items():
        if len(per) < MIN_SEQUENCE_SESSIONS:
            continue
        total = sum(c for c, _m in per.values())
        rank = (-len(g), -total, "seq:" + ">".join(g))  # longest, most chars, smallest key
        group = (job, frozenset(per))
        if group not in best or rank < best[group][0]:
            best[group] = (rank, (job, g))

    out = []
    for _rank, (job, g) in best.values():
        per = seen[(job, g)]
        out.append(Candidate(
            kind="sequence", key="seq:" + ">".join(g), job_id=job, sessions=len(per),
            occurrences=sum(m for _c, m in per.values()),
            result_chars=sum(c for c, _m in per.values()),
            est_monthly_usd=_money({sid: c for sid, (c, _m) in per.items()}, by_id)))
    return out


def _rereads(sessions: list[Session], by_id: Mapping[str, Session]) -> list[Candidate]:
    jobs: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        if s.job_id is not None:
            jobs[s.job_id].append(s)
    out = []
    for job, runs in jobs.items():
        runs.sort(key=lambda s: (s.started_at, s.id))
        # first read's size per ref, per run
        reads = []
        for s in runs:
            m: dict[str, int] = {}
            for c in s.calls:
                if c.name == GMAIL_GET and c.ref is not None and c.ref not in m:
                    m[c.ref] = c.result_chars
            reads.append(m)
        per_session: dict[str, int] = {}
        occurrences = 0
        for i in range(1, len(runs)):
            repeats = [ch for ref, ch in reads[i].items() if ref in reads[i - 1]]
            if repeats:
                occurrences += len(repeats)
                per_session[runs[i].id] = sum(repeats)
        if occurrences >= 1:
            out.append(Candidate(
                kind="reread", key="reread:" + job, job_id=job, sessions=len(per_session),
                occurrences=occurrences, result_chars=sum(per_session.values()),
                est_monthly_usd=_money(per_session, by_id)))
    return out


def _schema_lookups(sessions: list[Session], by_id: Mapping[str, Session]) -> list[Candidate]:
    per_session = {}
    for s in sessions:
        d = [c for c in s.calls if c.name == SCHEMA_TOOL]
        if len(d) >= 2:
            per_session[s.id] = sum(c.result_chars for c in d)
    if len(per_session) < MIN_SCHEMA_SESSIONS:
        return []
    return [Candidate(
        kind="schema_lookup", key="schema_lookup", job_id=None, sessions=len(per_session),
        occurrences=sum(1 for s in sessions for c in s.calls if c.name == SCHEMA_TOOL),
        result_chars=sum(per_session.values()), est_monthly_usd=_money(per_session, by_id))]


def _skill_loads(sessions: list[Session], by_id: Mapping[str, Session],
                 skill_names: Mapping[str, Sequence[str]]) -> list[Candidate]:
    loads: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for s in sessions:
        views = [c for c in s.calls if c.name == SKILL_TOOL]
        names = skill_names.get(s.id)
        if names is None or len(names) != len(views):
            continue  # missing or misaligned names: skip rather than misattribute chars
        for c, name in zip(views, names):
            loads[name][s.id].append(c.result_chars)
    out = []
    for name, per in loads.items():
        n = sum(len(v) for v in per.values())
        total = sum(sum(v) for v in per.values())
        if total >= MIN_SKILL_CHARS:  # loads x average chars == total chars
            out.append(Candidate(
                kind="skill_load", key="skill_load:" + name, job_id=None, sessions=len(per),
                occurrences=n, result_chars=total,
                est_monthly_usd=_money({sid: sum(v) for sid, v in per.items()}, by_id)))
    return out


def detect(sessions: list[Session], now: float,
           skill_names: Mapping[str, Sequence[str]] | None = None) -> list[Candidate]:
    window = [s for s in sessions if now - WINDOW_DAYS * DAY <= s.started_at <= now]
    by_id = {s.id: s for s in window}
    cands = _sequences(window, by_id) + _rereads(window, by_id) + _schema_lookups(window, by_id)
    if skill_names is not None:
        cands += _skill_loads(window, by_id, skill_names)
    cands = [c for c in cands if c.est_monthly_usd > 0]  # worth nothing: not a candidate
    cands.sort(key=lambda c: (-c.est_monthly_usd, c.kind, c.key, c.job_id or ""))
    return cands
