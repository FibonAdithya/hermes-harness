"""Command line entry point. Each subcommand registers itself in `_COMMANDS`."""
import argparse
import sys

from . import config


def _cmd_mcp(args, cfg) -> int:
    from . import mcp_server  # imported here: the container core has no `mcp`

    mcp_server.serve(cfg, write=not args.no_write)
    return 0


def _add_mcp(sub) -> None:
    p = sub.add_parser("mcp", help="run the wiki MCP server on stdio")
    p.add_argument("--no-write", action="store_true", help="omit the add_note tool")
    p.set_defaults(func=_cmd_mcp)


_COMMANDS = [_add_mcp]


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
    try:
        cfg = config.load(args.config)
    except (OSError, ValueError, TypeError) as e:
        print(f"hermes-wiki: cannot load config {args.config}: {e}", file=sys.stderr)
        return 2
    return args.func(args, cfg)
