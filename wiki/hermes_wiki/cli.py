"""Command line entry point. Each subcommand registers itself in `_COMMANDS`."""
import argparse
import os
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


def _cmd_session_end(args, cfg) -> int:
    from . import extract

    try:
        text = sys.stdin.read()
    except Exception:
        text = ""
    extract.session_end(text, os.environ, cfg)
    return 0  # the hook must never fail a session exit


def _add_session_end(sub) -> None:
    p = sub.add_parser("session-end", help="SessionEnd hook: condense, queue and spawn the extractor")
    p.set_defaults(func=_cmd_session_end)


def _cmd_drain(args, cfg) -> int:
    from . import extract

    for session, ok in extract.drain(cfg, limit=args.limit):
        print(f"{session}: {'ok' if ok else 'failed'}")
    return 0


def _add_drain(sub) -> None:
    p = sub.add_parser("drain", help="run the extractor over queued sessions, newest first")
    p.add_argument("--limit", type=int, default=4)
    p.set_defaults(func=_cmd_drain)


def _cmd_extract_dir(args, cfg) -> int:
    from . import extract

    ok = extract.extract_dir(cfg, Path(args.projects).expanduser(), args.session)
    print("extracted" if ok else "not extracted")
    return 0 if ok else 1


def _add_extract_dir(sub) -> None:
    p = sub.add_parser("extract-dir", help="extract notes from the newest transcript under a projects dir")
    p.add_argument("--projects", required=True)
    p.add_argument("--session", required=True)
    p.set_defaults(func=_cmd_extract_dir)


def _cmd_install_hook(args, cfg) -> int:
    from . import hooks

    settings = Path(args.settings).expanduser()
    try:
        changed = hooks.install_session_end_hook(settings, args.command)
    except (OSError, ValueError) as e:
        print(f"hermes-wiki: cannot update {settings}: {e}", file=sys.stderr)
        return 2
    print(f"hook added to {settings}" if changed else f"hook already present in {settings}")
    return 0


def _add_install_hook(sub) -> None:
    p = sub.add_parser("install-hook", help="add the SessionEnd hook to a Claude Code settings.json")
    p.add_argument("--settings", required=True)
    p.add_argument("--command", default=str(Path("~/.local/bin/hermes-wiki session-end").expanduser()))
    p.set_defaults(func=_cmd_install_hook, needs_config=False)


def _cmd_import_memory(args, cfg) -> int:
    from . import importer, notes

    projects = Path(args.projects).expanduser()
    if not projects.is_dir():
        print(f"hermes-wiki: --projects {projects} is not a directory", file=sys.stderr)
        return 2
    add = importer.validate_only if args.dry_run else notes.add_note
    try:
        results = importer.import_claude_memory(projects, cfg, add=add, ledger=cfg.state_dir / "imported.json",
                                                record=not args.dry_run)
    except (OSError, ValueError) as e:
        print(f"hermes-wiki: import failed: {e}", file=sys.stderr)
        return 2
    skipped = failed = 0
    for f, res in results:
        skipped += res.startswith("skipped: ")
        failed += res.startswith("failed: ")
        print(f"{f}\t{res}")
    done = len(results) - skipped - failed
    print(f"{'would import' if args.dry_run else 'imported'} {done}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


def _add_import_memory(sub) -> None:
    p = sub.add_parser("import-claude-memory", help="one-time import of Claude Code memory files as inbox notes")
    p.add_argument("--projects", required=True)
    p.add_argument("--dry-run", action="store_true", help="validate only; publish nothing")
    p.set_defaults(func=_cmd_import_memory)


_COMMANDS = [_add_mcp, _add_export_public, _add_worklist, _add_session_end, _add_drain, _add_extract_dir,
             _add_install_hook, _add_import_memory]


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
