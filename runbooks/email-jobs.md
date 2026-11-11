# Email jobs runbook: skip emails already read

Everything below runs on the droplet as the `hermes` user (see the top of
`runbooks/tuner.md` for switching users from the console).

## What it does

Two cron jobs read email in full:

| Job | Id | Reads |
|---|---|---|
| `morning-briefing` | `3bc4e11d29b1` | the 20 newest inbox emails, 08:00 |
| `Nightly calendar sync from email` | `cc68e24f21cd` | every inbox email of the last 7 days, 06:00 |

Without a record of earlier runs, each job read the same emails again: kept
emails stay in the inbox for the briefing, and calendar sync sees each email on
up to 7 nights. Over 2026-10-28..11-11 (MEASURED 2026-11-11, tuner `History`
over `state.db`), 56 of the briefing's 178 reads and 85 of calendar sync's 138
were of an email the same job had read in its previous 7 runs.

Each job now has a pre-run script, `~/.hermes/scripts/already-read-<job id>.py`
(a copy of `droplet/already-read.py`). Hermes runs it before the job and puts
its output at the top of the prompt as a "Script Output" section: the ids of the
emails this job read in full in its finished runs of the last 14 days. The
prompts (`droplet/prompts/`) tell the model not to read those again:

- the briefing lists them under "Still in inbox from earlier days", using the
  sender, subject and date from the search result (`include_headers`), and
  takes no action on them;
- calendar sync skips them, and calls `get_events` only for the dates of new
  candidate events.

A read counts only if its result succeeded and the run ended with a final
answer, so a failed read or a run that broke off is retried. If the script
fails, Hermes puts its error in the prompt instead, and both prompts fall back
to reading everything and say so in one line.

Trade-off: calendar sync used to see each email up to 7 times, so an event it
missed on the first night could be picked up later. Now it reads each email
once. An email the briefing decided to delete whose delete call failed shows
under "Still in inbox from earlier days" instead of being retried.

The live prompts are in `~/.hermes/cron/jobs.json`; the tuner may change them
later (its rollbacks restore only the fields its own fix wrote, so the `script`
field stays). `droplet/prompts/` is the text applied by this runbook.

## Install (once)

```bash
cd ~/hermes-harness && git fetch -q origin && git checkout -q master && git reset -q --hard origin/master
for id in 3bc4e11d29b1 cc68e24f21cd; do
  install -m 644 droplet/already-read.py ~/.hermes/scripts/already-read-"$id".py
  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/already-read-"$id".py | head -1
  # Written only once, so re-running this step keeps the original prompt.
  [ -e ~/prompt-"$id".before.txt ] || python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); jobs=d.get("jobs", d) if isinstance(d, dict) else d; sys.stdout.write(next(j["prompt"] for j in jobs if j["id"] == sys.argv[2]))' ~/.hermes/cron/jobs.json "$id" > ~/prompt-"$id".before.txt
done
hermes cron edit 3bc4e11d29b1 --script already-read-3bc4e11d29b1.py --prompt "$(cat droplet/prompts/morning-briefing.md)"
hermes cron edit cc68e24f21cd --script already-read-cc68e24f21cd.py --prompt "$(cat droplet/prompts/calendar-sync.md)"
```

Expected: each script prints one line, `Already-read emails for cron job <id>:
N message ids ...` (2026-11-11: 122 for the briefing, 53 for calendar sync).
`~/prompt-<id>.before.txt` is the rollback copy; the step never overwrites one
that exists. `harness-pull` refreshes the
installed copies when `droplet/already-read.py` changes on master; it never
creates one.

## Check

After two runs of each job, re-run the repeat count (prints counts only;
`state.db` is email-derived, so print no result text):

```bash
cd ~/hermes-harness/tuner && .venv/bin/python - <<'EOF'
import time
from pathlib import Path
from hermes_tuner.history import History
GET = "mcp__google__get_gmail_message_content"
runs = History.open(Path.home() / ".hermes/state.db").sessions(time.time() - 3 * 86400)
for job in ("3bc4e11d29b1", "cc68e24f21cd"):
    seen = set()
    for s in sorted((s for s in runs if s.job_id == job), key=lambda s: s.started_at):
        refs = {c.ref for c in s.calls if c.name == GET and c.ref}
        print(job, time.strftime("%m-%d", time.localtime(s.started_at)),
              "read", len(refs), "read before", len(refs & seen))
        seen |= refs
EOF
```

Expected: `read before` is 0 for runs after the install, and the Telegram
briefing has the "Still in inbox from earlier days" table.

## Roll back

```bash
for id in 3bc4e11d29b1 cc68e24f21cd; do
  hermes cron edit "$id" --script "" --prompt "$(cat ~/prompt-"$id".before.txt)"
done
```

The script copies can stay; without the `script` field Hermes does not run them.
