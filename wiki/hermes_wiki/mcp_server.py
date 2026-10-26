"""The `wiki` MCP server. Imports `mcp`, so only the `mcp` subcommand loads it."""
import json
import os
import time
from typing import Callable

from mcp.server import MCPServer

from . import notes, search as search_mod
from .config import WikiConfig
from .freshness import maybe_pull
from .schema import PRIVATE_TAGS, load_tags
from .search import PageNotFound


def _err(e: Exception) -> str:
    return json.dumps({"error": str(e)})


def make_tools(cfg: WikiConfig, now=time.time, publisher=notes.publish) -> dict[str, Callable]:
    stamp = cfg.state_dir / "last-pull"

    def _warning() -> str:
        if not cfg.pull:
            return ""
        w = maybe_pull(cfg.root, stamp, now())
        return f"{w}\n" if w else ""

    def search(
        query: str = "",
        tags: list[str] = [],
        type: str | None = None,
        repos: list[str] = [],
        updated_after: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search wiki pages by words in the body, optionally filtered by tags, type, repos or an updated-after date (YYYY-MM-DD). Returns JSON hits with page name, type, tags and matching lines."""
        warn = _warning()
        hits = search_mod.search(
            cfg.root, query, tags, type, repos, updated_after, limit, public_only=cfg.public_only
        )
        return warn + json.dumps(hits)

    def read(page: str, follow_links: bool = False) -> str:
        """Read one wiki page by name (for example `hermes-harness` or `nights/2026-09-25`). With follow_links, also appends short excerpts of pages it links to."""
        warn = _warning()
        try:
            text = search_mod.read(cfg.root, page, follow_links, public_only=cfg.public_only)
        except PageNotFound as e:
            return warn + _err(e)
        return warn + text

    def list_tags() -> str:
        """List the tags allowed on notes, as a JSON list. Use only these in add_note, and put new ideas in suggest_tags."""
        tags = load_tags(cfg.root)
        if cfg.public_only:
            tags -= PRIVATE_TAGS
        return json.dumps(sorted(tags))

    def add_note(
        kind: str,
        body: str,
        tags: list[str],
        repos: list[str] = [],
        verified: str = "claim",
        suggest_tags: list[str] = [],
        review_by: str | None = None,
    ) -> str:
        """Add a short note (<=300 words) to the wiki inbox for the curator. Search first; tags must come from list_tags. Returns JSON with the note path, or an error."""
        try:
            path = notes.add_note(
                cfg, kind=kind, body=body, tags=tags, repos=repos, verified=verified,
                suggest_tags=suggest_tags, review_by=review_by,
                session=os.environ.get("HERMES_WIKI_SESSION", ""), publisher=publisher,
            )
        except (notes.NoteRejected, notes.PublishError) as e:
            return _err(e)
        return json.dumps({"path": path})

    return {"search": search, "read": read, "list_tags": list_tags, "add_note": add_note}


def serve(cfg: WikiConfig, write: bool) -> None:
    server = MCPServer("wiki")
    for name, fn in make_tools(cfg).items():
        if name == "add_note" and not write:
            continue
        server.add_tool(fn, name=name)
    server.run()
