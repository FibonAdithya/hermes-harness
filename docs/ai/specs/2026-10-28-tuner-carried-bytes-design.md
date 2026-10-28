# Tuner: rank by bytes the model reads, shrink results, learn from failures — Design

Date: 2026-10-28. Status: decisions made by the owner on 2026-10-28; full
document awaiting owner approval.

Extends `docs/ai/specs/2026-10-19-hermes-tuner-design.md`. Everything there
still holds unless a section below says otherwise. All code is in `tuner/`
(host program and the `hermes-tuner` plugin). Hermes Agent is not modified.

## Purpose

The tuner's first composed tool made the job it targeted more expensive, and
the tuner had no way to see that coming. The owner wants the tuner to work out
by itself which kind of fix actually saves money, and to stop repeating fixes
that failed for a known reason.

The finding behind this: a fix saves money when deterministic code keeps data
away from the model (filters, strips, extracts, decides) and the model gets
something small back. A fix that only changes how the same data is fetched
saves nothing. The tuner currently looks for repeated call sequences, so it
proposes the second kind.

**Success:** the tuner, without being pointed at it, finds that email text is
the cost in the two email jobs, ships a fix that cuts what reaches the model,
and the trial keeps it on measured cost.

## What happened (MEASURED 2026-10-28 on `hermes-vm` unless marked)

The tuner (live since 2026-10-20) has 14 ledger records:

- 9 rejected: 6 by the copy-check (§5.3 of the 10-19 spec), 3 because the
  drafting call timed out. 1 failed with a bridge error. 2 failed in the shadow
  run. 2 are in trial.
- Weekly lines: W43 "saved ~$0.00", W44 "saved ~-$0.01 … tuner spent $0.63".

**The batch tool.** `tuned_batch_fetch_gmail_contents` (record
`20261024-949b9ef4`) went live 2026-10-25 for "Nightly calendar sync from
email" (`cc68e24f21cd`). It takes a list of message ids and returns every full
body.

- Before it existed, Hermes already sent the reads in parallel. On the 10-24 run,
  turn 5 held all 8 `get_gmail_message_content` calls. So there were no turns
  to merge.
- 14 of its 41 calls since going live returned
  `{"error": "output exceeds 50000 bytes"}`. That is `policy.MAX_OUTPUT_BYTES`
  (`tuner/hermes_tuner/policy.py:25`). The median email result is 5,225 chars
  and the p90 is 19,051. After an error the model halves the batch
  (8 → 4,4 → 2,2 → 1,1) and then falls back to the single-read tool. Each
  retry is another full model turn.
- Calendar sync, priced at list prices the same way the tuner prices it:

  | | Runs | Median $/run | Median prompt tokens |
  |---|---|---|---|
  | Baseline (stored on the record) | 13 | $0.1176 | 315k |
  | With the tool | 4 | $0.1418 | 484k |

  Its trial needs 5 runs (`JOB_TRIAL_RUNS`). With these four runs in, the median
  of five can't fall below $0.1285, so the trial should roll it back after the
  2026-10-29 run. This is a calculation from the rule, not an observed verdict.
- The drafting model was never told the 50 KB cap. The shadow run didn't catch
  the failures. The trial only looks at cost, and only once the run count is
  reached.

**Repeated rejections.** The label-sequence candidate
(`seq:…modify_gmail_message_labels×5`, morning briefing) was redrafted and
rejected by the copy-check on 10-26, 10-27 and 10-28, all for the same reason.
Each draft cost $0.006–$0.022.

**Dead field.** `FixRecord.trial_runs` (`ledger.py:49`) is never written, so
every record shows 0.

**What the email results contain.** 627 `get_gmail_message_content` results
from the last 30 days, 5.55M chars in total:
- URLs are 27.8% of the characters.
- HTML tags are 8.9%.
- Stripping both leaves 66.8%.

Quoted replies measured 0%, probably because the script re-encoded the JSON and
broke line starts. Treat the one-third cut as a lower bound. The script printed
only counts.

