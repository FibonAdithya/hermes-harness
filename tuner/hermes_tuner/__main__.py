"""CLI: python -m hermes_tuner run [--dry-run] | detect ... | scrub ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .bridge import Bridge, HermesBridge
from .cycle import CycleConfig, run_cycle
from .detect import detect
from .history import History
from . import scrub

DEFAULT_HOME = "/home/hermes/.hermes"


def _date(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hermes_tuner")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one daily cycle")
    run.add_argument("--dry-run", action="store_true", help="apply nothing; print the report")
    det = sub.add_parser("detect", help="print the top 10 candidates as JSON lines")
    det.add_argument("--db", type=Path, required=True)
    det.add_argument("--fixture", type=Path, help="a scrubbed fixture instead of the db")
    det.add_argument("--now", type=_date, help="YYYY-MM-DD (00:00 UTC) anchoring the window")
    scr = sub.add_parser("scrub", help="write a scrubbed fixture")
    scr.add_argument("--db", type=Path, required=True)
    scr.add_argument("--out", type=Path, required=True)
    scr.add_argument("--since", type=_date, default=0.0, help="YYYY-MM-DD")
    return p


def main(argv: list[str] | None = None, bridge: Bridge | None = None,
         now: float | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "run":
        home = Path(os.environ.get("HERMES_HOME", DEFAULT_HOME))
        cfg = CycleConfig(root=home / "tuner", db=home / "state.db",
                          broker_state=home / "broker" / "state",
                          skills_root=home / "skills", dry_run=args.dry_run)
        report = run_cycle(cfg, bridge if bridge is not None else HermesBridge(),
                           time.time() if now is None else now)
        if report.skipped:
            print(report.skipped)
            return 0
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        return 0
    if args.cmd == "detect":
        sessions = (scrub.load(args.fixture) if args.fixture
                    else History.open(args.db).sessions(0))
        anchor = args.now if args.now is not None else max(
            (s.started_at for s in sessions), default=0.0)
        for c in detect(sessions, anchor)[:10]:
            print(json.dumps(asdict(c), sort_keys=True))
        return 0
    sessions = History.open(args.db).sessions(args.since)
    scrub.dump(sessions, args.out, os.urandom(16))
    return 0


if __name__ == "__main__":
    sys.exit(main())
