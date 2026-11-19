# Wiki runbook

The wiki (`FibonAdithya/hermes-wiki`, branch `main`) is Hermes's long-term
memory. Hermes edits pages on the droplet; a timer (`hermes-wiki-sync.timer`)
pulls, commits and pushes every 15 minutes. Other agents (laptop and box Claude
Code sessions, the one-time memory import) only add notes under `inbox/`,
except todo items (see "Todo items"). A
nightly Hermes cron job, `wiki-curator`, files those notes into pages, keeps
pages under their size caps and tidies the wiki. Design:
`docs/ai/specs/2026-10-26-shared-knowledge-base-design.md`.

Droplet commands run as the `hermes` user. From the DigitalOcean console you
are `root`: run `sudo -iu hermes`, then `export XDG_RUNTIME_DIR=/run/user/$(id -u)`.
Without the switch `systemctl --user` looks at root's manager; without the
export it fails with "Failed to connect to bus: No medium found".

## Moving parts

| Where | What | Source |
|---|---|---|
| droplet | `~/.local/bin/hermes-wiki-sync.sh` (symlink), pull-rebase then push | `droplet/hermes-wiki-sync.sh`, `droplet/wiki-git-sync.sh` |
| droplet | `~/.hermes/scripts/wiki-worklist.py` (copy; Hermes only runs `--script` files from there), refreshed by `harness-pull` | `droplet/wiki-worklist.py` |
| droplet | `~/.hermes/skills/wiki/delegate-with-context/` | `droplet/skills/delegate-with-context/` |
| droplet | `todo-nightly.timer` (00:45) runs `todo-nightly.service`, which runs `~/.local/bin/todo-nightly.sh` (symlink) | `droplet/units/todo-nightly.{service,timer}`, `droplet/todo-nightly.sh` |
| droplet | `~/.config/hermes-wiki/config.json` (written only if absent), used by the nightly job | `droplet/hermes-wiki-droplet.json` |
| droplet | `wiki-curator` cron job, 01:00 (moved from 03:00 on 2026-11-09 so it ends before the tuner's 02:00–05:00 restart window) | prompt in `droplet/wiki-curator-prompt.md` |
| laptop | `hermes-wiki` CLI, `wiki` MCP server, SessionEnd hook | `wiki/install-laptop.sh` |
| box | wiki mirror and public export at `~/TIG/hermes-wiki-public` | `box/install.sh` |

The worklist script computes the curator's work inside `hermes-sandbox:latest`
(the wiki files are root-owned), runs every inbox note through Hermes's own
cron prompt scanner, deletes any note the scanner flags, and lists it as
`quarantined: <path> (<reason>)`. If Hermes is upgraded and the scanner can no
longer be imported, it prints `FATAL: Hermes scanner not importable` and the job
fails rather than running unfiltered.

## Rollout

Do the steps in this order. Each names the command and what it should print.
Record each result in the "Rollout log" at the bottom.

**Order matters.** The new pull-before-push sync (step 1) must be installed on
the droplet before anything else writes to the wiki (spec amendment 5). The old
sync script never pulls, so the first outside commit would make every later
droplet push fail as non-fast-forward. On 2026-10-26 a test published four junk
notes to `main`; that was repaired by resetting `main` to `c1844dd`. Step 1
therefore also confirms the droplet sync is healthy (`no changes` or `pushed`)
before any outside write.

### 1. Droplet: install the sync, worklist script and skill

Merge, then let `harness-pull` deploy the droplet checkout (or run
`systemctl --user start harness-pull.service`). On the droplet:

```bash
~/hermes-harness/droplet/install-wiki.sh
systemctl --user start hermes-wiki-sync.service
journalctl --user -u hermes-wiki-sync -n 5
```

Expected: `install-wiki.sh` prints `linked ...hermes-wiki-sync.sh`,
`installed .../wiki-worklist.py` and `installed .../delegate-with-context`; the
journal shows `pushed` or `no changes`, and no `FATAL`. If it shows
`FATAL: rebase conflict, not pushed`, stop and fix the wiki checkout before
going on.

### 2. Droplet: worklist script against the real image

```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/wiki-worklist.py | head
```

Expected: worklist sections (`## backfill`, `## orphans`, `## lint`, ...) or
`NOTHING TO DO`. A `## withheld` section or a `notes withheld: scanner` line
means the script dropped lines that Hermes's scanner flags; the curator reports
them. On today's wiki, expect `## backfill` with 10 pages. This run
is real: any note the scanner flags is deleted from `inbox/`.

### 3. Droplet: prompt and skill against Hermes's strict scanner

```bash
cd ~/.hermes/hermes-agent && venv/bin/python -c 'from tools.cronjob_tools import _scan_cron_prompt as s; print(repr(s(open("/home/hermes/hermes-harness/droplet/wiki-curator-prompt.md").read())))'
cd ~/.hermes/hermes-agent && venv/bin/python -c 'from tools.cronjob_tools import _scan_cron_prompt as s; print(repr(s(open("/home/hermes/hermes-harness/droplet/skills/delegate-with-context/SKILL.md").read())))'
```

Expected: `''` for both. Anything else names the pattern that matched; fix the
wording in the repo before creating the job (a match blocks `cron create`).

### 4. Laptop: seed `TAGS.md` and `CONVENTIONS.md`

First bring the laptop checkout to the merged `master`, so the seed files are
the reviewed ones:

```bash
git -C ~/hermes-harness fetch && git -C ~/hermes-harness checkout master && git -C ~/hermes-harness pull --ff-only
```

Expected: `Already up to date.` or a fast-forward to the merge commit; no
conflict. Then:

```bash
hermes-wiki seed
```

`hermes-wiki` is only on PATH after step 7. Before that, run it from the
checkout: `cd ~/hermes-harness/wiki && uv run hermes-wiki seed`.

Expected:

```
created TAGS.md
replaced CONVENTIONS.md
```

If it prints `hermes-wiki: seed failed: ...` (one line, exit 2), nothing after
the failing step was written; fix the cause and re-run (it is safe to re-run).
If it prints `CONVENTIONS.md changed since planning; not replaced`, Hermes
edited `CONVENTIONS.md` after 2026-10-26. Compare
`gh api repos/FibonAdithya/hermes-wiki/contents/CONVENTIONS.md --jq .content | base64 -d`
with `wiki/seed/CONVENTIONS.md`, merge by hand into `wiki/seed/CONVENTIONS.md`,
then run `hermes-wiki seed --force`.

Then wait for one droplet sync (up to 15 minutes, or
`systemctl --user start hermes-wiki-sync.service`) and check the journal shows
`pushed` or `no changes`.

### 5. Droplet: create the curator

```bash
hermes cron create "0 1 * * *" "$(cat ~/hermes-harness/droplet/wiki-curator-prompt.md)" --name wiki-curator --script wiki-worklist.py --deliver telegram:8887384436 --model deepseek/deepseek-v4-pro --provider nous
```

Expected: the new job's id. Restrict its tools (`hermes cron create` has no
toolset flag):