**Carried bytes by tool, last 28 days, 14 runs per job.** Computed as
first-send chars × input rate plus chars × later turns × cache-read rate.
`messages.token_count` is NULL on all 665 tool rows, so the detector must work
from chars.

| Job | 1st | 2nd | 3rd |
|---|---|---|---|
| Calendar sync | `search_gmail_messages` (790k first, 3.4M carried) | `get_gmail_message_content` (589k, 1.5M) | `get_events` (488k, 1.0M) |
| Morning briefing | `get_gmail_message_content` (895k, 4.7M) | `search_gmail_messages` (314k, 2.1M) | batch tool (165k, 1.1M) |

Email bodies fetched through the batch tool are counted under the batch tool,
not under `get_gmail_message_content`.

**Where Hermes lets a plugin change a result.** On the pinned Hermes (v0.19.0,
`7bfbfa3e`), `model_tools.handle_function_call` runs `transform_tool_result`
hooks:
- They run after `post_tool_call` and before the result enters the
  conversation.
- They get `tool_name`, `args`, `result`, `session_id`, `tool_call_id` and
  `turn_id`.
- The first string returned replaces the result. The hook fails open.

The `tool_call` bridge that wraps every deferred tool routes through the same
function (`tools/tool_search.py`, header comment).

## Decisions

- **Direction approved by the owner on 2026-10-28:** rank by bytes reaching the
  model; add a result-reducer fix kind; replay fixes against history before they
  go live; gate on an estimated saving; give the drafter limits and past
  outcomes; back off on repeated rejections; roll back early on errors.
- **Reducers go live without asking the owner** once they pass the replay
  check and the existing shadow quality check. Owner's decision, 2026-10-28.
  It follows the 10-19 rule that a fix adding no new reach is applied without
  approval. The checks in §3 and §7 are the guard.
- **v1 reducers apply to calendar sync only.** Owner's decision, 2026-10-28.
  Morning briefing's quality check compares label and archive decisions, not
  what the briefing says, so a reducer there could drop an item unnoticed.
  Morning briefing gets reducers once a check on the briefing output exists.
- **Replay may read tool-result text on the droplet** (§2). Owner's decision,
  2026-10-28.
- **Reducers are not written by a model.** They are a fixed library of
  transforms in the tuner's code. The tuner chooses which transforms to combine
  by searching on replay. This is Claude's call, for three reasons:
  - The drafting model never sees email text (10-19 §4.3), so it couldn't design
    an email reducer well.
  - Deterministic code can be tested.
  - It matches the owner's preference for code over model calls.

## Goals

- The detector ranks candidates by the cost of the bytes the model reads, so
  the tools that bring in the most text rank first without being named anywhere
  (Gmail search in calendar sync, email bodies in morning briefing, MEASURED).
- A new fix kind, `reducer`, shrinks one tool's results for one job.
- Every `reducer` and `composed_tool` fix is replayed against recorded history
  before it is drafted into a trial. A fix that would have hit a limit, or would
  have removed text the model used, is rejected at no model cost.
- Every candidate carries an estimated saving from its mechanism. A candidate
  whose mechanism can't save anything is not drafted. The batch tool's
  candidate is the test case.
- The drafter is told the runtime limits and the outcomes of earlier attempts
  on the same candidate.
- A candidate rejected twice for the same reason stops being redrafted every
  day.
- A trial with errors from the fix's own tool or reducer is rolled back without
  waiting for 5 runs.

## Non-goals

- Reducers on chat sessions, on morning briefing, or on any job other than
  calendar sync (`cc68e24f21cd`). The detector still ranks `bytes:` candidates
  for every job, and those feed the other fix kinds as before.
- A quality check on the morning briefing's output. That is the next step
  before briefing reducers, and a separate design.
- Model-written reducer code.
- Writing tuner lessons to the shared wiki.
- The tuner for friend profiles.
- Stopping the current batch-tool trial by hand. It should roll back on its own;
  see Acceptance check 7.

