"""Builder for a small fixture wiki, extended by later tasks through parameters."""
from pathlib import Path

BASE_PAGES = {
    "alpha": (
        "---\ntype: project\ntags: [hermes, cost]\nupdated: 2026-10-01\n---\n"
        "Alpha covers the deploy pipeline.\n"
        "We moved the droplet and ran deploy again.\n"
        "Unique word: alpha-only-word. See [[beta]] and [[gamma|g]].\n"
    ),
    "beta": (
        "---\ntype: decision\ntags: [cost]\nupdated: 2026-10-20\n---\n"
        "Beta records a single deploy choice.\n"
    ),
    "gamma": (
        "---\ntype: person\ntags: [people]\nupdated: 2026-09-01\n---\n"
        "Gamma is a person page with private details.\n"
    ),
    "delta": "Delta has no frontmatter but mentions deploy.\n",
    "nights/2026-09-25": (
        "---\ntype: log\ntags: [ops]\nupdated: 2026-09-25\n---\n"
        "Night log with routine entries.\n"
    ),
}
BASE_TAGS = {
    "hermes": "the Hermes harness",
    "cost": "spend and budgets",
    "people": "pages about people",
    "ops": "operations and nightly logs",
}


def build_wiki(root, extra_pages=None, index_lines=None, extra_tags=None, inbox=None, unindexed=None) -> Path:
    """Write the base wiki under root and return it.

    extra_pages: {name: full text} added (or overriding) pages, also listed in index.md.
    index_lines: extra raw lines appended to index.md (e.g. a link to a missing page).
    extra_tags: {tag: meaning} added to TAGS.md.
    unindexed: {name: full text} pages written but left out of index.md.
    inbox: {relative path under inbox/: text} extra inbox files.
    """
    root = Path(root)
    pages = {**BASE_PAGES, **(extra_pages or {})}
    for name, text in {**pages, **(unindexed or {})}.items():
        path = root / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    index = ["# Index", ""] + [f"- [[{n}]] — {n} page" for n in sorted(pages)] + list(index_lines or [])
    (root / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    tags = {**BASE_TAGS, **(extra_tags or {})}
    (root / "TAGS.md").write_text(
        "# Tags\n\n" + "".join(f"- {t} — {m}\n" for t, m in tags.items()), encoding="utf-8"
    )
    (root / "CONVENTIONS.md").write_text("# Conventions\n\nWrite short pages.\n", encoding="utf-8")
    files = {"claude-laptop/x.md": "---\nkind: fact\n---\nInbox note about deploy.\n", **(inbox or {})}
    for rel, text in files.items():
        path = root / "inbox" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
