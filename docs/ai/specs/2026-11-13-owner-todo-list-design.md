# Owner todo list for agent follow-ups

Date: 2026-11-13. Status: draft for owner approval.

## Intent

Claude Code sessions send the owner a Telegram reminder (`remind-owner` on the
droplet) for anything that needs him later. Between 2026-11-12 00:00 and
2026-11-13 10:40 UTC, 13 reminders fired (MEASURED: `hermes cron runs` and
`~/.hermes/cron/output/` on hermes-vm). Ten were decisions or "merge PR N" items
that did not need a ping at any particular time; five arrived together at 09:00
on 2026-11-13, two of them the same reminder.

The owner wants those items on his todo list instead, so he can see where to
continue, and a Telegram ping only when something needs his attention at a time.

The todo list is `todos.md` at the root of the wiki repo
`FibonAdithya/hermes-wiki` (branch `main`). The owner edits it in Obsidian on
his laptop (vault `~/hermes-wiki`, obsidian-git plugin) and on his phone. Agents
get their own heading in it.

## The split (owner decision, 2026-11-13)

- **Ping** (`remind-owner`, unchanged behaviour): tied to a time. A running
  job's expected finish, a check on a date, or something that breaks if nobody
  acts by then.
- **Todo item** (new): everything else. Decisions the owner deferred, problems
  found but not fixed, "continue from here" notes, PRs waiting for his merge.
  No Telegram message.
- No daily summary ping and no change to the morning briefing.

## Part A: `hermes-wiki todo` (wiki package)

### Commands

```
hermes-wiki todo add TITLE [--detail LINE]... [--detail-stdin]
hermes-wiki todo done ID
hermes-wiki todo list [--all]
```

- `add` appends one item to the agents' section and prints the new id on its
  own line on stdout. `--detail-stdin` reads detail lines from stdin (one
  detail per non-blank line); it combines with `--detail` (flags first).
- `done` ticks the item with that id. An item already ticked: exit 0 and say
  so on stderr. No item with that id in the agents' section: exit 1.
- `list` prints the open items of the agents' section (`--all`: ticked ones
  too), one line each: id, then the title. Read-only.

They run on the laptop and the box, where the `hermes-wiki` CLI and an
authenticated `gh` already exist (`wiki/install-laptop.sh`, `box/install.sh`).
They do not run on the droplet; Hermes there edits the wiki checkout directly
and is out of scope.

All three commands read `todos.md` from GitHub (the GET below), never from the
local wiki root. On the laptop that root can be out of date. On the box it is
the public export, which has no `todos.md`.

**Privacy on the box (design call, 2026-11-13).** The box config has
`public_only: true`, and its `search` and `read` hide private pages; `todos.md`
is private. With `cfg.public_only` true:

- `add` and `done` run. They need the whole file to apply the edit, but print
  nothing from it except the new id (`add`) or the stderr messages above.
- `list` refuses with exit 2 and a one-line message, and makes no GitHub call.
- Every error message prints the id or the path only, never file content.

### The section

The agents' section is the level-2 heading line `## Agent follow-ups` and every
line after it up to, not including, the next line that starts with `## ` (or
the end of the file).

- If the heading is missing, `add` inserts it directly after the first level-1
  heading line (`# ...`), separated by one blank line on each side. If there
  is no level-1 heading, it goes directly after the frontmatter block. `done`
  and `list` with no section: `done` exits 1, `list` prints nothing.
- If the heading appears more than once, every command refuses with exit 1
  and changes nothing.
- New items go at the end of the section, after any existing items and before
  the blank line(s) that end it.
- Every byte of the file outside the section is unchanged after any command.
  That includes the frontmatter, which keeps `todos.md` private (`tags:
  [people]`; `schema.is_private`), so the box's public export never includes it.
- `todos.md` missing on `main`: exit 1, create nothing.

### Item format

```
- [ ] TITLE (YYYY-MM-DD, SOURCE, id XXXXXX)
  - DETAIL
  - DETAIL
```

- The date is the UTC date. SOURCE is `cfg.source` (`claude-laptop`,
  `claude-box`). The id is 6 lowercase hex characters from `secrets`, unique
  within the section (regenerate on a clash).