## Anti-goals (a failure even if it works)

- A reducer hides something the job needed: a meeting the calendar sync no
  longer creates, or an email the briefing no longer surfaces.
- Email text reaches a model call, the ledger, a log or the weekly line through
  the replay machinery.
- The tuner's own spend goes up. Replay and reducer search must cost no model
  calls.
- A reducer changes results for the owner's chats, for other jobs, or in
  friend profiles.

## Design

### 1. Carried-bytes detector

A new deterministic signal in `detect.py`, alongside the existing four. It uses
only sizes, turn positions, tool names and argument keys, as before.

For each tool result in a job's runs:

- **First send:** its size, priced at the input rate.
- **Carry:** its size times the number of later model turns in the same
  `sessions` row, priced at the cache-read rate. Every later turn re-sends it.
  Count to the end of the row, not across a merged compression chain, because
  compression stops earlier results being re-sent. The MEASURED table above was
  computed this way. Claude's call.

Sum per (job, tool), and divide by runs to get a per-run cost. Tokens are
chars ÷ 4 (ESTIMATE), because `messages.token_count` is NULL on every tool row
(MEASURED 2026-10-28). A composed tool's results are counted under the tool it
calls, so the same email bodies aren't split across two names. The candidate is `bytes:<job>:<tool>`. It feeds two
fix kinds: `reducer` when the tool is on the reducer allowlist (§3), and
`composed_tool` or `job_prompt` otherwise.

The existing `sequence` signal stays, but its estimated saving changes (§5).

### 2. Replay harness

A new module, `replay.py`. It reads recorded sessions from `state.db`, including
tool-result text, inside the tuner process on the droplet.

- **What leaves the module:** counts, booleans and reason codes. Text never goes
  to a model call, the ledger, a log line or a file.
- **Amendment to 10-19 §4.2,** which says detection never reads tool-result
  text. Replay is not detection. It reads text only to measure and check, and an
  attacker's email can't steer it beyond choosing among fixed transforms.
  Owner's decision, 2026-10-28.

It has two uses.

**Reducers.** Apply the reducer to every recorded result of its tool, in the
job's runs from the last 28 days (builder may tune the window). Record:
- bytes before and after;
- **used strings:** maximal substrings of at least 8 chars from the raw
  result that appear verbatim, after whitespace and case normalisation, in a
  later tool-call argument or assistant message in the same session. Excluded:
  strings that also occur in the job prompt, the tool schemas, Hermes's
  result-wrapper text, or more than half of that tool's results in the window
  (boilerplate). Claude's call; the builder may tune the length and the
  boilerplate share.
- whether each used string survives, meaning it occurs in the reduced result
  after the same normalisation.

**Composed tools.** Replay covers composed tools drafted from a `sequence`
candidate or a new `bytes:` candidate. Those are the only candidates that
produce composed tools. Claude's call.
- The draft payload gains a required `replay_inputs` field. It says how the
  tool's input schema is filled from the recorded calls it replaces, e.g.
  `message_ids` ← the `message_id` argument of each replaced
  `get_gmail_message_content` call. `limits.py` rejects a draft without it.
- Replay builds each input from history from before the tool existed, then runs
  the program in the sandbox, as live. Its `dispatch_tool` calls go to a stub
  that serves the recorded result for the same tool and canonicalised
  arguments.
- A session where any call has no recorded result is skipped.
- Record limit hits (output bytes, call count, run time), program errors and
  output size.
- At least 3 replayable sessions are required. Builder may tune this.

### 3. The `reducer` fix kind

A reducer is a short, ordered list of transforms from a fixed library, plus a
target tool and job. It is stored as data, not code, under
`~/.hermes/tuner/reducers/<id>.json`, and versioned like the other fix kinds.

The `hermes-tuner` plugin registers one `transform_tool_result` hook. It
applies a live reducer only when all of these hold:
- `tool_name` matches the reducer's tool (unwrapped from `tool_call` if the
  hook sees the bridge name; the builder confirms which it sees);
