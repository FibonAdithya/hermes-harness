# The approvals bot asks to deploy the box when it falls behind master

Date: 2026-11-13. Status: draft for owner approval.

## Intent

The box (tig-server) runs hermes-harness from `~/TIG/hermes-harness` and is
deployed only through the owner's gate. Something calls `request_deploy(sha)`,
the owner reads the commit and types `deploy <prefix>` in the approvals chat,
and `deploy_harness()` runs within 10 minutes (PR #14; `runbooks/dispatch.md`,
"Deploys"). Nothing calls `request_deploy` unless the owner asks Hermes to, so
the box drifts. On 2026-11-13 its stamp was `195a042` (MEASURED: `cat
~/.local/share/harness-deployed.sha` on tig-server) while master was `4774e23`,
so box sessions lacked `hermes-wiki todo` (PR #55).

The owner wants the approvals bot to ask him when the box is behind. **The gate
stays as it is: nothing is deployed until he types the commit prefix.** Only
the asking becomes automatic.

## Goals

1. When box's deployed hermes-harness commit differs from `origin/master` and
   master has been unchanged for 30 minutes, the approvals bot sends one
   message to the owner's approvals chat. The message says the box is behind,
   names both commits, lists the commits in between, and gives the reply to type.
2. The owner's reply `deploy <prefix>` (existing parsing, `parse_command`)
   approves it, and **the listener deploys it itself**, by calling the box's
   `deploy` verb with that sha, then replies in the chat with the verb's result.
   No Hermes turn and no model call is involved.
3. At most one message per master commit. Restarts do not cause re-asks
   (`droplet/harness-pull.sh` restarts `hermes-approvals` on every new master
   commit, so the listener restarts at exactly the moment a new ask may be due).

## Non-goals

- Fleet deploys (`deploy fleet <prefix>`). Same idea, but a separate target with
  its own stamp. It can follow once this has run for a while.
- Any automatic deploy without the owner's reply.
- Changing how the droplet deploys itself (its `harness-pull.timer`).
- Changing `request_deploy` / `deploy_harness` for agent-initiated deploys,
  except where the shared pending slot is described below.

## Design

### Reading the box's deployed commit: new ungated box verb `deployed`

`box/verbs/deployed` prints
`{"harness": "<sha or null>", "fleet": "<sha or null>"}` from
`~/.local/share/harness-deployed.sha` and `~/.local/share/fleet-deployed.sha`
(null when the file is missing or does not hold a 40-hex sha). It is
read-only, takes no arguments (consume stdin like `status` does), and is added
wherever the box's verb allow-list lives (check `box/dispatch` and the
broker's verb list; follow how `uptime` / `status` are registered).

Bootstrap: the box only has this verb after one deploy through the existing
path. Until then the verb call fails ("unknown verb" or similar). The
announcer then logs one warning per hour and sends nothing. The first deploy is
done by hand through `request_deploy` (rollout step 2).

### The announcer: `DeployAnnouncer` in `broker/hermes_broker/approvals.py`

It follows the existing announcer pattern (`tick(now)`, driven by the loop in
`run_listener` every `ANNOUNCE_EVERY` seconds).

On each tick:

1. Read master: `git rev-parse origin/master` in the droplet's checkout,
   through an injected runner. **The announcer never fetches and never writes to
   that checkout.** It is `~/hermes-harness` (MEASURED 2026-11-13: the
   `hermes-approvals` unit runs `uv run --directory
   /home/hermes/hermes-harness/broker`), the same checkout `harness-pull.sh`
   fetches and resets every 15 minutes, so its `origin/master` is at most about
   15 minutes old, which the 30-minute settle time covers. The path is a new
   `BrokerConfig` field, `harness_checkout`, defaulting to `~/hermes-harness`,
   with a test in `test_config.py`. Every git command the announcer runs is
   read-only (`rev-parse`, `log`, `diff`, `merge-base`). A git failure logs
   and skips the tick; a read that overlaps harness-pull's reset fails at
   worst, and the next tick retries.
2. Read the box: the `deployed` verb through the existing `call`. Failure
   logs (rate-limited as above) and skips.
3. If they are equal, do nothing. If the box's harness sha is null (no stamp,
   or a stamp that is not 40 hex characters), skip step 5's commit listing
   and docs-only check, and ask with the line `box: unknown (no deploy
   stamp)` in place of the box commit and the commit list. Settle time and
   once-per-commit still apply.
3a. **Never displace another request.** If `grants.json` holds an unexpired
   `pending_deploy` whose origin is not `"auto"`, or any live deploy grant (of
   either target), do nothing this tick and do not record `asked_sha`. The
   ask comes on a later tick, once that request is used or expired. If a
   `deploy_running` record exists (below), also do nothing.
