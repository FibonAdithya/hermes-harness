# Wiki runbook

The wiki (`FibonAdithya/hermes-wiki`, branch `main`) is Hermes's long-term
memory. Hermes edits pages on the droplet; a timer (`hermes-wiki-sync.timer`)
pulls, commits and pushes every 15 minutes. Other agents (laptop and box Claude
Code sessions, the one-time memory import) only add notes under `inbox/`. A
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
| droplet | `wiki-curator` cron job, 03:00 | prompt in `droplet/wiki-curator-prompt.md` |
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
hermes cron create "0 3 * * *" "$(cat ~/hermes-harness/droplet/wiki-curator-prompt.md)" --name wiki-curator --script wiki-worklist.py --deliver telegram:8887384436 --model deepseek/deepseek-v4-pro --provider nous
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
`run_task`'s new `context_pages`) reloads on Hermes's next MCP restart; see
`runbooks/dispatch.md`.

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

## Day to day

- **The curator's report** arrives on Telegram after 03:00 when it changed
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

| check | result | MEASURED/ESTIMATED | command | when |
|---|---|---|---|---|