- `done` replaces `- [ ]` with `- [x]` on that item's first line and changes
  nothing else. Matching is on the exact text `id XXXXXX)` at the end of an
  item's first line inside the section only.
- An item's details are the following lines that start with two spaces. The
  owner may edit or reorder items by hand; the commands must parse whatever
  is there without failing on lines they do not recognise (such lines are kept
  as they are).

### Validation (before any network call)

- TITLE: one line, no control characters, 1–120 characters after stripping.
- Each DETAIL: one line, no control characters, at most 300 characters. At
  most 15 details.
- `secretscan.find_secrets` over the title and details combined: any hit
  refuses the item (exit 2, the matched pattern names on stderr, no write).
- Any validation failure exits 2.

### Writing to GitHub

Every write goes through the GitHub contents API with `gh` (`cfg.gh`), as
`notes.publish` does, never through a local clone:

1. `GET repos/{cfg.repo}/contents/todos.md?ref=main`: base64 content and blob
   `sha`.
2. Apply the edit in memory.
3. `PUT` the new content with that `sha`, `branch=main`, message
   `todo: add XXXXXX` or `todo: done XXXXXX`.
4. On HTTP 409 (the file or branch moved since step 1), go back to step 1. At
   most 5 attempts, with the same backoff as `notes.publish`; then exit 1.
5. A timeout on the PUT is not retried: report on stderr that the write may or
   may not have landed and exit 1 (same reason as `notes.publish`).

The `sha` makes each write a compare-and-swap, so a concurrent write by the
owner's obsidian-git push or the droplet's 15-minute wiki sync is never
overwritten. It only causes a retry.

The GitHub calls go through an injected callable (the pattern `notes.add_note`
uses with `publisher`), so tests never reach the network. Tests pass the fake
explicitly; they do not monkeypatch module defaults. A default argument binds at
definition time, and a patched default once published to the live wiki.

`HERMES_WIKI_DRY_PUBLISH=1` makes `add` and `done` read and write
`<state_dir>/dry/todos.md` instead of GitHub (seeded from a fixture in tests).

### Curator

`droplet/wiki-curator-prompt.md` gets one rule: never edit, move, split, trim
or file anything into the `## Agent follow-ups` section of `todos.md`. The
curator may still treat the rest of `todos.md` as it does today.

### Tests (Part A)

Each test names the mutation it catches.

1. Owner sections and frontmatter byte-identical after `add` and after `done`
   on a fixture copied from the real file's shape (frontmatter, `# Todos`,
   several `##` owner sections with blank items). Catches: rewriting the whole
   file, normalising whitespace, re-dumping frontmatter.
2. `add` with no section inserts it right after `# Todos` and before the
   owner's first `##` section. Catches: appending at the end of the file;
   inserting before the H1.