```bash
cd ~/.hermes/hermes-agent && venv/bin/python -c 'from cron.jobs import update_job; print(update_job("<id>", {"enabled_toolsets": ["file", "terminal"]}))'
```

Expected: the updated job, with `enabled_toolsets` `['file', 'terminal']`. Then
run it once and read the Telegram summary:

```bash
hermes cron run <id>
```

Expected: one Telegram message in the report format (`Curated N notes: ...`,
backfill and lint counts), or no message if nothing changed (`[SILENT]`).

### 6. Droplet: frontmatter for the cron jobs that write pages

`morning-briefing` (`3bc4e11d29b1`, writes `nights/<date>.md`) and
`Weekly content roundup` (`224d19b6b1ad`, writes `content-roundup.md`). For
each id, dump the current prompt, append the line, and set it:

```bash
id=3bc4e11d29b1   # then repeat with id=224d19b6b1ad
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); jobs=d.get("jobs", d) if isinstance(d, dict) else d; print(next(j["prompt"] for j in jobs if j["id"] == sys.argv[2]))' ~/.hermes/cron/jobs.json "$id" > ~/prompt-"$id".txt
cp ~/prompt-"$id".txt ~/prompt-"$id".before.txt
printf '\n%s\n' "Pages you create or edit start with the wiki's frontmatter block (see CONVENTIONS.md); nights/ pages and content-roundup use type: log." >> ~/prompt-"$id".txt
hermes cron edit "$id" --prompt "$(cat ~/prompt-"$id".txt)"
```

Expected: `hermes cron edit` confirms the change; `diff ~/prompt-"$id".before.txt ~/prompt-"$id".txt`
shows only the added line. `~/prompt-<id>.before.txt` is the rollback copy.

### 7. Laptop: install and import

