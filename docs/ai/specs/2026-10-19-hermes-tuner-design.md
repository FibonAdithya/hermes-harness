# Hermes tuner: Hermes finds its own wasted work and fixes it — Design

Date: 2026-10-19
Status: agreed in conversation, not yet built.

Extends `docs/superpowers/specs/2026-07-28-hermes-assistant-vm-design.md` (the
droplet, the three planes, the Docker sandbox) and
`docs/superpowers/specs/2026-08-05-hermes-code-and-experiments-design.md` (the
broker and the approvals bot). Everything there still holds unless a section
below says otherwise. All the code lives in this repository (`hermes-harness`)
and runs on the droplet. Hermes Agent itself (NousResearch/hermes-agent, pinned
on the droplet at `7bfbfa3e`) is not modified.

## Purpose

Hermes repeats the same expensive procedures every day. The owner wants Hermes
to notice that from its own session history and fix it without being asked:
turn a repeated procedure into a tool, stop re-reading what it has already
read, shrink instructions it loads over and over, and tune its own settings.

**Success:** everything Hermes does gets cheaper, and the owner notices no
change in how it behaves. The owner hears about it in one line a week.

## Decisions made on 2026-10-19

The owner made each of these in conversation.

- **Hermes finds and fixes the waste itself.** The owner does not want to be
  the one who spots inefficiencies. The tuner reads the session history,
  decides what to fix, applies the fix, and measures it.
- **The Gmail flow is the first test case, not a hand-built fix.** The tuner
  must discover it on its own from the existing history (§2 is the answer key).
- **A fix is not only a tool.** Fix kinds: a composed tool, a change to a
  scheduled job's instructions, a change to Hermes's own config, a smaller
  skill. All four are applied without asking the owner.
- **Composed tools go live without approval; tools with new reach need the
  owner's approval.** A composed tool only calls tools Hermes already has. A
  tool that adds reach (a new API, a credential, a newly exposed MCP tool, host
  access) is proposed in the approvals chat and goes live only on approval.
- **"Already checked" is a Gmail label** the owner can see, e.g.
  `hermes/briefed`, one per job.
- **Reporting is one weekly summary line**, e.g. "saved ~$X this week,
  2 fixes applied, 1 rolled back".

The limits in §5 were proposed by Claude. The owner was shown them, raised no
objection, and moved on to the reporting question; treat them as agreed unless
the owner says otherwise when reviewing this document.

## 1. How Hermes is wired today (surveyed 2026-10-19)

Facts the build depends on, each from a command run on the droplet or the
pinned Hermes source:

- **Session history** is in `~/.hermes/state.db` (SQLite, WAL). `sessions` has
  per-session `input_tokens`, `cache_read_tokens`, `output_tokens` and
  `estimated_cost_usd` (`actual_cost_usd` is always 0). `messages` has every
  tool call (`tool_calls` JSON on assistant rows) and every tool result
  (`role='tool'`, `tool_name`, `content`). Scheduled-job sessions have ids
  `cron_<job id>_<YYYYmmdd_HHMMSS>`.
- **Scheduled jobs** are in `~/.hermes/cron/jobs.json`, each with a `prompt`.
  Five are enabled. Two of them read email: `morning-briefing`
  (`3bc4e11d29b1`, 08:00) and `Nightly calendar sync from email`
  (`cc68e24f21cd`, 06:00).
