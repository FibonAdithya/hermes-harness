"""Command line entry point. Each subcommand registers itself in `_COMMANDS`."""
import argparse
import subprocess
import sys
import time
from pathlib import Path

from . import config


def _cmd_mcp(args, cfg) -> int:
    from . import mcp_server  # imported here: the container core has no `mcp`

    mcp_server.serve(cfg, write=not args.no_write)
    return 0


def _add_mcp(sub) -> None:
    p = sub.add_parser("mcp", help="run the wiki MCP server on stdio")
    p.add_argument("--no-write", action="store_true", help="omit the add_note tool")
    p.set_defaults(func=_cmd_mcp)


def _cmd_export_public(args, cfg) -> int:
    from . import export

    src = Path(args.src).expanduser()
    try:
        head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip().splitlines()
        print(f"hermes-wiki: cannot read HEAD of {src}: {detail[0] if detail else e}", file=sys.stderr)
        return 2
    n = export.export_public(src, Path(args.dest).expanduser(), head, time.time())
    print("unchanged" if n < 0 else f"exported {n} pages")
    return 0


def _add_export_public(sub) -> None:
    p = sub.add_parser("export-public", help="build the filtered public export of a wiki checkout")
    p.add_argument("--src", required=True)
    p.add_argument("--dest", required=True, help="symlink to point at the new export")
    p.set_defaults(func=_cmd_export_public, needs_config=False)


def _cmd_worklist(args, cfg) -> int:
    import json
    from datetime import date

    from . import worklist

    try:
        today = date.fromisoformat(args.today) if args.today else date.today()
    except ValueError:
        print(f"hermes-wiki: --today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
        return 2
    print(json.dumps(worklist.build(Path(args.root), today), indent=2, ensure_ascii=False))
    return 0


def _add_worklist(sub) -> None:
    p = sub.add_parser("worklist", help="print the curator worklist for a wiki checkout as JSON")
    p.add_argument("--root", required=True)
    p.add_argument("--today", help="YYYY-MM-DD (default: today)")
    p.set_defaults(func=_cmd_worklist, needs_config=False)


_COMMANDS = [_add_mcp, _add_export_public, _add_worklist]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-wiki")
    sub = parser.add_subparsers(dest="command", required=True)
    for add in _COMMANDS:
        add(sub)
    for p in sub.choices.values():
        p.add_argument("--config", default=config.DEFAULT_PATH, help="config file path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "needs_config", True):
        return args.func(args, None)
    try:
        cfg = config.load(args.config)
    except (OSError, ValueError, TypeError) as e:
        print(f"hermes-wiki: cannot load config {args.config}: {e}", file=sys.stderr)
        return 2
    return args.func(args, cfg)