4. **Settle time:** if master's commit time (`git log -1 --format=%ct`) is less
   than 30 minutes before `now`, do nothing yet. A burst of merges then gets
   one ask, for the last one.
5. **Docs-only skip:** if every path in `git diff --name-only <box>..<master>`
   is under `docs/`, do nothing. If the box's sha is not an ancestor of master
   (`git merge-base --is-ancestor`), do not skip, and say "not an ancestor of
   master" in the message instead of listing commits.
6. **Once per commit:** if the state file says this master sha was already
   asked, do nothing. Otherwise create the pending deploy (below), send the
   message, and record the sha only if the send succeeded.

State lives in a small JSON file in the broker's state directory (next to the
grants file; follow `_proposal_wiring` for how paths come from config). It
holds `{"asked_sha": "<sha>"}`, written atomically like `GrantStore._write`.

### The pending deploy

`GrantStore` keeps one `pending_deploy` slot. An auto-ask writes it through a
new keyword on `create_deploy_request`: `ttl_seconds` (default stays
`REQUEST_TTL_SECONDS` = 120) and `origin` (`"agent"` default, or `"auto"`).
An auto-ask uses a TTL of 24 hours. The owner may read the commits hours
later; the TTL bounds how long a typed prefix can still act. The grant that
follows approval keeps `DEPLOY_GRANT_MINUTES` = 10.

The pending slot holds one request. An agent's `request_deploy` still
replaces a pending auto-ask; the next auto-ask is only for a newer master sha.
That is acceptable, and the runbook says so. The reverse never happens (step
3a).

### Approval and deploy

In `handle()` (`verb == "deploy"`), the pending request's origin decides.

- `origin == "agent"` (or absent, for requests written before this change):
  unchanged. `approve_deploy` writes the grant and Hermes is prodded as today.
- `origin == "auto"`: a new `GrantStore` method approves and consumes in **one
  write**. It checks the prefix and target exactly as `approve_deploy` does,
  pops the pending request, and returns the sha **without writing any grant**.
  No grant ever exists for an auto-ask, so the MCP server's `deploy_harness()`
  cannot pick it up, and there is no race between two processes. The listener
  then calls the box `deploy` verb with `{"sha": sha, "target": "harness"}`,
  `timeout=900`, the same call `server.deploy_harness` makes. It replies
  `tig-server: deployed <sha12>` or the verb's error text.

The deploy can take minutes (`box/install.sh`). It must not stop the listener
from answering other updates in the meantime: run it in a worker thread and
send its result as a separate message when it finishes. The builder chooses
the mechanism, but one deploy at a time; a second approval while one is
running gets "a deploy is already running".

