# Wiki conventions

This wiki is an Obsidian-compatible vault. It is the assistant's long-term
memory and the owner's second brain — both read it.

- One topic per page. Short kebab-case filenames, e.g. `jane-doe.md`,
  `project-atlas.md`.
- Entity pages are first-class. Prefer a page per **person**, **project**,
  **organisation**, and **standing commitment**.
- Every page gets a one-line entry under "## Pages" in `index.md`.
- Cross-link with `[[page-name]]` (no `.md`). Obsidian resolves these into a
  graph view, so link generously — an unlinked page is a lost page.
- Before creating a page, `rg` the wiki to check it doesn't already exist.
  Extend the existing page instead of duplicating.
- Record concrete facts, decisions, and context — not restated generalities.
- Never record secrets: no passwords, API keys, card numbers, or one-time
  codes, even if they appear in an email.
- Delete or correct pages that turn out to be wrong.
- Only ever edit files here. A background timer commits and pushes the wiki;
  never run git inside this repo yourself.