3. `add` into an existing section with two items puts the new one third, inside
   the section, before the next `##`. Catches: off-by-one at the section
   boundary (writing into the owner's next section).
4. `is_private` on the frontmatter after `add` is still true. Catches: any
   frontmatter change that drops `people`.
5. The fake GitHub returns 409 on the first PUT: the command re-GETs and the
   second PUT carries the new sha and an edit applied to the new content (the
   fake changed the owner's section between the two GETs; that change
   survives). Catches: retrying the stale body; no retry.
6. PUT times out: exactly one PUT, exit 1, "may or may not" on stderr.
   Catches: blind retry.
7. A title containing a token that `find_secrets` flags: exit 2, zero GitHub
   calls. Catches: scan after write, or scan skipped.
8. `done ID` ticks only that item when two items share a title. Catches:
   matching on title instead of id.
9. `done ID` where `id ID)` appears only in the owner's section: exit 1, no
   PUT. Catches: matching outside the section.
10. Duplicate `## Agent follow-ups` headings: `add` exits 1, no PUT.
11. `list` skips ticked items unless `--all`.
12. Validation boundaries: a 120-character title is accepted, 121 refused; 15
    details accepted, 16 refused.
13. With `public_only` true, `list` exits 2 with zero GitHub calls, and `add`
    prints exactly the id line on stdout although the fixture's owner sections
    contain text. Catches: a missing `public_only` check; echoing the file.

### Docs (Part A)

- `runbooks/wiki.md`: a "Todo items" section. What writes where, the section
  rule, the commands, and that this is the one exception to "other agents only
  add notes under `inbox/`".
- `wiki/install-laptop.sh` and `box/install.sh` need no change if the
  subcommand ships in the existing `hermes-wiki` entry point. Verify that, and
  say so in the PR.

## Part B: `remind-owner` cancel and cleanup (droplet)

Separable from Part A; it touches only `droplet/remind-owner.sh`, its tests in
`box/tests/test_remind_owner.py`, and the header comment.

`hermes cron remove ID` deletes the job but leaves its `remind-*.sh` and
`remind-*.txt` in `~/.hermes/scripts/`. On 2026-11-13, 6 of the 9 message files
there had no job (MEASURED: `ls ~/.hermes/scripts/remind-*.txt` against
`~/.hermes/cron/jobs.json`).

```
remind-owner --cancel JOB_ID
remind-owner --prune
```

- `--cancel`: read `~/.hermes/cron/jobs.json` (a JSON object whose `jobs` key
  is a list of objects with `id`, `name`, `script`), find the job, and refuse
  (exit 1, nothing removed) unless its `script` matches
  `remind-[0-9]{14}-[0-9]+\.sh`. Otherwise run `hermes cron remove JOB_ID`
  and, only if that succeeded, delete that `.sh` and its `.txt`.
- `--prune`: delete every `~/.hermes/scripts/remind-*.sh` / `.txt` pair whose
  `.sh` name is not the `script` of any job in `jobs.json` and whose `.sh` was
  last modified more than 10 minutes ago. Print each deleted path.
  Unreadable or malformed `jobs.json`: exit 1, delete nothing. The age guard
  exists because `remind-owner` writes the pair before `hermes cron create`
  registers the job. A prune in that window would otherwise delete a reminder
  that is about to exist (design call, 2026-11-13).
- The existing `WHEN NAME < message` form is unchanged.

Tests (stub `hermes` on PATH, fake `jobs.json` under a temp `HOME`, as the
existing tests do):

1. `--cancel` of a reminder job calls `hermes cron remove ID` once and leaves
   no files for it. Another reminder's files stay. Catches: deleting by glob.
2. `--cancel` of a job whose script is `wiki-worklist.py`: exit 1, no `cron
   remove`, nothing deleted. Catches: cancelling non-reminder jobs.
3. `cron remove` fails: files kept, exit non-zero. Catches: deleting before
   removal succeeds.
4. `--prune` with one live and one orphan pair deletes only the orphan.
   Catches: inverted membership test.
5. `--prune` with malformed `jobs.json` deletes nothing. Catches: treating a
   parse failure as "no jobs" (which would delete every pending reminder).
6. `--prune` keeps an orphan pair whose `.sh` mtime is 5 minutes old and
   deletes one that is 11 minutes old (mtimes set with `os.utime`, not by
   sleeping). Catches: a missing or inverted age guard.

## Non-goals

- An MCP tool for todo items (the CLI is enough; sessions already run it).
- Changing the morning briefing or adding any summary message.
- Pruning ticked items. The owner deletes them when he wants.
- A todo command on the droplet.
- Editing the owner's own sections, or creating `todos.md`.

## Outside the repo (done by the owner's session after merge, not by the builder)

- Rewrite the "Reminders for me" section of the owner's global
  `~/.claude/CLAUDE.md` around the split above: `hermes-wiki todo add` by
  default, `remind-owner` only for time-bound items, `remind-owner --cancel`
  instead of `hermes cron remove`.
- Move the two queued reminders that are not time-bound (jobs `8cd254d6253e`
  "fleet public: finish switchover" and `7319b2ed9950` "chain PR 4 open
  decisions") to todo items and cancel them; keep `6f192f9fab05` (naive-loop,
  2026-11-14 06:00); run `remind-owner --prune` once.

## Left to the builder

Module layout inside `wiki/hermes_wiki/` (for example a new `todos.py`), exact
stderr wording, and how `--detail-stdin` and `--detail` are parsed, within the
rules above.
