# Wiki conventions

This wiki is an Obsidian-compatible vault. It is the assistant's long-term
memory and the owner's second brain — both read it.

- One topic per page. Short kebab-case filenames, e.g. `jane-doe.md`,
  `project-atlas.md`.
- Entity pages are first-class. Prefer a page per **person**, **project**,
  **organisation**, and **standing commitment**.
- Every page gets a one-line entry in `index.md`, under the heading for its
  type (see "Index" below).
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

## Frontmatter

Every page except `index.md`, `CONVENTIONS.md` and `TAGS.md` starts with a
frontmatter block in exactly this format:

```
---
type: project
tags: [hermes, droplet, cost]
repos: [hermes-harness]
updated: 2026-10-26
review_by: 2026-11-26
---
```

- A `---` line opens and closes the block. One `key: value` per line. Lists are
  written `key: [a, b]`. Quote a value that contains `:` or `#`, e.g.
  `verified: "gh api ... -> private"`. No other YAML forms.
- `type` (required): one of `person`, `project`, `org`, `commitment`, `topic`,
  `how-to`, `decision`, `log`.
- `tags` (required): each tag must be listed in `TAGS.md`.
- `repos` (optional): GitHub repository names the page is about.
- `updated` (required): the date of the last change, `YYYY-MM-DD`.
- `review_by` (optional): a date after which the page should be checked again;
  use it for status, plans and prices.

## Private pages

A page is private when it has `type: person`, or carries one of the tags
`finance`, `investing`, `health` or `people`, or has no frontmatter. Private
pages are never copied to the coding box. Facts about people, money,
investments or health go only on private pages. Never remove `type: person` or
a private tag from a page.

## Inbox

Other agents add notes as new files under `inbox/`. The rule:
only the curator edits pages from notes; never hand-edit `inbox/`.
The curator files each note into a page (or discards it) and then deletes the
note file.

## Size caps

- A page's body is at most 1,500 words. Over that, split it into linked pages
  or cut stale material. Pages with `type: log` (such as `nights/` pages and
  `content-roundup`) are exempt.
- `TAGS.md` holds at most 40 tags. `finance`, `investing`, `health` and
  `people` are never removed or renamed.
- The curator does at most 5 split, trim or delete operations per night.
- `index.md`, `CONVENTIONS.md` and `TAGS.md` have no frontmatter and no word
  cap.

## Index

`index.md` lists every page except `index.md`, `CONVENTIONS.md` and `TAGS.md`,
grouped under one `## <type>` heading per page type (pages without frontmatter
go under `## unsorted`). Each entry is one line, `- [[page-name]] — summary`, and
every entry uses a `[[wikilink]]`. No other prose.

## Finding pages by tag

Filter by tag with `rg` on the frontmatter, for example:

```
rg -l '^tags:.*\bcost\b'
```

Combine with a type: `rg -l '^type: decision' $(rg -l '^tags:.*\bcost\b')`.