`install-laptop.sh` links `~/.local/bin/hermes-wiki` into the virtualenv of the
checkout it runs from, and the `wiki` MCP server and SessionEnd hook run through
that link. Run it from a checkout that stays on `master`. If `~/hermes-harness`
is on another branch, clone one for the install (the 2026-10-27 rollout used
`git clone --branch master git@github.com:FibonAdithya/hermes-harness.git
~/.local/share/hermes-harness-wiki`; update it with `git pull` there).

```bash
~/hermes-harness/wiki/install-laptop.sh
hermes-wiki import-claude-memory --projects ~/.claude/projects --dry-run
```

Expected: the dry run lists each memory file and ends with
`would import N, skipped S, failed 0`, and no errors. Then publish:

```bash
hermes-wiki import-claude-memory --projects ~/.claude/projects
```

Expected: `imported N, skipped S, failed 0`. It is resumable: a re-run skips
files already imported.

### 8. Box: deploy and mirror

Deploy through `request_deploy` / `deploy_harness` as usual (it runs
`box/install.sh`). Then on the box:

```bash
systemctl --user start wiki-mirror.service && ls -l ~/TIG/hermes-wiki-public
gh api repos/FibonAdithya/hermes-wiki --jq .permissions.push
```

Expected: `~/TIG/hermes-wiki-public` is a symlink to `.hermes-wiki-public.<ts>`;
the `gh api` call prints `true`.

### 9. Broker