- `session_id` starts with `cron_<job id>_` for the reducer's job, or with the
  shadow prefix during a shadow run;
- the result parses as the tool's normal output.

Otherwise it returns `None` and leaves the result alone. Any exception also
returns `None`, so it fails open, as Hermes does.

**Allowlisted tools (v1):** `mcp__google__get_gmail_message_content`,
`mcp__google__search_gmail_messages`, `mcp__google__get_events`. The list is
held in code.

**Transform library (v1):**
- HTML to text
- replace each URL with its host, keeping full URLs on a keep-host list that
  includes meeting hosts (`meet.google.com`, `zoom.us`, `teams.microsoft.com`)
  and any host the replay found in used strings
- drop quoted replies
- drop footers after common unsubscribe or legal markers
- collapse whitespace
- drop top-level JSON fields when every used string survives without them.
  Amended in the plan audit, 2026-10-28. The original rule was "fields that
  never contain a used string". MEASURED: all 794 live results of the three
  allowlisted tools carry the same text twice, as `result` and
  `structuredContent.result`. A field holding a copy of a used string would
  never be dropped, and the duplicate is about half of every result.
- cap the body at N chars

Each transform is a pure function with unit tests.

**Search (deterministic, no model):**
1. Start from the empty list.
2. Add the transform that cuts the most replayed bytes while keeping every used
   string.
3. Repeat until no transform cuts at least 2% more.
4. For the cap transform, try a fixed ladder of N values.

**A reducer becomes a fix only if replay shows both:**
- **100% of used strings survive** across all replayed sessions;
- it cuts at least 15% of the tool's replayed bytes. That figure is ESTIMATE,
  tunable, and set below the measured one-third.

Used strings only catch verbatim use. A model that reads "Thursday at 2pm" and
writes `2026-10-30T14:00` isn't detected that way. That is why the existing
shadow quality check (10-19 §4.5: no missed calendar event, no email the old
version kept that the new one archives) still runs before the reducer goes
live.

### 4. Composed tools are replayed before shadow

A drafted `composed_tool` is replayed (§2) after it passes `limits.py`, and
before shadow or apply. It is rejected if:
- any replayed session hits a limit or errors (the batch tool would have hit
  the output cap);
- its estimated saving (§5), recomputed with the replayed output sizes, is at or
  below the drafting threshold.

### 5. Estimated saving by mechanism

Every candidate and every draft carries a per-run estimate that says how it
saves:

- **Bytes removed:** bytes cut × (input rate once + cache-read rate × later
  turns).
- **Turns removed:** the turns that would disappear × the job's median cost per
  turn. For a sequence candidate, count only turns whose calls are all in the
  sequence. If the sequence already runs in one turn (the 10-24 case), turns
  removed is 0.
- **Composed tools:** both of the above, using the replayed output size against
  the calls' recorded result sizes.

Candidates under $0.05/month (ESTIMATE, tunable) are not drafted. Sort order
uses this estimate instead of the current `est_monthly_usd`.

Each kept or rolled-back fix records its estimate next to its measured trial
saving. The weekly line adds one figure: estimated versus measured over the
last four weeks. That shows whether the estimates can be trusted.

### 6. Drafting context and back-off

The drafting request (`draft.py`) also gets:
- the runtime limits (output bytes, call count, run time);
- the per-turn layout of the sequence: how many calls Hermes already sends per
  turn;
- the outcomes of the last 3 drafts for the same candidate key, as fix kind,
  state and reason. Reasons are tuner-generated text and never contain result
  text.

For a copy-check rejection, the reason names the fields that tripped, and the
prompt says that no literal text from emails may appear in a fix: no label
names, addresses or subjects.

**Back-off,** keyed on (candidate key, fix kind, reason class):
- Second identical rejection: skip the candidate for 7 days.
- Third: park it until its inputs change, meaning the job prompt hash, the
  schema hash of the involved tools, or the skill hash.

Parked candidates are listed in the weekly line by count only.

### 7. Early rollback

