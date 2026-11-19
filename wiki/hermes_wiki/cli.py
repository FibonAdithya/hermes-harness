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


def _cmd_seed(args, cfg) -> int:
    from . import seed

    try:
        actions = seed.seed(args.repo, force=args.force, gh=args.gh)
    except (OSError, seed.SeedError) as e:
        print(f"hermes-wiki: seed failed: {e}", file=sys.stderr)
        return 2
    for a in actions:
        print(a)
    return 0


def _add_seed(sub) -> None:
    p = sub.add_parser("seed", help="one-time: create TAGS.md and replace CONVENTIONS.md in the wiki repo")
    p.add_argument("--force", action="store_true", help="replace CONVENTIONS.md even if it changed since planning")
    p.add_argument("--repo", default="FibonAdithya/hermes-wiki")
    p.add_argument("--gh", default="gh", help="gh binary")
    p.set_defaults(func=_cmd_seed, needs_config=False)


def _cmd_todo(args, cfg) -> int:
    import json
    import secrets
    from dataclasses import asdict
    from datetime import datetime, timezone

    from . import todocmds, todos

    run, sleep = subprocess.run, time.sleep
    today = datetime.now(timezone.utc).date()
    new_id = lambda: secrets.token_hex(3)  # noqa: E731
    tree = todocmds.tree_for(cfg, run)
    common = {"today": today, "new_id": new_id, "sleep": sleep}
    try:
        if args.todo_cmd == "add":
            details = list(args.detail)
            if args.detail_stdin:
                details += [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
            print(todocmds.cmd_add(cfg, tree, args.title, details, args.project, args.done_when,
                                   Path.cwd(), run=run, **common))
        elif args.todo_cmd == "done":
            if not todocmds.cmd_done(cfg, tree, args.id, args.reason, run=run, **common):
                print(f"hermes-wiki: {args.id} is already done", file=sys.stderr)
        elif args.todo_cmd == "edit":
            todocmds.cmd_edit(cfg, tree, args.id, args.title, args.add_detail, args.project,
                              args.done_when, **common)
        elif args.todo_cmd == "rm":
            todocmds.cmd_rm(cfg, tree, args.id, **common)
        elif args.todo_cmd == "reopen":
            todocmds.cmd_reopen(cfg, tree, args.id, **common)
        else:
            for r in todocmds.cmd_list(cfg, tree, args.all, args.json, args.project, run=run):
                if args.json:
                    print(json.dumps(asdict(r), sort_keys=True, ensure_ascii=False))
                else:
                    tag = {"suggested": " [suggested]", "done": " [done]"}.get(r.status, "")
                    print(f"{r.id}{tag} {r.title}")
    except todos.TodoRejected as e:
        for err in e.errors:
            print(f"hermes-wiki: todo refused: {err}", file=sys.stderr)
        return e.code
    except todos.TodoError as e:
        print(f"hermes-wiki: {e}", file=sys.stderr)
        return e.code
    return 0


def _add_todo(sub) -> None:
    p = sub.add_parser("todo", help="add, edit, tick or list agent follow-ups (agent-todos in the wiki repo)")
    p.set_defaults(func=_cmd_todo)
    tsub = p.add_subparsers(dest="todo_cmd", required=True)
    a = tsub.add_parser("add", help="add an item; prints its id")
    a.add_argument("title")
    a.add_argument("--detail", action="append", default=[], help="detail line (repeatable)")
    a.add_argument("--detail-stdin", action="store_true", help="also read detail lines from stdin")
    a.add_argument("--project", help="project name (default: from the git origin of the cwd)")
    a.add_argument("--done-when", action="append", default=[],
                   help="pr:OWNER/REPO#N or issue:OWNER/REPO#N (repeatable)")
    d = tsub.add_parser("done", help="tick an item by id")
    d.add_argument("id")
    d.add_argument("--reason", help="why it is done")
    e = tsub.add_parser("edit", help="change an item")
    e.add_argument("id")
    e.add_argument("--title")
    e.add_argument("--add-detail", action="append", default=[], help="detail line to append (repeatable)")
    e.add_argument("--project")
    e.add_argument("--done-when", action="append", default=[],
                   help="check to append: pr:OWNER/REPO#N or issue:OWNER/REPO#N (repeatable)")
    r = tsub.add_parser("rm", help="remove an item")
    r.add_argument("id")
    ro = tsub.add_parser("reopen", help="reopen a done item or reject a suggestion")
    ro.add_argument("id")
    ls = tsub.add_parser("list", help="list open items (--all includes done)")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true", help="one JSON record per line")
    ls.add_argument("--project", help="only items of this project")
    for child in (a, d, e, r, ro, ls):
        child.add_argument("--config", default=argparse.SUPPRESS, help="config file path")


_COMMANDS = [_add_mcp, _add_export_public, _add_worklist, _add_session_end, _add_drain, _add_extract_dir,
             _add_install_hook, _add_import_memory, _add_seed, _add_todo]


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
