"""CLI: python -m hermes_tuner run [--dry-run] | detect ... | scrub ... | pricing |
replay-check ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .bridge import Bridge, HermesBridge
from .cycle import CycleConfig, run_cycle
from .detect import DAY, detect
from .history import History, HistoryUnavailable
from .model import Pricing
from . import limits, policy, scrub
from .replay import (ReducerResult, composed_gate, composed_monthly, replay_composed,
                     search_reducer)

DEFAULT_HOME = "/home/hermes/.hermes"


def _date(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _replay_input(s: str) -> dict:
    """`<param>=<tool>.<arg>:<list|first>` -> a replay_inputs entry. Tool names hold
    no dot, so the split is on the last dot before the colon."""
    param, eq, rest = s.partition("=")
    src, colon, collect = rest.rpartition(":")
    tool, dot, arg = src.rpartition(".")
    if not (eq and colon and dot and param and tool and arg) or collect not in ("list", "first"):
        raise argparse.ArgumentTypeError("expected <param>=<tool>.<arg>:<list|first>")
    return {"param": param, "from_call": tool, "arg": arg, "collect": collect}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hermes_tuner")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one daily cycle")
    run.add_argument("--dry-run", action="store_true", help="apply nothing; print the report")
    det = sub.add_parser("detect", help="print the top 10 candidates as JSON lines")
    det.add_argument("--db", type=Path, help="Hermes's state.db (unless --fixture)")
    det.add_argument("--fixture", type=Path, help="a scrubbed fixture instead of the db")
    det.add_argument("--now", type=_date, help="YYYY-MM-DD (00:00 UTC) anchoring the window")
    det.add_argument("--pricing", type=Path,
                     help="a JSON price list (as `pricing` prints) instead of asking Hermes")
    scr = sub.add_parser("scrub", help="write a scrubbed fixture")
    scr.add_argument("--db", type=Path, required=True)
    scr.add_argument("--out", type=Path, required=True)
    scr.add_argument("--since", type=_date, default=0.0, help="YYYY-MM-DD")
    scr.add_argument("--until", type=_date, help="YYYY-MM-DD, exclusive")
    rc = sub.add_parser("replay-check",
                        help="replay the reducer search or a composed tool; print counts only")
    rc.add_argument("--job", required=True, help="cron job id")
    rc.add_argument("--tool", help="one of the reducer tools (default: all of them)")
    rc.add_argument("--composed", type=Path, help="a composed tool dir to replay instead")
    rc.add_argument("--replay-input", type=_replay_input, action="append", default=[],
                    metavar="PARAM=TOOL.ARG:list|first",
                    help="a replay_inputs entry (repeatable); replaces tool.json's")
    rc.add_argument("--db", type=Path, help="Hermes's state.db (default $HERMES_HOME/state.db)")
    rc.add_argument("--now", type=_date, help="YYYY-MM-DD (00:00 UTC) ending the window")
    rc.add_argument("--days", type=int, default=policy.REPLAY_WINDOW_DAYS,
                    help="window length in days")
    rc.add_argument("--pricing", type=Path,
                    help="a JSON price list (as `pricing` prints) instead of asking Hermes")
    sub.add_parser("pricing", help="print the configured model's price list (USD per million)")
    return p


def _load_tool(parser: argparse.ArgumentParser, d: Path, inputs: list[dict]) -> dict:
    """A composed tool dir as a replay payload: tool.json plus the program, with
    `inputs` (when given) as its replay_inputs. Errors name types and checks only."""
    try:
        spec = json.loads((d / "tool.json").read_text(encoding="utf-8"))
        program = (d / "program.py").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read tool dir: {type(exc).__name__}")
    if not isinstance(spec, dict) or not isinstance(spec.get("calls"), list):
        parser.error("tool.json is not a composed tool")
    payload = {**spec, "program": program}
    if inputs:
        payload["replay_inputs"] = inputs
    reasons = limits._check_replay_inputs(payload, payload["calls"])
    if reasons:
        parser.error("; ".join(reasons))
    return payload


def _reducer_line(tool: str, job: str, runs, exclude: list[str], pricing: Pricing,
                  search: Callable[..., ReducerResult]) -> dict:
    """One tool's search as counts: no spec params, field names, hosts or text."""
    try:
        r = search(runs, tool, job, exclude, pricing)
    except Exception as exc:  # noqa: BLE001 - the type only: a message could hold text
        return {"tool": tool, "reason": f"error:{type(exc).__name__}"}
    return {"tool": tool, "reason": r.reason, "sessions": r.sessions, "results": r.results,
            "bytes_before": r.bytes_before, "bytes_after": r.bytes_after,
            "cut": round(1 - r.bytes_after / r.bytes_before, 4) if r.bytes_before else 0.0,
            "used_total": r.used_total, "used_kept": r.used_kept,
            "est_saving_per_run": r.est_saving_per_run,
            "ops": [st["op"] for st in r.spec["steps"]] if r.spec is not None else []}