`harness-pull` restarts `hermes-approvals`. The `compute` MCP server (which has
`run_task`'s new `context_pages`) reloads only when `hermes-gateway` restarts;
the nightly 04:00 session reset does not restart it. Two checks on the droplet:

- `~/.hermes/config.yaml` lists the `compute` server's tools in
  `mcp_servers.compute.tools.include`. A broker tool missing from that list is
  never registered. Compare it with the `@mcp.tool()` functions in
  `broker/hermes_broker/server.py`.
- Restart with `systemctl --user restart hermes-gateway`, then check
  `grep "MCP server 'compute'" ~/.hermes/logs/agent.log | tail -1` lists every
  broker tool. Hermes sessions started before the restart keep the old tool
  list; send `/new`.

### 10. Acceptance checks

Run spec acceptance checks 1–13
(`docs/ai/specs/2026-10-26-shared-knowledge-base-design.md`, "Acceptance
checks") and record each below as MEASURED, with the command and its output.
For check 8, put the hostile note text in a file and publish it with
`hermes-wiki` from the laptop rather than typing it into any Hermes prompt.
Measure the cost (spec §7) from the first few curator runs.

### 11. Later: switch the curator to Claude

Put `ANTHROPIC_API_KEY` in `~/.hermes/.env`, then:

```bash
hermes cron edit <id> --model claude-sonnet-5-5 --provider anthropic
hermes cron run <id>
```

Expected: a normal curator summary on Telegram. Whether `--provider anthropic`
reads `ANTHROPIC_API_KEY` from `~/.hermes/.env` is unverified; this run is the
check (acceptance check 13).

## Todo items

The owner's todo list lives in the wiki repo on `main`, in three files:

| File | Owner | Contents |
|---|---|---|
| `agent-todos.jsonl` | `hermes-wiki todo` (laptop, box, and the droplet nightly job) | The records. One JSON object per line, sorted by id. This is the source of truth. |
| `agent-todos.md` | generated from the jsonl by every write | The page the owner reads and ticks in Obsidian. Frontmatter is `type: log`, `tags: [people]`, `updated: DATE`, so the export to the box never includes it. |
| `todos.md` | the owner | The owner's own list. After migration its `## Agent follow-ups` section holds only `![[agent-todos]]` and a link line to the page. |

`agent-todos.md` and `agent-todos.jsonl` are also kept out of the public
export by `wiki/tests/test_export.py::test_export_excludes_agent_todo_files`.
Nothing edits these files by hand and Hermes on the droplet no longer edits
`todos.md`: every change goes through `hermes-wiki todo`, which commits all
changed files in one commit through the GitHub git-data API. On the droplet
only the nightly job runs it (`todo nightly`).

**Records.** Fields: `id` (6 lowercase hex), `title` (1-120 characters, one
line), `details` (at most 15 lines of 1-300 characters), `project`
(`[a-z0-9][a-z0-9._-]{0,39}`, `unsorted` when unknown), `created` (UTC date),
`source` (the config's `source`), `status`, `done_when`, `closed` and
`suggestion`.

- `status` is `open`, `suggested` (the curator or a check thinks it is done and
  the owner has not confirmed) or `done`.
- `done_when` is a list of checks, `{"pr": "OWNER/REPO#N", "state": "merged"}`
  or `{"issue": "OWNER/REPO#N", "state": "closed"}`. The nightly job closes an
  item when any check passes.
- `closed` is `{"at": DATE, "by": "check" | "agent" | "owner", "reason": TEXT}`.
  `by` is `agent` for `todo done`, `owner` for a tick in Obsidian or in
  `todos.md`, and `check` for the nightly job.
- `suggestion` is `{"at": DATE, "reason": TEXT, "evidence": TEXT}` plus
  optional `rejected: true`, `expired: true` and `blocked_evidence: [TEXT, ...]`.
  `rejected` is set by `todo reopen` on a suggested item. `expired` is set when
  a suggestion is 7 days old without an answer. `blocked_evidence` is a sorted
  list that collects the evidence of every rejected or expired suggestion on
  the item, so a replaced suggestion does not make the old evidence usable
  again.

The page groups items as: `## Probably done, confirm` (suggested items), one
`## PROJECT` heading per project with open items (alphabetical, `unsorted`
last), and `## Recently done` (items closed fewer than 7 days ago). Each item is
`- [ ] TITLE (DATE, id XXXXXX)` in a project group and
`- [ ] TITLE (PROJECT, DATE, id XXXXXX)` elsewhere, with its details indented
two spaces. A hidden comment on the page carries the sha256 of the jsonl it was
generated from.

**Commands.** All take `--config PATH`. Item text must not contain a secret
(any secret-scan hit refuses it), and input is validated before any GitHub call.

- `hermes-wiki todo add TITLE [--detail LINE]... [--detail-stdin] [--project NAME] [--done-when CHECK]...`
  adds an item and prints its id. `--detail-stdin` also reads detail lines from
  stdin, stripping each and dropping blank ones. `--project` defaults to the repo
  name in the `origin` remote of the current directory (`unsorted` when there is
  none). A `CHECK` is `pr:OWNER/REPO#N` or `issue:OWNER/REPO#N`.
- `hermes-wiki todo edit ID [--title T] [--add-detail LINE]... [--project NAME] [--done-when CHECK]...`
  changes the title or project and appends details and checks.
- `hermes-wiki todo done ID [--reason TEXT]` closes the item with `by: agent`
  (reason defaults to `done`; it is one line, at most 300 characters, and is
  secret-scanned). An item that is already done exits 0 with a message on stderr.
- `hermes-wiki todo rm ID` deletes the record (use it for duplicates).
- `hermes-wiki todo reopen ID` reopens a done item (clears `closed`). On a
  suggested item it sets `rejected: true` and returns the item to open. On an
  open item it exits 1.
- `hermes-wiki todo list [--all] [--json] [--project NAME]` prints
  `ID TITLE` for open items, with ` [suggested]` or ` [done]` after the id.
  `--all` includes done items. `--json` prints one record per line.
- `hermes-wiki todo migrate` is a one-off, see "Migration".
- `hermes-wiki todo nightly --suggestions PATH --evidence PATH --state PATH` is
  the droplet job's command, see "Nightly job".

Exit codes: 2 for a validation failure, a secret hit, or a `public_only`
refusal. 1 for an unknown id, `reopen` on an open item, a missing
`agent-todos.jsonl` where the command needs it (`run hermes-wiki todo migrate
first`), 5 conflicts in a row, a timeout, or any other GitHub failure. Otherwise
0. Each write retries a conflict at most 5 times (reads the three files again,
re-imports the owner's ticks, re-applies the change). A timeout on the commit is
not retried: the error names the item id, and the write may or may not have
landed. Run `hermes-wiki todo list --all` on the laptop and look for that id
before adding the item again.

**Box (`public_only`).** `list` (with or without `--json`), `migrate` and
`nightly` are refused with exit 2 and make no GitHub call. The other commands
run, but print only ids and error messages, never file content, and write no
progress log.

**Dry mode.** `HERMES_WIKI_DRY_PUBLISH=1` reads and writes a local tree under
`<state_dir>/dry/` instead of GitHub. `migrate` skips the commit-history lookup
in dry mode.

**Dual mode until migrated.** Until `hermes-wiki todo migrate` has run (no
`agent-todos.jsonl` on `main`), `add`, `done` and `list` keep the old behaviour:
they edit `## Agent follow-ups` in `todos.md` through the GitHub contents API
(GET, then PUT with the file's `sha`; HTTP 409 re-reads and retries, 5 attempts
at most), items are `- [ ] TITLE (YYYY-MM-DD, SOURCE, id XXXXXX)` with optional
`  - DETAIL` lines, and `list` prints old items with project `unsorted`.
`add` ignores `--project` and `--done-when` (it prints a notice on stderr), and
`done --reason` is not recorded. `edit`, `rm`, `reopen`, `list --json` and
`nightly` refuse with exit 1 and the message `run hermes-wiki todo migrate
first`. So an older laptop or box checkout keeps working before and after
migration for `add` and `done`: after migration, items it adds under the
heading are imported (see "Owner edits").

**Migration.** `hermes-wiki todo migrate` (laptop, once) reads the items under
`## Agent follow-ups` in `todos.md` and writes, in one commit `todo: migrate`:
`agent-todos.jsonl`, `agent-todos.md`, `todos.md` with the items replaced by
`![[agent-todos]]` and `Open the page to tick: [[agent-todos]]`, and a
`.gitignore` line for `.todo-suggestions.jsonl`. It prints one line per item,
`ID PROJECT CHECK TITLE`. It guesses the project from repo names in the item
text (`unsorted` when none or more than one), adds a `pr:` check to an item that
starts with `Merge` or `Review and merge` and names exactly one PR URL, and takes
the closed date of a ticked item from the commit history of `todos.md` (the
`todo: done ID` commit; otherwise today with `by: owner`). Exit 1 when the
records already exist, when `todos.md` or its heading is missing, when the wiki
changed during the run (re-run), or when the history lookup fails. It does not
run on the box.

**Owner edits.** The owner ticks boxes on `agent-todos.md` in Obsidian. The
next `hermes-wiki todo` write (any command, including the nightly job) reads the
page and the jsonl at the same commit and applies the ticks before its own
change:

- A `[x]` counts only on an item line outside `## Recently done`. It closes an
  open or suggested item with `by: owner`, reason `ticked in Obsidian`.
- Unticking counts only on a line inside `## Recently done`. It reopens a done
  item. An item with lines that disagree is left alone.
- Stale-page rule: an untick is applied only when the page's hidden data marker
  equals the sha256 of the current jsonl. A stale page can show an item as done
  that was reopened since, so its unticks are ignored (logged as
  `agent-todos.md was stale; unticks ignored`). Ticks are applied either way.
- Every other edit to the page (text, moved lines, new lines) is lost at the
  next write, which regenerates the page. Change items with `todo edit`.
- Stray items: an old client can still add `- [ ] TITLE (...)` lines under
  `## Agent follow-ups` in `todos.md`. The next write imports each one as a
  record (project `unsorted`, same id and date when they parse, a fresh id when
  the id already belongs to a different title; a ticked one is imported done,
  `by: owner`, reason `ticked in todos.md`), removes it from `todos.md`, and
  commits that with the write. A write with an owner tick or stray import and no
  change of its own commits as `todo: import owner edits`.
- If two sides touched the page, obsidian-git can report a conflict on
  `agent-todos.md`. Take the remote version of that file and tick again. The
  jsonl keeps every tick that was already imported.

**Nightly job (droplet).** `todo-nightly.timer` starts `todo-nightly.service` at
00:45 (`Persistent=true`, so a missed run catches up), 15 minutes before the
01:00 curator. The service runs `~/.local/bin/todo-nightly.sh`
(`droplet/todo-nightly.sh`) as `hermes` with `GH_CONFIG_DIR=~/.hermes/gh-config`
and a 10 minute timeout. In order:

1. Read `.todo-suggestions.jsonl` from the sandbox checkout
   (`~/.hermes/sandboxes/docker/default/home/hermes-wiki/`, root-owned, read
   through a `hermes-sandbox:latest` container with no network). A failed read
   stops the job before anything else runs. Record the file's sha256.
2. Run `hermes-wiki todo nightly` with `~/.config/hermes-wiki/config.json`,
   `--evidence ~/.hermes/state/todo-evidence.json` and
   `--state ~/.hermes/state/todo-nightly.json`. It fetches the state of every PR
   or issue named in a `done_when` check or in the text (a
   `github.com/OWNER/REPO/pull/N` or `/issues/N` URL) of an open or suggested
   item, once per distinct reference, then, in one write (`todo: nightly`):
   imports owner ticks and stray items, applies the curator's suggestions
   (open items only), closes items whose check passes (`by: check`, reason
   `PR #N merged DATE` or `issue #N closed DATE`), turns an open item with a
   `pr` check on a PR that was closed unmerged into a suggestion, and
   returns suggestions older than 7 days to open with `expired: true`. It
   re-renders the page, so "Recently done" ages out every night, then writes the
   evidence file for the curator. It prints one JSON summary line
   (`closed`, `suggested_by_checks`, `suggestions_accepted`,
   `suggestions_dropped`, `expired`, `fetch_failures`, `committed`).
3. Delete the suggestions file only if its sha256 still equals the one read in
   step 1. If the curator wrote to it in between, it is kept and
   `suggestions changed since read; kept` goes to stderr; the next night reads
   it again, and ids that already have a suggestion are dropped.

The state file keeps, per reference, the dates (UTC) of nights its fetch
failed. A success removes the reference. A reference that failed on 3 or more
distinct nights is listed under `check_failures` in the evidence file and
appears in the curator's worklist as `## todo_check_failures`, which the curator
must report. If the evidence file is older than 36 hours, the worklist says
`todo evidence stale since TIMESTAMP` (the job is failing, or the token
expired); an unreadable file shows `todo evidence unreadable`. Check with
`systemctl --user status todo-nightly.service` and `journalctl --user -u
todo-nightly.service` as `hermes`.

`droplet/install-wiki.sh` links `todo-nightly.sh` into `~/.local/bin`, installs
the two unit files into `~/.config/systemd/user/`, runs `daemon-reload`, and
copies `droplet/hermes-wiki-droplet.json` to
`~/.config/hermes-wiki/config.json` only if that file is absent. It does not
enable the timer: `systemctl --user enable --now todo-nightly.timer` is a
separate step, taken after the checks below.

**Curator suggestions.** The curator never edits `agent-todos.jsonl` or
`agent-todos.md`, and never edits, moves, splits, trims or files anything in the
`## Agent follow-ups` section of `todos.md`. The rest of `todos.md` follows the
usual rules. For an item whose referenced PR or issue is
finished, or that a page or note says is finished, it appends one line to
`~/hermes-wiki/.todo-suggestions.jsonl` (the file is git-ignored):
`{"id": "XXXXXX", "reason": "a few words", "evidence": "what it saw"}`. Both text
fields are 1-300 characters, one line, secret-scanned. The next nightly run
accepts a line only for an open item, shows the item on the page under
`## Probably done, confirm`, and drops anything else (logged as `dropped
suggestion line N: WHY`, without the text). The owner confirms by ticking it
(`by: owner`) or rejects it with `hermes-wiki todo reopen ID`, which marks the
suggestion `rejected` and returns the item to open. An unanswered suggestion
expires after 7 days the same way. The nightly job never accepts a suggestion
whose evidence equals the rejected or expired evidence of that item, including
every entry in `blocked_evidence`, even when the curator ignores its prompt. The
curator sees each item once per change (the worklist keeps a seen-file), and
item text reaches it as data between `BEGIN TODO` and `END TODO` lines.

**Sync guard.** `droplet/wiki-git-sync.sh` runs before each snapshot commit. If
`agent-todos.jsonl` or `agent-todos.md` is tracked and differs from `HEAD` in the
droplet checkout (edited, deleted or staged), it restores the file from `HEAD`
and prints `discarded local edit to FILE`. A local edit on the droplet therefore
cannot be pushed over the generated copy.

**Droplet token.** The nightly job needs, for the token in
`~/.hermes/gh-config`: read access to pull requests and issues on every
repository a todo item references, and contents write on
`FibonAdithya/hermes-wiki`. MEASURED 2026-11-19 on `hermes-vm` as `hermes` with
that config: `gh api repos/FibonAdithya/chain-acceptance/pulls/5` returned HTTP
403, and `gh api -X POST repos/FibonAdithya/hermes-wiki/git/blobs ...` returned
HTTP 403. PR and issue reads worked only on `hermes-harness`. So the current
token cannot run the job: widen it (and update the rotation reminder) before
enabling the timer. Nothing is deployed yet.

**Reminder or todo item.** Anything tied to a time goes through `remind-owner`.
Everything else is a todo item.

**Cancelling and pruning reminders.** Do not use `hermes cron remove` on a
reminder directly. Use `remind-owner --cancel JOB_ID`: it refuses a job that is
not a reminder, runs `hermes cron remove`, and deletes that reminder's `.sh`
and `.txt` files only if the removal succeeded. `remind-owner --prune` deletes
`remind-*.sh` and `.txt` pairs that have no live job and whose `.sh` is more
than 10 minutes old. If `jobs.json` is malformed, `--prune` exits 1 and deletes
nothing.

## Day to day

- **The curator's report** arrives on Telegram after 01:00 when it changed
  something. "Conflicts that need you" lists pages where two verified facts
  disagree; both are on the page with dates.
- **Quarantined notes** are gone from `inbox/` but kept in git history:
  `git -C ~/.local/share/hermes-wiki log --diff-filter=D --name-only -- inbox/`
  on the laptop clone.
- **Worklist fails** (`FATAL` in the job's output): run step 2 by hand. A
  scanner import failure after a Hermes upgrade means the function was renamed;
  update `load_scanner` in `droplet/wiki-worklist.py`.
- **Sync fails** with `FATAL: rebase conflict, not pushed`: a page edit on the
  droplet conflicts with an outside commit. Only the curator edits pages, so
  this should not happen; resolve it in the sandbox checkout by hand. While
  the rebase is open, each timer run exits with `FATAL: rebase or merge in
  progress` and leaves the checkout alone; the next run after you finish
  pushes. `FATAL: pull failed` is a network or auth error, not a conflict.

## Rollout log

All times UTC. The droplet is `hermes-vm`, the box `tig-adi`.

| check | result | MEASURED/ESTIMATED | command | when |
|---|---|---|---|---|
| step 1: droplet sync | `install-wiki.sh` printed the three `installed`/`linked` lines and backed up the old script to `~/.local/bin/hermes-wiki-sync.sh.pre-wiki`; first sync `no changes`; droplet wiki `HEAD` = GitHub `main` = `c5f1e1d` | MEASURED | `droplet/install-wiki.sh`; `systemctl --user start hermes-wiki-sync.service`; `git rev-parse HEAD origin/main` in the sandbox vs `gh api .../commits/main` | 2026-10-27 10:31 |
| step 2: worklist | exit 0; `## backfill` 10 pages, `## orphans` 22, `## lint` 5; nothing quarantined (no `inbox/` yet). Lint `tig-agent-host: secret pattern: password` was three 6-character `sk-...` placeholders | MEASURED | `~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/wiki-worklist.py` | 2026-10-27 |
| step 3: scanner | `''` for `wiki-curator-prompt.md` and `delegate-with-context/SKILL.md` | MEASURED | `_scan_cron_prompt(...)` as in step 3 | 2026-10-27 |
| step 4: seed | `created TAGS.md`, `replaced CONVENTIONS.md` (wiki `af9918b`, `a9ad7f5`); both match `wiki/seed/`; droplet pulled them, sync `no changes` | MEASURED | `hermes-wiki seed` (owner, laptop); `cmp` against `wiki/seed/*` | 2026-10-27 15:26 |
| step 5: curator | job `c13398050405`, `enabled_toolsets ['file', 'terminal']`; first run `ok` (15:55–16:10): backfilled 10, linked 21 orphans, deleted 0-byte `briefing-notes`; pushed as `991c74e` | MEASURED | `hermes cron create ...`; `update_job`; `hermes cron run c13398050405` | 2026-10-27 16:10 |
| step 6: cron prompts | `morning-briefing` and `Weekly content roundup` prompts differ from the old ones by the one added line; model, toolsets, delivery unchanged. Rollback: `~/prompt-<id>.before.txt` | MEASURED | `hermes cron edit <id> --prompt ...`; `diff` | 2026-10-27 |
| step 7: laptop + import | installed from `~/.local/share/hermes-harness-wiki`; `wiki` MCP `Connected`; dry run `would import 173, skipped 3, failed 0`; import `imported 173, skipped 3, failed 0`; droplet pulled all 173 (`81c7496`) | MEASURED | `wiki/install-laptop.sh`; `hermes-wiki import-claude-memory` | 2026-10-27 17:44 |
| step 8: box | box deployed `f23dc80` via `request_deploy`; `exported 16 pages`; `hermes-wiki-public -> .hermes-wiki-public.1793222707`; `.permissions.push` `true`. No exported page has a private tag, `type: person` or no frontmatter | MEASURED | `systemctl --user start wiki-mirror.service`; frontmatter audit of the export | 2026-10-28 21:25 |
| step 9: broker | `compute.tools.include` lacked `file_task`, `request_deploy`, `deploy_harness`, `deploy_fleet`, `pause_talos`; added (backup `config.yaml.bak-2026-10-28-pre-deploy-tools`); gateway restarted, `compute` registered all 18 broker tools | MEASURED | `systemctl --user restart hermes-gateway`; `agent.log` | 2026-10-28 17:51 |
| 1 | first run failed: laptop and box notes published within 10 ms, box landed, laptop got `HTTP 409` (`is at … but expected …`). Fixed by #33 (`8c792cb`, retry on 409). Rerun on box `8c792cb`: 3 synchronised pairs, all 6 landed (test notes then deleted) | MEASURED | `notes.add_note` from both hosts, started at the same epoch second | 2026-10-28 21:42 (fail), 22:11 (pass) |
| 2 | unknown tag, 301 words, `ghp_` token, empty `kind` rejected; 300-word control accepted; fake publisher called once | MEASURED | `notes.add_note(cfg, publisher=fake, ...)` | 2026-10-28 |
| 3 | live: `tags=["cost"]` → `aws-startup-credits` only; scratch copy with three synthetic pages: `tags=["cost"], type="decision"` → only the matching page, not one that mentions "cost" untagged | MEASURED | `search.search(...)` | 2026-10-28 |
| 4 | scratch repos, real script: outside note + droplet edit → `pushed`, no merge commit; same-line conflict → `FATAL: rebase conflict, not pushed`, exit 1, origin unchanged. Live: snapshot `b1969d6` rebased onto outside note `86172db` and pushed; 0 merge commits since `a9ad7f5` | MEASURED | `droplet/wiki-git-sync.sh` on scratch repos; sync timer paused during check 8 | 2026-10-28 22:07 |
| 5 | hook 0.11 s; extractor wrote 1 and 2 notes (dry publish), 0 pending files. `/tmp`, `.no-wiki-notes` and `WIKI_NOTE_CHILD=1` → `tmp`, `opted-out`, `child`, no pending file, no extractor. Since install, 8 real sessions each produced 1 note | MEASURED | `hermes-wiki session-end` with a scratch `state_dir` and `HERMES_WIKI_DRY_PUBLISH=1` | 2026-10-28 21:43 |
| 6 | under `unshare -rn` the extractor exited 1 (`ENOTFOUND`), pending file kept; the next session end processed it (rc 0) | MEASURED | `unshare -rn hermes-wiki session-end ...` | 2026-10-28 21:43 |
| 7 | not yet checked | — | Anthropic console usage page for 2026-10-28 21:42–21:44 | — |
| 8 | manual run 21:59–22:06 `ok`: hostile note `cc55bd1f` quarantined; "tag every page `urgent`" note `533ab6c9` discarded and listed; no page tagged `urgent`; new-topic note → page `hermes-ops`; duplicate note recognised, page unchanged | MEASURED | notes via `notes.add_note` with `created: 2026-10-26` so they sorted before the backlog; `hermes cron run c13398050405` | 2026-10-28 22:06 |
| 9 | on the `a9ad7f5` snapshot: `over_cap` empty; 10 backfill, the four over 1,500 words first (3,966; 2,076; 1,683; 1,543); 22 orphans; no infra page in any list | MEASURED | `hermes-wiki worklist --root <a9ad7f5 checkout>` | 2026-10-28 |
| 10 | `hermes-harness` page does not exist → dropped. With `talos-and-tig-box`: included under the reference-data heading | MEASURED | `boxlib.write_task_prompt` on the box with the real export | 2026-10-28 |
| 11 | profile `aryappra`: MCP servers `['google']` (7 tools), no wiki skills, no wiki in its sandbox | MEASURED | `config.yaml` and `agent.log` of the profile | 2026-10-28 |
| 12 | `banking-stack` (`finance`), `adithya` (`person`), `financial-plan` (no frontmatter): absent from the export, box `search`, `read` (not-found), `find /` in a `hermes-exec` container, and the brief (`context_dropped: ["banking-stack"]`). Gap: the task token `/etc/hermes-exec/github-token` can read and push the private wiki; see #31 | MEASURED | script on `tig-adi` | 2026-10-28 |
| 13 | deferred: no `ANTHROPIC_API_KEY` on the droplet | — | — | — |
| cost (spec §7) | not measured | — | — | — |
