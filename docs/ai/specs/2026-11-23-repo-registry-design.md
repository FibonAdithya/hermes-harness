# Repo registry: a generated wiki page every agent can read

Date: 2026-11-23. Status: draft, awaiting owner approval. Builder check done 2026-11-23.
Source of the idea: wiki `home-repo-pattern.md`, TODO 1 ("explicit repo-registry
section to agent steering: one entry per repo, path, purpose, commit-hotness").

This repo is public. Names of private repos, private wiki pages and other people's
accounts are redacted as `<finance-repo-1>`, `<people-page>`, `<collaborator>` and so on;
the real names live only in the laptop's overrides file and the private wiki.

## Intent

Agents working for the owner (Claude Code on the laptop, Codex on the laptop,
Hermes on hermes-vm, the research partner on tig-adi) do not know which repos
exist, what each is for, where it is checked out on the machine they run on, or
which repos are in active use. They find out by scanning or by asking. A
navigation list fixes that, but it has to:

- be readable by any agent, not only Claude Code (owner's requirement), so it
  is plain Markdown in the wiki, not a Claude-specific file;
- give the right path on each machine, since paths differ per machine;
- be generated, not hand-maintained (the owner prefers auto-discovery; the
  hand-written repo claim in Hermes's `SOUL.md` was false for ~3 months, see
  wiki `hermes-sync.md`).

## Facts behind this (MEASURED 2026-11-23 unless marked)

| claim | value | command |
|---|---|---|
| git repos under laptop `~` (depth 3) | ~50, incl. third-party clones (nvm, gbrain, LibreSignal, tokenmaxxer, formuevo, self-evolving-hackathon) | `find ~ -maxdepth 3 -name .git` |
| repos owned by FibonAdithya on GitHub | 42 | `gh repo list FibonAdithya --limit 200 --json name -q length` |
| local repos with no `origin` | 8 (MIP_gen, AIDDAxHiGHS_Hack, ai-agent-guide, echo_ops, company_brain, <finance-repo-3>, WarhammerResale, hackathons/self-evolving-ai-2026) | same scan, `git remote get-url origin` |
| local HEAD commit count misleads | `~/hermes-harness` shows 0 commits in 30 days because it is checked out on stale `feat/agent-todos`; master has many | `git rev-list --count --since=30.days HEAD` |
| droplet mirrors | 42 dirs under `~/.hermes/sandboxes/docker/default/home/repos` on hermes-vm, seen by Hermes as `/root/repos/<name>` | `ssh hermes-vm 'ls ~/.hermes/sandboxes/docker/default/home/repos \| wc -l'` |
| Hermes's wiki path | `/root/hermes-wiki` (sandbox home also has `hermes-wiki/`) | same ssh `ls` |
| tig-adi | home `/home/adi`, 8 repos at `~/*/.git` or `~/TIG/*/.git`; partner reads the public export at `~/TIG/hermes-wiki-public` (wiki `research-assistant.md`) | `ssh tig-adi 'ls -d ~/TIG/*/.git ~/*/.git \| wc -l'` |
| wiki pages that name repos in frontmatter | 50 (`repos: [chain]` etc.) | `grep -l '^repos:' ~/hermes-wiki/*.md \| wc -l` |
| pages naming finance repos | `<finance-page>.md` (finance, investing), `<investing-page>.md` (investing, names <finance-repo-2>); `fleet.md` (public) also names <finance-repo-1> | grep of frontmatter |
| Codex global instructions | `~/.codex/AGENTS.md` does not exist | `ls ~/.codex/AGENTS.md` |

## Goals

- One page, `repos.md`, in `FibonAdithya/hermes-wiki`, listing each repo with:
  name, GitHub slug (or "local only"), purpose, a link to its wiki page if one
  exists, commits on the default branch in the last 30 days, date of the last
  commit, and its path on each machine (laptop, hermes-vm, tig-adi), blank where
  it is not checked out.
- Finance/investing repos go on a second page, `repos-private.md`, tagged
  `finance`, so the public export (and so the tig-adi partner) never sees them.
- Rebuilt daily without anyone acting. Re-running with nothing changed makes no
  commit.
- Each agent's global steering file gets one pointer line to the page path on
  its machine.

## Non-goals

- No per-repo `AGENTS.md` files and no edits inside other repos.
- No hand-written fields beyond a small overrides file (below). In particular no
  "where skill changes go" column in this version; it can be added later as an
  override field.
- No live service, no board integration (research-assistant stays separate).
- Not a mirror or clone tool: it reports where repos are, it does not fetch
  missing ones.

## Anti-goals

- A finance repo's name or purpose appearing on the public page, or anywhere
  in the public export.
- A page that silently drops a machine's paths because the machine was
  unreachable that day.
- Daily churn commits when nothing changed.

## Approaches considered

- **A. `hermes-wiki repos` subcommand run daily on the laptop (chosen by owner
  2026-11-23).** The laptop is the only machine with full `gh` access and ssh to
  both hermes-vm and tig-adi. Reuses the wiki package's GitHub contents-API
  write path (`todos.GhContents` / `update`, sha-conditional with retry), so no
  local wiki clone is touched.
- B. A new source in research-assistant's probe. Rejected: ties the registry to
  the board's service and has a status tool writing to the wiki.
- C. Each machine writes its own section. Rejected: three implementations and
  write conflicts on one file.

## Design

### Where the code goes

`wiki/hermes_wiki/repos.py` (collection + rendering, pure functions where
possible) and a `repos` subcommand registered in `wiki/hermes_wiki/cli.py`, in
hermes-harness. Python 3.11 stdlib only (`tomllib` for the overrides file).
`todos.update()` is hard-wired to `todos.md` (`store.get(PATH)`,
`store.put(PATH, …)`); give it a `path` parameter defaulting to the current one
rather than copying the loop. Shell-outs (`gh`, `git`, `ssh`) go through an injected runner so tests use
fixtures and never call the network (wiki memory: inject, don't monkeypatch).

### Which repos are listed

A repo is listed if any of these holds, then the exclusions apply:

1. owned by an allowlisted GitHub owner (default `FibonAdithya`) and not a fork
   and not archived;
2. checked out on any of the three machines with an `origin` whose owner is in
   the allowlist (default `FibonAdithya`, `tig-foundation`, `AIDDA-Institute`);
3. checked out with no `origin` (listed as "local only");
4. named in `include` in the overrides file.

Excluded, whichever rule matched: forks and archived repos (by GitHub
metadata), and anything named in `exclude`; `include` overrides both. Clones of
other owners' repos (e.g. `nvm-sh/nvm`) are not listed unless included.
hermes-vm mirrors supply paths only; they never make a repo listed, because
`droplet/repo-sync.sh` mirrors every owned repo, forks included. (Builder-check
call, 2026-11-23. MEASURED: forks/archived among FibonAdithya repos today are
tokenmaxxer, self-evolving-hackathon, tig-monorepo.)

Identity: a repo is keyed by its GitHub slug (`owner/name`); a local-only repo
by `local:<dirname>`. Two laptop checkouts of the same slug (e.g. `~/TIG/Talos`
and `~/talos-c3`, both `FibonAdithya/talos`) are one row with both paths.

### Columns

- **purpose**: first sentence (max 160 chars) of the first body paragraph of
  the wiki page whose `repos:` frontmatter names the repo by its GitHub name;
  else the GitHub description; else the first non-heading line of the README at
  any local checkout; else `(no description)`. If several pages name it, use
  the page whose filename matches the repo name, else the first alphabetically,
  and link all of them.
- **activity / last commit**: from the default branch on GitHub (one
  GraphQL query per owner where possible; `history(since:)` totalCount and the
  head commit date). Rendered as a band, not the raw count: `hot` (20+ commits
  in 30 days), `active` (1-19), `dormant` (0), plus the last commit date (day).
  A raw count drops every day as old commits leave the window, which would
  rewrite the page daily with nothing new (builder-check call).
  If one repo's lookup fails (renamed, deleted, not visible to the token), the
  row keeps its local data and shows `lookup failed` in the activity column;
  only a failure of the owner-level listing aborts the run (builder-check
  call). Local-only repos: `git rev-list --count --since=30.days`
  on `main` if it exists, else `master`, else `HEAD`, on the laptop checkout
  (or the first machine that has it).
- **paths**: laptop from a local scan of `~` (depth 3, skipping `.cache`,
  `node_modules`, `.local/share/Trash`, `.worktrees`); tig-adi from one
  `ssh tig-adi` running the same scan with a 20 s timeout; hermes-vm from one
  `ssh hermes-vm` listing the sandbox `repos/` dir, rendered as
  `/root/repos/<name>`.
- Rows sorted by band (hot, active, dormant), then last commit date, newest first.

### Privacy split

A repo goes to `repos-private.md` instead of `repos.md` if any wiki page whose
`repos:` names it is private under `schema.is_private` (so `<finance-repo-1>` and
`<finance-repo-2>` today), or it is named in `private` in the overrides file
(initially `<finance-repo-3>`, which has no page). Fail closed: if the wiki pages
cannot be read, the run aborts and writes nothing.

GitHub visibility does not decide the split (owner decision, 2026-11-23). The
"public" export is the copy on tig-adi, not the internet, and many GitHub-private
repos (MIP_gen, research-assistant, fleet-private) are already named on public
pages. MEASURED 2026-11-23: this rule puts 4 repos on the private page:
<finance-repo-1> (`<finance-page>.md`, finance), <finance-repo-2> (`<investing-page>.md`,
investing), <people-repo> (`<people-page>.md`, people) and <finance-repo-3>
(override). Splitting by GitHub visibility would have moved 26 owned repos.

**RETRACTED 2026-11-23 (during implementation): the "4 repos" count above is
wrong.** `<people-repo>` is `<collaborator>/<people-repo>`, a collaborator
repo, not owned and not checked out anywhere, so it is not a row at all. The
count came from a GitHub listing that included collaborator repos (GraphQL
`repositories` without `ownerAffiliations: [OWNER]`, which returned 53 = 42
owned + 11 others). MEASURED with a read-only end-to-end render at that point
in the branch: 3 private rows (<finance-repo-1>, <finance-repo-2>, local:<finance-repo-3>).
A repo named only by a private page still counts as a private name for the
scrub and the final check, even when it is not a row, so `<people-repo>`
can never appear on the public page.

Text check (builder-check call): a purpose string for a public row that
contains a private row's name is replaced by the next fallback source, then by
`(no description)`. Before writing, the full rendered public page is checked
for every private row's name (case-insensitive, word boundary); a hit aborts the
run. Existing wiki pages that already mention a private repo (e.g. public
`fleet.md` names <finance-repo-1> in its `repos:`) are out of scope; the generator
does not edit other pages.

`repos-private.md` frontmatter: `type: log`, `tags: [coding, finance]`.
`repos.md`: `type: log`, `tags: [coding]`. `type: log` keeps both out of the
curator's word-cap list (`worklist.build` skips logs), like `agent-todos.md`.
Both start with an HTML comment: `<!-- Generated by hermes-wiki repos. Edits are
lost; change ~/.config/hermes-wiki/repos.toml instead. -->`. The public page
ends with a line pointing to `[[repos-private]]`; the export strips that link
when the page is private, which is what we want.

### Unreachable machines

If `ssh tig-adi` or `ssh hermes-vm` fails, that machine's paths are taken from
the previous run's state file (`<state_dir>/repos-state.json`, laptop-local,
never committed) and the page header says `tig-adi paths as of <date>
(unreachable today)`. If there is no previous state, the column says
`unknown`. GitHub unreachable: abort, write nothing (counts would be wrong).

### Writing

Render both pages, compare with the current file contents fetched through the
contents API, and PUT only the ones whose body (excluding the `updated:` line)
changed. Commit message `repos: refresh`. Uses the same retry-on-conflict loop
as `hermes-wiki todo`. `--dry-run` prints both pages to stdout and writes
nothing. Secret scan (`secretscan.find_secrets`) runs on both bodies before any
write; a hit aborts.

### Overrides file

`~/.config/hermes-wiki/repos.toml`, required (changed during implementation,
2026-11-23: a missing file exits 2, so a private repo with no wiki page cannot reach the
public page because the file was absent; the file is laptop-local and never committed,
and the installer seeds it with `private = []`):

```toml
owners = ["FibonAdithya"]                                   # rule 1
clone_owners = ["FibonAdithya", "tig-foundation", "AIDDA-Institute"]  # rule 2
include = []      # slugs or local:<dir> to force-list
exclude = []      # slugs or local:<dir> to drop
private = ["local:<finance-repo-3>"]
[purpose]         # per-repo purpose override, slug = "text"
```

### Schedule

A systemd user timer on the laptop, daily at 08:00 local, `Persistent=true` so
a missed run happens at next boot. Unit files live in `wiki/` next to
`install-laptop.sh`, which installs them.

### Pointers in steering files (rollout, by hand, once)

One line each, same wording:

> Repo registry (what each repo is for, where it is checked out on this machine,
> how active it is): `<path to repos.md>`. Read it before searching the disk for
> a repo.

- Claude Code, laptop: `~/.claude/CLAUDE.md` → `~/hermes-wiki/repos.md`.
- Codex, laptop: create `~/.codex/AGENTS.md` with that line.
- Hermes, hermes-vm: `~/.hermes/SOUL.md` → `/root/hermes-wiki/repos.md`, and
  remove SOUL.md's existing hand-written repo claims. SOUL.md has been blocked
  by Hermes's injection scanner before (wiki memory, 2026-10-19); check its
  blocked-lines log after the edit.
- Partner, tig-adi: `~/.ra/partner/prompt.md` →
  `~/TIG/hermes-wiki-public/repos.md` (restart the partner loop by exact PID
  per research-assistant's gotchas).
- `index.md`: add `[[repos]]` and `[[repos-private]]` lines (else the curator's
  lint reports "missing from index.md").

## Tests (each names the mutation it catches)

- Listing rules on a fixture: a fork, an archived repo, a `nvm-sh/nvm` clone, a
  local-only repo, an included and an excluded slug. Catches: dropping any one
  rule or the exclude step.
- Two checkouts of one slug render as one row with two laptop paths. Catches:
  keying by directory name.
- Commit count comes from the default-branch fixture, not local HEAD: fixture
  where local HEAD count is 0 and GitHub says 37 → row shows 37.
- Privacy: a repo named by one public and one `finance` page lands only on the
  private page, and its name and purpose are absent from the public page text.
  Catches: using "all pages private" instead of "any page private".
- Unreadable wiki → no write. Catches: fail-open.
- tig-adi unreachable with previous state → old paths plus "as of" header; with
  no state → `unknown`. Catches: silently empty column.
- Unchanged input → zero PUTs; a new last-commit date or a band change → one
  PUT; a 30-day count that drops within its band → zero PUTs. Catches: churn
  commits and missed updates.
- A public row whose GitHub description names a private repo renders with the
  fallback purpose, and a public page that still contains a private name aborts
  with zero PUTs. Catches: a row-only split.
- One repo's lookup fails → its row shows `lookup failed`, other rows normal,
  page written. Catches: one bad clone blocking the whole registry.
- A fork that is checked out on the laptop is not listed; the same fork in
  `include` is. Catches: rule 2 re-adding forks.
- Secret pattern in a purpose string → abort, zero PUTs.

## Left to the builder

- GraphQL vs REST for counts, and how to page through owners.
- Exact table layout (one table, or one per hotness band) as long as all
  columns above are present.
- How the tig-adi scan is shipped over ssh (inline script vs `find` command).

## Rollout

1. Merge; install on the laptop (`wiki/install-laptop.sh`).
2. `hermes-wiki repos --dry-run`; owner reads both pages.
3. First real run; check both pages in Obsidian and that `repos-private` is
   absent from `~/TIG/hermes-wiki-public` on tig-adi after the next export.
4. Add the steering-file pointers and index lines.
5. Enable the timer; confirm the next run with no new default-branch commits
   and no band change made no commit.