def _composed_line(payload: dict, runs, pricing: Pricing,
                   run_program: Callable[..., str] | None,
                   replay_argv: Callable[[str], list[str]] | None) -> dict:
    """The composed tool's replay as counts. The monthly estimate behind `gate` has no
    candidate, so it holds the carried-bytes saving only (no turns removed)."""
    name = payload.get("name") if isinstance(payload.get("name"), str) else None
    try:
        if run_program is None or replay_argv is None:
            from .cycle_draft import load_relay
            relay = load_relay()
            run_program = run_program or relay.run_program
            replay_argv = replay_argv or relay.production_argv
        rep = replay_composed(payload, runs, run_program, pricing=pricing, argv_for=replay_argv)
    except Exception as exc:  # noqa: BLE001 - the type only
        return {"tool": name, "reason": f"error:{type(exc).__name__}"}
    return {"tool": name, "reason": rep.reason, "sessions": rep.sessions,
            "limit_hits": rep.limit_hits, "errors": rep.errors, "in_bytes": rep.in_bytes,
            "out_bytes": rep.out_bytes, "eligible": rep.eligible,
            "saving_per_run": rep.saving_per_run,
            "gate": composed_gate(rep, composed_monthly(rep, 0.0))}


def _replay_check(parser: argparse.ArgumentParser, args: argparse.Namespace,
                  bridge: Bridge | None, now: float | None,
                  run_program: Callable[..., str] | None,
                  replay_argv: Callable[[str], list[str]] | None,
                  search: Callable[..., ReducerResult]) -> int:
    """Counts and pass/fail only: result text, argument values, field names and
    hosts never reach stdout or stderr."""
    if args.days < 1:
        parser.error("--days must be at least 1")
    payload = (_load_tool(parser, args.composed, args.replay_input)
               if args.composed is not None else None)
    if payload is None and args.replay_input:
        parser.error("--replay-input needs --composed")
    b = bridge if bridge is not None else HermesBridge()
    if args.pricing is not None:
        pricing = Pricing.from_dict(json.loads(args.pricing.read_text(encoding="utf-8")))
    else:
        pricing = Pricing.from_dict(b.pricing())
    end = args.now if args.now is not None else (time.time() if now is None else now)
    db = args.db if args.db is not None else (
        Path(os.environ.get("HERMES_HOME", DEFAULT_HOME)) / "state.db")
    try:
        if not Path(db).is_file():
            raise HistoryUnavailable("no such file")
        runs = History.open(db).replay_runs(args.job, end - args.days * DAY, end)
    except HistoryUnavailable:
        print("replay-check: history unavailable", file=sys.stderr)
        return 1
    if payload is not None:
        print(json.dumps(_composed_line(payload, runs, pricing, run_program, replay_argv),
                         sort_keys=True))
        return 0
    try:  # the cycle excludes the job prompt's strings; without it nothing is excluded
        job = b.get_job(args.job)
    except Exception as exc:  # noqa: BLE001 - the type only
        print(f"replay-check: job prompt unavailable ({type(exc).__name__})", file=sys.stderr)
        job = None
    prompt = job.get("prompt") if isinstance(job, dict) else None
    exclude = [prompt] if isinstance(prompt, str) else []
    for tool in [args.tool] if args.tool else sorted(policy.REDUCER_TOOLS):
        print(json.dumps(_reducer_line(tool, args.job, runs, exclude, pricing, search), sort_keys=True))
    return 0


def main(argv: list[str] | None = None, bridge: Bridge | None = None,
         now: float | None = None, run_program: Callable[..., str] | None = None,
         replay_argv: Callable[[str], list[str]] | None = None,
         search: Callable[..., ReducerResult] | None = None) -> int:
    """`run_program` and `replay_argv` (relay.run_program's signature, and the runner
    argv for a container name) are for composed replay-check; None means the plugin's
    relay in Docker, as the cycle uses. `search` (replay.search_reducer's signature)
    is the reducer search; None means search_reducer."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "replay-check":
        return _replay_check(parser, args, bridge, now, run_program, replay_argv,
                             search if search is not None else search_reducer)
    if args.cmd == "pricing":
        b = bridge if bridge is not None else HermesBridge()
        print(json.dumps(Pricing.from_dict(b.pricing()).to_dict(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "run":
        home = Path(os.environ.get("HERMES_HOME", DEFAULT_HOME))
        cfg = CycleConfig(root=home / "tuner", db=home / "state.db",
                          broker_state=home / "broker" / "state",
                          skills_root=home / "skills", dry_run=args.dry_run)
        report = run_cycle(cfg, bridge if bridge is not None else HermesBridge(),
                           time.time() if now is None else now,
                           clock=time.time if now is None else None)
        if report.skipped:
            print(report.skipped)
            return 0
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        return 0
    if args.cmd == "detect":
        if args.fixture is None and args.db is None:
            parser.error("detect needs --db or --fixture")
        if args.pricing is not None:
            pricing = Pricing.from_dict(json.loads(args.pricing.read_text(encoding="utf-8")))
        else:
            pricing = Pricing.from_dict((bridge if bridge is not None else HermesBridge()).pricing())
        sessions = (scrub.load(args.fixture) if args.fixture
                    else History.open(args.db).sessions(0))
        anchor = args.now if args.now is not None else max(
            (s.started_at for s in sessions), default=0.0)
        for c in detect(sessions, anchor, pricing)[:10]:
            print(json.dumps(asdict(c), sort_keys=True))
        return 0
    sessions = History.open(args.db).sessions(args.since, args.until)
    scrub.dump(sessions, args.out, os.urandom(16))
    return 0


if __name__ == "__main__":
    sys.exit(main())