The box verb already refuses when master moved past the approved sha ("request
a new deploy"). That text is relayed as is, and the next tick asks again for
the new master after its settle time.

### Surviving a listener restart during a deploy

`harness-pull.sh` restarts `hermes-approvals` on each new master commit, so a
merge can land while a deploy is running.

- Before calling the box, the listener writes `"deploy_running": {"sha": ...,
  "started_at": ...}` to its state file. It clears the record when the call
  returns, after sending the result.
- `droplet/harness-pull.sh` does not restart `hermes-approvals` while that
  record exists and is younger than 20 minutes. It then also leaves its own
  stamp unwritten, so the next 15-minute tick retries the whole owner step.
  It reads the record with `python3 -c` against the state file's path; the
  builder chooses how it learns that path. A record older than 20 minutes is
  treated as stale: restart as usual.
- On start, a listener that finds a `deploy_running` record (it was killed
  anyway) calls the `deployed` verb on its first tick. It sends `tig-server:
  deployed <sha12>` if the stamp equals the recorded sha, otherwise `deploy
  of <sha12> did not finish; the next ask will offer it again`, and clears the
  record either way.
- `box/verbs/deploy` takes an exclusive non-blocking `flock` on
  `~/.local/share/deploy.lock` for the whole deploy (both targets) and refuses
  with "a deploy is already running" if it cannot get it. This also covers
  agent-initiated deploys.

### The message

```
Box is behind master.
box:    195a042c401f
master: 4774e2314f46 (merged 11:44 UTC)
7 commits:
  4774e23 Owner todo list: hermes-wiki todo add|done|list, ... (#55)
  ...
Read them, then reply: deploy 4774e2314f46
Valid 24 h. Ignore it to skip.
```

- Commit subjects come from `git log --format='%h %s' <box>..<master>`, at most
  15 lines, each cut to 100 characters, then "and N more".
- The reply line gives a 12-character prefix, as `request_deploy` does. The
  owner is still told to type it after reading, as the runbook says.
- Plain text, sent with the existing `send`. Telegram's 4096-character limit
  is never reached at these bounds.

## Tests

Each test names the mutation it catches. Use injected fakes for git, `call`,
`send` and the clock; never monkeypatch module defaults (a patched default once
reached the live wiki).

1. Equal shas: no send, no pending deploy. (Catches: asking when up to date.)
2. Behind, master committed 29 min ago: no send; at 31 min: one send.
   (Catches: missing or inverted settle check.)
3. Two ticks at the same master: one send. Then a new announcer on the same
   state file, which is the restart case: still no second send. (Catches:
   in-memory-only dedupe.)
4. Send fails: state not recorded, next tick sends. (Catches: recording before
   a successful send.)
5. Docs-only diff: no send. One path outside `docs/`: send. (Catches: `any`
   vs `all`.)
6. Box sha not an ancestor: send, with "not an ancestor" and no commit list.
7. `deployed` call fails: no send, no pending deploy, warning logged at most
   once per hour across 20 ticks. (Catches: log spam; asking blind.)
8. Auto pending deploy accepts `deploy <prefix>` 23 h later and refuses it at
   25 h. An agent request still expires at 120 s. (Catches: TTL applied to
   the wrong origin; default changed.)
9. Approval of an auto request calls the box `deploy` verb once with that sha
   and does not prod Hermes. Approval of an agent request prods Hermes and
   makes no box call. (Catches: origin not carried into the grant.)
10. While a deploy is running, another update is answered before the deploy
    returns, and a second auto approval gets "already running". (Catches: a
    blocking deploy in the poll loop.)
11. Message: 20 commits give 15 lines plus "and 5 more"; a 150-character
    subject is cut to 100. (Catches: unbounded message.)
12. `deployed` verb: missing files give nulls, a non-hex stamp gives null, and
    valid stamps are returned.
13. A live agent `pending_deploy` (harness, and separately fleet) or a live
    deploy grant: the tick sends nothing, leaves `grants.json` byte-identical,
    and records no `asked_sha`. After it expires, the next tick asks.
    (Catches: the announcer overwriting the shared slot.)
14. Approving an auto request leaves no `grants["deploy"]`, and
    `take_deploy_grant` returns None right after. (Catches: writing a grant
    that Hermes's `deploy_harness()` could take.)
15. Null box stamp: one ask containing "unknown (no deploy stamp)" and no
    commit list; no git diff/log is run against a null ref. (Catches: the
    null case falling into "git failure, skip" forever.)
16. Restart recovery: a state file with `deploy_running` for sha X and a box
    stamp of X gives "deployed X"; stamp Y gives "did not finish"; both clear
    the record. (Catches: the record never read on start.)
17. `harness-pull.sh` with a 5-minute-old `deploy_running` record does not run
    `systemctl --user restart hermes-approvals` and does not write its stamp.
    With a 25-minute-old record it does both. Use a stub `systemctl`/`git`/`uv`
    on PATH, as `box/tests/test_remind_owner.py` stubs `hermes`. (Catches: the
    restart killing a running deploy; a stale record blocking deploys forever.)
18. `box/verbs/deploy` with the lock held by another process refuses with
    "already running" and runs no git command. (Catches: a missing or
    blocking lock.)
19. The announcer's runner never receives a git command other than
    `rev-parse`, `log`, `diff` or `merge-base`. (Catches: a fetch or reset in
    the listener's own checkout.)

## Docs

- `runbooks/dispatch.md`, "Deploys": the bot now asks by itself when the box is
  behind (settle time, docs-only skip, once per commit, 24 h validity). The
  listener deploys an auto-asked commit itself, with no grant. An agent request
  replaces a pending auto-ask, but an auto-ask never replaces an agent's
  request. `harness-pull` defers the approvals restart while a deploy runs, and
  the box refuses two deploys at once.

## Rollout (by the owner's session after merge, not by the builder)

1. Droplet: `harness-pull` deploys the merge and restarts `hermes-approvals`.
2. Box: one deploy through the existing path (`request_deploy(<master sha>)`,
   `deploy <prefix>`, `deploy_harness()`). This installs the `deployed` verb.
3. Check: `journalctl --user -u hermes-approvals` shows the announcer ticking.
   The next merge that touches more than `docs/` gets one ask after 30
   minutes.

## Left to the builder

Module layout, exact log wording, the worker mechanism for the deploy, and how
the verb is registered on the box, within the rules above.