During a trial, each cycle counts the fix's own failures in the runs since
`live_from`:
- **composed tool:** a result from its tool that is an error;
- **reducer:** the hook raised an exception, or the job re-reads more than it
  did before. A re-read is the same tool called again in one session with the
  same key argument (`message_id` for message reads, the query for searches).
  It counts as a failure only when the trial's re-read rate per run exceeds the
  baseline runs' rate. A search followed by reading the ids it found is the
  normal flow and is not a re-read. Claude's call.

Two failures, or 10% of the fix's calls (whichever comes first), roll the fix
back at that cycle, with the reason. Builder may tune both numbers.

`trial_runs` is written each cycle with the run count the verdict used.

## Limits (changes to 10-19 §5)

- §5.2 limits are unchanged, and the drafter is now told them.
- New: the reducer tool allowlist and transform library are held in code.
  Adding to either is a code change, not a tuner fix.
- New: replay output is counts and reason codes only (§2).
- Reducers cost no model spend. The weekly cap is unchanged.

## Acceptance checks

Where they run (Claude's call). `scrub.py` exports keep no result text or
argument values, and that stays true: no real email text enters the repository.
- **In CI:** checks 1, 2 and 5 on a `scrub.py` fixture exported from `state.db`
  up to 2026-10-28, which includes the 10-24 and 10-25…10-28 runs. Check 6, and
  the replay logic behind checks 3 and 4, on a small synthetic fixture with
  made-up email text written for the tests.
- **On the droplet:** checks 3 and 4 against the live `state.db`, through a
  tuner command (e.g. `python -m hermes_tuner replay-check`) that prints counts
  and pass/fail only. The builder pastes that output into the PR.

1. On the fixture, the top `bytes:` candidate is
   `mcp__google__search_gmail_messages` for calendar sync and
   `mcp__google__get_gmail_message_content` for morning briefing, matching the
   MEASURED table above. Both are on the reducer allowlist.
2. On the fixture, the `seq:get_gmail_message_content×3` candidate for calendar
   sync has a turns-removed estimate of 0 for the 10-24 run.
3. Replaying the batch tool's program (from `~/.hermes/tuner/tools/`, copied
   into the fixture) records at least one output-cap hit. The draft is
   rejected.
4. The reducer search over calendar sync's last 28 days produces a reducer
   for `get_gmail_message_content` that cuts at least 25% of bytes and keeps
   100% of used strings. The 25% target is set from the measured one-third
   across all jobs, and is ESTIMATE for this job alone. It also reports what it
   found for `search_gmail_messages`, pass or fail.
5. Three identical copy-check rejections of one candidate leave it parked, and
   the fourth cycle sends no drafting call (asserted on the bridge fake).
6. Unit tests for each transform, the hook's job scoping (a chat session id and
   a different job's id both get `None`), and the fail-open path.
7. Live, after deploy:
   - the batch-tool trial has a rolled-back verdict, or was rolled back by early
     rollback;
   - within 14 days a reducer reaches trial on calendar sync;
   - its trial verdict is recorded with estimate and measured saving.

## Left to the builder

- Whether the hook sees the bridge name `tool_call` or the underlying tool name.
- Whether `ctx.dispatch_tool` results (composed tools) also pass through
  `transform_tool_result`. If they do, a reducer must not apply to them twice.
- What the six copy-check rejections actually matched. Check this on the
  droplet without printing the matched text. If they are false positives
  (for example a Gmail query string or label id that also appears in results),
  fix the check rather than the prompt, and say which.
- Transform details: footer markers, the cap ladder, the URL keep-host list.
- Whether the plugin picks up new reducers at session start without a restart
  (10-19 §4.7 asks the same for tools).
- Whether the hook still exists with the same arguments on Hermes v0.22.x. The
  droplet is on v0.19.0; an upgrade is pending separately.

## Out of scope

- Upgrading Hermes.
- Reducers for web, browser or compute tools.
- Changing the tuner's pricing source or the trial cost rule.