- **Gmail and Calendar** come from the `workspace-mcp` server (`uvx`), exposing
  seven tools through `mcp_servers.google.tools.include`. Its Google login holds
  `gmail.modify`, `gmail.labels` and `gmail.compose`, which can **send**
  email (Google's scope docs: compose = "Manage drafts and send emails").
  "Hermes cannot send email" holds only because no send tool is in the include
  list.
- **Tool Search** is on (default `auto`): MCP tools are hidden behind
  `tool_search`/`tool_describe`/`tool_call`, so the model fetches each MCP
  tool's schema in every new session.
- **`execute_code`** runs a Python program in the Docker sandbox whose tool
  calls are relayed back to the host, and only the program's stdout returns to
  the model. It can call only 7 tools (`tools/code_execution_tool.py:62`,
  hard-coded): no MCP tools.
- **Plugins** in `~/.hermes/plugins/<name>/` get `ctx.register_tool(...)` and
  `ctx.dispatch_tool(...)` (`hermes_cli/plugins.py:410`, `:604`). This is the
  sanctioned way to add tools without changing Hermes.
- **The tool list and system prompt are frozen per session** (prompt caching).
  A new tool or changed setting takes effect from the next session. Every
  scheduled job run is a new session.
- **Injection scanner:** `tools/threat_patterns.py::scan_for_threats(text,
  scope="context")`.

## 2. The answer key: what the history shows (measured 2026-10-19)

Read-only queries over `state.db`, 867 sessions, 2026-07-28 to 2026-10-19.
Costs are Hermes's own estimates. The query script is reproduced in the
appendix. The tuner's detector must find items 1–3 on this history without
being told.

1. **Reading whole emails one at a time.** `get_gmail_message_content`: 8,235
   calls, 95.4M characters of results (average 11,589 per email), more than all
   other tools combined. The most common repeated sequence is search → read ×N
   → label ×N (5,942 single-email label calls), in 240–360 sessions.
2. **Re-reading the same emails.** Calendar sync reads every inbox email from
   the last 7 days, every night. Morning briefing reads the 20 newest inbox
   emails, and whatever it keeps is read again the next day.
3. **Looking up tool schemas every session.** 1,809 `tool_describe` calls; 308
   sessions open with `read_file > tool_describe > tool_describe`.
4. Context-heavy skills: `gmail-cleanup` is 21,307 characters per load
   (56 loads), `hermes-agent` 14,019, `google-workspace` 15,242.

**Baseline per run, September 2026 medians, model `deepseek/deepseek-v4-pro`:**

| Job | Est. cost | Prompt tokens | Tool calls | Emails read | Email chars |
|---|---|---|---|---|---|
| morning-briefing (29 runs) | $0.123 | 293.3k | 45 | 20 | 194.7k |
| calendar sync (29 runs) | $0.158 | 379.6k | 36 | 19 | 310.5k |

Spend by month (estimated): Aug $39.86, Sep $28.66; October was cut short when
the credits ran out (scheduled jobs failed 2026-10-04 to 10-17).

## 3. Shape of the system

```
 state.db, jobs.json, skills, config ──read──▶ tuner (host, systemd timer)
                                                │ 1 measure  2 detect
                                                │ 3 draft (model call, structured output)
                                                │ 4 validate (deterministic limits, §5)
                                                │ 5 apply + record version
                                                │ 6 trial, then keep or roll back
                                                ▼
          ~/.hermes/tuner/  (fix ledger, versions, composed tools)
                 │                                   │
   hermes-tuner plugin (loads composed tools)   weekly line / approval requests
                 │                                   ▼
          Hermes sessions                   approvals bot (broker/approvals.py)
```

Two parts, both in this repository:

- **`tuner` host program** (Python, `uv`, like `broker/`), run by a systemd
  user timer on the droplet as `hermes`. It is the only component that writes
  fixes. A model drafts fixes, but deterministic code decides whether a draft
  is allowed and applies it.
- **`hermes-tuner` Hermes plugin**, installed into `~/.hermes/plugins/`. At
  load it registers every live composed tool from `~/.hermes/tuner/tools/`. It
  never writes anything.

## 4. The tuner cycle

### 4.1 Measure

Per scheduled job and per week: median estimated cost, prompt tokens, tool
calls and tool-result characters per run, taken from `state.db`. Chat sessions
are measured in total only: they differ too much from each other to attribute
a change to one fix.

### 4.2 Detect

Deterministic: no model call. It works only from tool names, argument key
names, message ids, counts and sizes, never from tool-result text. Signals,
at minimum:

- repeated tool-call sequences (n-grams over tool names) per job and overall,
  ranked by the result characters they bring in;
- the same item read in consecutive runs of the same job (for Gmail: the same
  `message_id` argument to `get_gmail_message_content`);
- per-session schema lookups (`tool_describe` count);
- skill loads ranked by characters loaded × frequency.

Each signal becomes a candidate with an estimated saving per month. Detection
is cheap enough to run on every tuner run.

### 4.3 Draft

For the top candidate per job (and the top overall candidate for config), one
model call drafts a fix as structured JSON of one kind:

| Kind | Payload |
|---|---|
| `composed_tool` | name, input schema, description, Python program, tools it calls |
| `job_prompt` | job id, new prompt |
| `config` | key, new value (key must be on the allowlist, §5.1) |
| `skill` | skill path, new `SKILL.md` text |
| `new_reach` | what reach, why, the exact change; goes to the owner (§4.6) |

**What the drafting model is given:** the candidate's statistics, the job
prompt, the skill text, the schemas of the tools involved, and the tool-call
sequence by name and argument keys. Never email text or other tool-result
content. That keeps the owner's mail out of a second model call and gives an
attacker's email no route into a fix.

The tuner uses the same provider and login as Hermes. Its own spend is
recorded and counted against the savings (§6).

### 4.4 Validate

Every check here is deterministic code (§5). A draft that fails any check is
discarded and recorded as rejected, with the reason.

### 4.5 Apply, trial, keep or roll back

- Apply writes the new version and keeps the previous one (jobs.json entry,
  config value, SKILL.md, composed tool directory) under
  `~/.hermes/tuner/versions/`, so every fix can be undone.
- **At most one fix under trial per job at a time.** Otherwise two changes'
  effects can't be separated.
- **Quality check before it counts.** Before a fix touching a mutating job goes
  live, a dry run of the new version is compared with the old version's
  behaviour on the same inputs:
  - **Triage:** keep/archive/delete decisions agree, and there is **no email
    the old version kept that the new one deletes or archives.**
  - **Calendar sync:** no event the old version would create is missed.

  Dry runs need composed tools that mutate to accept `dry_run` and return what
  they would have done.
- **Which jobs v1 changes** (Claude's call): `job_prompt` and `composed_tool`
  fixes apply only to the two jobs that have a quality check:
  - `morning-briefing`, which labels mail, so the triage rule applies;
  - calendar sync, where the calendar rule applies.

  The other three enabled jobs (web research reports) get only `config` and
  `skill` fixes until a quality check is written for them. `gmail-cleanup-batch` is
  disabled and is left alone.
- **Trial:** the fix stays for a trial window (builder sets the length; daily
  jobs need several runs), then is kept only if the median cost per run is
  below the baseline and no quality check failed. Otherwise it is rolled back
  automatically and recorded with the reason.
- A config change that lengthens sessions or raises cost is rolled back on the
  same rule.

### 4.6 New-reach tools

A `new_reach` draft is sent to the approvals chat with a 4-digit code bound to
a hash of the exact change, following the `task|solo|drop` pattern in
`broker/hermes_broker/approvals.py`: a new reply verb, e.g. `tool NNNN` to
approve and `drop NNNN` to discard. Approval applies that exact change; an
edited proposal needs a new code. Example: creating the `hermes/*` labels
needs a label-management tool that is not in the Google include list today.
Adding it is new reach.

**Who applies an approved change** (Claude's call):
- The listener only records the approval in its state directory, as it does
  for grants.
- The tuner reads that record and applies **exactly the hashed change**. This
  is the one route by which the tuner may touch a key forbidden in §5.1, and
  only for the change named in the approval.
- A change to the `workspace-mcp` launch arguments (`--permissions`,
  `--tool-tier`) is never proposed as a tuner fix, because those arguments are
  what withhold the send tool. If a capability needs one, the proposal says so
  and the owner makes that change by hand.

**Answer-key item 2 waits for the owner.** The `hermes/*` labels arrive through
the first `new_reach` proposal. That is expected, and it is also the first live
test of §4.6. Until it is approved, the tuner may still apply a `job_prompt` fix
that narrows calendar sync to emails newer than its last successful run (a date
in the Gmail query, with no labels).

### 4.7 Restarting the gateway

Plugins are loaded once per gateway process, and MCP changes need a restart
too. Rules (Claude's call):
- The plugin re-scans `~/.hermes/tuner/tools/` at session start if Hermes's
  hooks allow it, so composed tools need no restart. Whether that works is the
  builder's to confirm.
- When a restart is needed, the tuner restarts `hermes-gateway` only between
  02:00 and 05:00 UK time, only when no session has had activity in the last
  30 minutes, and never within 15 minutes of a scheduled job.
- A fix that needs a restart counts as live from the first session after the
  restart.

### 4.8 Weekly line

Once a week, one message: estimated saving this week against baseline, fixes
applied, kept, rolled back, and the tuner's own spend. Sent through the
approvals bot's `sendMessage` (sending does not conflict with the listener's
single `getUpdates` consumer).

## 5. Limits (agreed)

### 5.1 Config allowlist

The tuner may change only keys on a fixed allowlist of cost settings, for
example `tools.tool_search.*` and `compression.*`. It may **never** change:
`mcp_servers.*` (including `tools.include`), `terminal.*` (sandbox and
network), `model`/provider/`auxiliary` routing, `platform_toolsets`,
`delegation.*`, plugins configuration, or anything in `.env` or
`~/.hermes/broker/`. The allowlist lives in the tuner's code, not in a file the
tuner writes.

A config fix must also name a setting Hermes already has. Each cycle the tuner
reads Hermes's resolved settings under the allowed prefixes (strings, numbers
and booleans only, the values it can set and roll back), sends them with their
current values in the drafting request, and rejects any other key when the fix
is drafted. If the settings cannot be read, every config fix is rejected.
*Added 2026-10-21:* the first run's model proposed
`tools.tool_search.cache_schemas`, which does not exist.

### 5.2 Composed tools add no reach

- The program runs **in the Docker sandbox** (no network, no credentials), not
  in the gateway process. Running it in-process would give it the host's
  credentials.
- Its tool calls go back to the host and are dispatched with the plugin API
  `ctx.dispatch_tool` (Claude's call). That API skips Hermes's
  `pre_tool_call` hooks and middleware, but the gates that matter are enforced
  elsewhere and still hold:
  - the Gmail include list, enforced at registration;
  - the broker's `LOCKED` grants, enforced server-side;
  - the sandbox, enforced by Docker.

  Calling Hermes internals (`model_tools.handle_function_call`) instead was
  rejected because it breaks on Hermes updates.
- Because the handler is not told the session's enabled tools, the plugin keeps
  its own **allowlist of callable tools**, held in code:
  - the 7 Google tools;
  - the read-only compute tools (`night_status`, `night_log`, `list_repos`,
    `job_status`, `job_logs`);
  - `web_search`, `web_extract`, `read_file`, `search_files`.

  Not on it: `delegate_task`, `cronjob`, `skill_manage`, `memory`, `terminal`,
  file writes, gated compute tools, and other composed tools. Adding a tool to
  the allowlist is new reach (§4.6).
- It has limits on run time, number of tool calls and output size, like
  `execute_code` (300 s, 50 calls, 50 KB).

### 5.3 No text copied from the logs

Job prompts, skill text, tool names and tool descriptions the tuner writes are
built from the draft. They pass `scan_for_threats(..., scope="context")`, and
none may contain text copied from **untrusted** tool results. A failing draft
is discarded.

- **Untrusted results** are the results of tools that return outside content:
  every `mcp__google__*` tool, `web_search`, `web_extract`, every `browser_*`
  tool, `session_search`, and `read_file` on paths outside `~/.hermes/skills/`.
- **Hermes's own text is not untrusted:** `skill_view`, `tool_describe` and
  `tool_search` results. A shrunk skill may reuse its own wording, and a tool
  description may name `get_gmail_message_content`.
- **What counts as copied** (Claude's call): any run of 40 or more characters
  that matches untrusted-result text after whitespace is normalised.

Note: Hermes's scanner currently blocks the droplet's `SOUL.md` entirely
because line 32 quotes "ignore previous instructions" (404 log lines since
2026-09-19). The tuner's own text must not trip the same scanner.

### 5.4 Skills

Only skills not shipped with Hermes are edited. Bundled skills are overwritten
on Hermes updates. A shrunk skill keeps the decision-making guidance and
points at the tool for the mechanics.

## 6. The tuner pays for itself

- The tuner's estimated spend has a weekly cap (Claude's call: $1/week,
  ESTIMATE, adjustable). **All** its model spend counts, including drafting
  calls and the dry runs in §4.5. A dry-run comparison is roughly $0.25–$0.30
  at §2 medians (ESTIMATE). So the cap allows about three comparisons a week,
  which is enough for one fix under trial per job.
- **Scale of the prize** (ESTIMATE): the two email jobs cost about $8/month at
  September usage. A 50% saving is about $4/month, so the tuner must stay well
  under that. Detection costs nothing, which is why it is deterministic.
- If its trailing four-week spend exceeds both the trailing four-week measured
  savings and a $1 starting allowance (ESTIMATE, the size of the weekly cap), it
  stops drafting new fixes. It keeps measuring and still reports in the weekly
  line. A tuner that never saves anything therefore costs about $1 per four
  weeks.
  - *Amended 2026-10-21.* The rule first read "spend exceeds savings" alone.
    Any spend before the first fix succeeds trips that rule, so the first run's
    two failed drafts ($0.04, ESTIMATE) would have paused drafting until they
    aged out four weeks later.

## 7. Verification

1. **Answer-key test.** Run the detector on the droplet against a read-only
   snapshot of `state.db` taken 2026-10-19 or later, and confirm items 1–3 of §2
   rank in its top candidates.
   - This repository is **public**, so the raw database and anything holding
     message text, search queries or argument values never leave the droplet.
   - CI runs the detector on a **scrubbed fixture** instead. It holds tool
     names, argument key names, result lengths, session ids, timestamps and
     message ids as salted hashes, and nothing else. A script in `tuner/`
     produces it on the droplet. The scrubbed fixture is committed only after
     a test confirms it contains no text field longer than an id.
2. **First live result.** Within the first trial windows, both email jobs run
   cheaper than the §2 baseline with no quality-check failure. Target (Claude's
   call, ESTIMATE): median estimated cost per run down at least 50% for each
   email job.
3. **Limits hold**, each by a test that tries to break it:
   - a draft changing a forbidden config key is rejected;
   - a composed tool calling a tool outside the session is refused;
   - a composed program trying network access from the sandbox fails;
   - a draft containing a substring of tool-result content is rejected;
   - a fix that raises cost is rolled back.
4. **New reach waits for approval.** A `new_reach` proposal does nothing until
   `tool NNNN`; an edited proposal needs a new code.

## Left to the builder

- How composed programs reach the sandbox and relay tool calls back:
  `execute_code`'s Docker file-RPC transport is the model to follow, since its
  allowlist cannot be widened without changing Hermes.
- The exact detection thresholds and ranking, provided §7.1 passes.
- Trial window length, dry-run mechanics per job, and the tuner timer schedule.
- How the drafting model call is made (Hermes one-shot or direct API), within
  §4.3's input rule.
- File formats under `~/.hermes/tuner/`.
- Whether `tool_search` should be turned off or kept on, which §4.5 decides by
  measurement.

## Out of scope

- Server-side Gmail filters that bin noise senders on arrival. This is a likely
  later fix kind.
- Fixes on `tig-server` or `tig-gpu`.
- Changing model or provider to save money.
- Per-fix measurement of chat sessions.
- Changing Hermes Agent's source.

## Appendix: answer-key query

The read-only script used for §2 is
`docs/ai/specs/2026-10-19-hermes-tuner/session_stats.py`. It is run on the droplet
as `ssh hermes-vm 'python3 -' < session_stats.py` and prints only counts and sums. The
per-job baseline table came from a second query, grouping `cron_<job id>_*` sessions
by month and taking medians. The builder should turn both into the first detector
test fixtures under `tuner/`.
