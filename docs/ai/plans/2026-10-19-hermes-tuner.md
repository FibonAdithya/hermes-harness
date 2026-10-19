# Hermes Tuner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A host program on the droplet that reads Hermes's session history, finds repeated wasted work, applies bounded fixes (composed tools, job prompts, config, skills), checks them, and keeps or rolls them back — plus the Hermes plugin that serves composed tools and the approvals-bot changes for new-reach proposals and the weekly line.

**Architecture:** `tuner/` is a stdlib-only Python package (`hermes_tuner`) run daily by a systemd user timer. It never imports Hermes; every Hermes action goes through `bridge_entry.py`, a script executed with Hermes's own venv Python. `tuner/plugin/hermes-tuner/` is a Hermes plugin (symlinked into `~/.hermes/plugins/`) that registers composed tools, runs their programs in a throwaway `--network=none` sandbox container, and blocks writes in shadow sessions. The broker's approvals listener gains a proposal store, the `tool NNNN` verb, and an outbox sender.

**Tech Stack:** Python ≥3.11 (stdlib only in `hermes_tuner` and the plugin), `uv`, pytest, SQLite (read-only), Docker, systemd user units, the existing `hermes_broker` package.

**Spec:** `docs/ai/specs/2026-10-19-hermes-tuner-design.md` (read it first; section numbers below refer to it).

## Global Constraints

- Hermes Agent source is never modified. Hermes is reached only through `bridge_entry.py` (run with `/home/hermes/.hermes/hermes-agent/venv/bin/python`) and the plugin API (`ctx.register_tool`, `ctx.dispatch_tool`, `ctx.register_hook`).
- `state.db` is opened only as `file:<path>?mode=ro` with `uri=True`.
- The tuner writes only under `~/.hermes/tuner/`, plus the targets of applied fixes (job prompt/toolsets via bridge, config keys via bridge, `SKILL.md` files of non-bundled skills). Never under `~/.hermes/broker/`, never `.env`.
- Config allowlist (spec §5.1): only keys starting `tools.tool_search.` or `compression.`. Forbidden prefixes, always rejected even if allowlisted by mistake: `mcp_servers.`, `terminal.`, `model`, `provider`, `auxiliary.`, `platform_toolsets`, `delegation.`, `plugins.`.
- Composed tools: names start `tuned_`; toolset `tuner`; program runs in `docker run --rm -i --network=none --memory 512m hermes-sandbox:latest`; limits 300 s, 50 tool calls, 50,000 bytes output (§5.2).
- Callable allowlist for composed programs (§5.2): `mcp__google__search_gmail_messages`, `mcp__google__get_gmail_message_content`, `mcp__google__modify_gmail_message_labels`, `mcp__google__draft_gmail_message`, `mcp__google__list_calendars`, `mcp__google__get_events`, `mcp__google__manage_event`, `mcp__compute__night_status`, `mcp__compute__night_log`, `mcp__compute__list_repos`, `mcp__compute__job_status`, `mcp__compute__job_logs`, `web_search`, `web_extract`, `read_file`, `search_files`, plus tools named in owner-approved `callable_add` proposals.
- Untrusted tool results (§5.3): any tool starting `mcp__google__` or `browser_`, plus `web_search`, `web_extract`, `session_search`, and `read_file` on a path not under `/root/hermes-wiki/` or the skills dir. Copy threshold: 40 characters after collapsing whitespace runs to one space.
- Jobs that may get `job_prompt`/`composed_tool` fixes in v1: `3bc4e11d29b1` (morning-briefing, triage rule) and `cc68e24f21cd` (calendar sync, calendar rule). Others: `config`/`skill` fixes only. `8da9a6857256` (gmail-cleanup-batch) is never touched.
- Spend: weekly cap $1.00 (ESTIMATE) over all tuner model spend, drafting and shadow runs included (§6). Drafting pauses when trailing-28-day spend > trailing-28-day measured savings.
- Gateway restart only 02:00–05:00 Europe/London, no message in `state.db` in the last 30 min, and no enabled job's `next_run_at` within 15 min (§4.7).
- Every number in a report or commit message is MEASURED (command shown) or labelled `ESTIMATE`.
- Git: stage explicit paths; never `git add -A`/`.`; nothing over 1 MB; this repo is **public** — no mail-derived text is ever committed.

Claude's calls made in this plan (not in the spec; the owner reviews them with the plan): trial length 5 runs for job fixes and 7 days for config/skill fixes; triage agreement ≥ 0.90 on messages both versions saw; shadow runs start 20 min before the live run; detector thresholds in Task 3; winnowing parameters in Task 4.

## Review Focus

1. **`state.db` busy or mid-checkpoint** while the gateway writes — expected: the tuner reads a consistent snapshot or skips the run with a logged reason; it never blocks or corrupts the gateway's writes. (Test in Task 1.)
2. **Owner edited a job/config/skill by hand while a fix is on trial** — expected: rollback does not overwrite the owner's edit; the fix is closed as `owner_changed`. (Test in Task 6.)
3. **A composed program hangs, floods output, or loops on tool calls** — expected: killed at the limit, the model gets an error string, the gateway thread survives, the container is gone. (Test in Task 5.)
4. **Tuner crashed after creating a shadow job** — expected: the next cycle removes stale `tuner-shadow-` jobs, and the plugin still blocks writes in any session of a `tuner-shadow-` job even with no ledger. (Tests in Tasks 5 and 8.)
5. **A Hermes update changes the bridge's internals** (import moved, signature changed) — expected: the bridge call fails, the cycle applies nothing that run, records `bridge_error`, and the weekly line says so. (Test in Task 10.)

---

## File map

```
tuner/pyproject.toml
tuner/hermes_tuner/__init__.py
tuner/hermes_tuner/policy.py        constants from Global Constraints (single source)
tuner/hermes_tuner/model.py         dataclasses shared by all modules
tuner/hermes_tuner/history.py       state.db reader → model objects; raw-arg access for trials
tuner/hermes_tuner/scrub.py         History ↔ scrubbed fixture (gzip JSON)
tuner/hermes_tuner/measure.py       per-job run statistics
tuner/hermes_tuner/detect.py        candidates from history
tuner/hermes_tuner/fingerprint.py   winnowing copy detector
tuner/hermes_tuner/limits.py        check_fix(): every §5 rule
tuner/hermes_tuner/bridge.py        HermesBridge (subprocess to bridge_entry.py) + FakeBridge for tests
tuner/hermes_tuner/bridge_entry.py  runs under Hermes venv; JSON in/out
tuner/hermes_tuner/ledger.py        fix records, versions, apply/rollback
tuner/hermes_tuner/draft.py         drafting request + response parser
tuner/hermes_tuner/trial.py         shadow runs, quality comparison, keep/rollback verdicts
tuner/hermes_tuner/proposals.py     tuner side of new-reach proposals and approvals
tuner/hermes_tuner/restart.py       restart window
tuner/hermes_tuner/cycle.py         one daily run; weekly line
tuner/hermes_tuner/__main__.py      CLI: run | detect | scrub
tuner/plugin/hermes-tuner/plugin.yaml
tuner/plugin/hermes-tuner/__init__.py   register(ctx)
tuner/plugin/hermes-tuner/relay.py      run_program(): container + call relay
tuner/plugin/hermes-tuner/stub.py       code executed inside the container
tuner/tests/...                         one test file per module, factory.py builds temp state.db
broker/hermes_broker/proposals.py       ProposalStore + approval record
broker/hermes_broker/approvals.py       tool verb, drop routing, proposal + outbox announcing
broker/hermes_broker/config.py          optional tuner_dir
droplet/install-tuner.sh, droplet/units/hermes-tuner.{service,timer}
runbooks/tuner.md
.github/workflows/ci.yml                tuner job
```

On-disk layout under `~/.hermes/tuner/` (all JSON, written atomically with temp file + `os.replace`, mode 0600):
`ledger.json`, `versions/<fix_id>/before.json` and `after.json`, `tools/<name>/tool.json` + `program.py`, `shadow_jobs.json`, `shadow/<session_id>.jsonl`, `proposals/<proposal_id>.json`, `outbox/<name>.txt`, `.lock`.

---

### Task 1: Package skeleton, policy constants, history reader, test factory

**Files:**
- Create: `tuner/pyproject.toml`, `tuner/hermes_tuner/__init__.py`, `tuner/hermes_tuner/policy.py`, `tuner/hermes_tuner/model.py`, `tuner/hermes_tuner/history.py`, `tuner/tests/factory.py`, `tuner/tests/test_history.py`
- Modify: `.github/workflows/ci.yml` (add job `tuner`: `cd tuner && uv sync --extra dev && uv run pytest -q`)

**Interfaces:**
- Produces (`model.py`):
  - `ToolCall(call_id: str, name: str, arg_keys: tuple[str, ...], ref: str | None, result_chars: int)`. `ref` is the `message_id` argument when present, else `None`.
  - `Session(id: str, source: str, job_id: str | None, started_at: float, est_cost: float, prompt_tokens: int, output_tokens: int, calls: tuple[ToolCall, ...])`. `prompt_tokens = input_tokens + cache_read_tokens`.
- Produces (`history.py`):
  - `JOB_SESSION_RE = re.compile(r"^cron_([0-9a-f]{12})_\d{8}_\d{6}$")`
  - `class History` with `@classmethod open(cls, db_path: Path) -> History`, `sessions(since: float, until: float | None = None) -> list[Session]`, `last_activity() -> float` (max `messages.timestamp`), `calls_with_args(session_id: str) -> list[tuple[str, dict]]` (tool name, parsed args; trials only), `untrusted_texts(since: float) -> Iterator[str]` (results of untrusted tools per Global Constraints).
  - `class HistoryUnavailable(Exception)`.
- Produces (`policy.py`): every constant from Global Constraints as module-level names: `CONFIG_ALLOW_PREFIXES`, `CONFIG_FORBIDDEN_PREFIXES`, `CALLABLE_TOOLS` (frozenset), `UNTRUSTED_PREFIXES`, `UNTRUSTED_TOOLS`, `TRUSTED_READ_ROOTS = ("/root/hermes-wiki/", "/root/.hermes/skills/")`, `COPY_MIN_CHARS = 40`, `TOOL_PREFIX = "tuned_"`, `TOOLSET = "tuner"`, `RUN_TIMEOUT_S = 300`, `MAX_CALLS = 50`, `MAX_OUTPUT_BYTES = 50_000`, `SANDBOX_ARGV`, `MUTATING_TOOLS = frozenset({"mcp__google__modify_gmail_message_labels", "mcp__google__manage_event", "mcp__google__draft_gmail_message", "write_file", "patch", "terminal"})`, `COMPUTE_READ_TOOLS` (the five read-only compute tools), `SHADOW_BLOCK` (Task 5), `EDITABLE_JOBS = {"3bc4e11d29b1": "triage", "cc68e24f21cd": "calendar"}`, `NEVER_TOUCH_JOBS = frozenset({"8da9a6857256"})`, `WEEKLY_CAP_USD = 1.0`, `JOB_TRIAL_RUNS = 5`, `GLOBAL_TRIAL_DAYS = 7`, `TRIAGE_AGREEMENT_MIN = 0.90`, `SHADOW_LEAD_MIN = 20`, `SHADOW_PREFIX = "tuner-shadow-"`, `RESTART_WINDOW = (2, 5)`, `RESTART_TZ = "Europe/London"`, `QUIET_MIN = 30`, `JOB_MARGIN_MIN = 15`.

`tests/factory.py`: `make_db(path: Path, sessions: list[dict]) -> Path` creates the subset of the real schema the tuner reads (`sessions`: id, source, started_at, input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, model; `messages`: id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp). Each session dict: `{"id", "source", "started_at", "cost", "in", "cache", "out", "calls": [(name, args_dict, result_text), ...]}`. It writes one assistant row with OpenAI-style `tool_calls` and one `tool` row per call.

- [ ] **Step 1: Write failing tests** in `tuner/tests/test_history.py`:
  - `test_job_id_parsed_from_cron_session_id`: session `cron_3bc4e11d29b1_20260901_080000` → `job_id == "3bc4e11d29b1"`; `20260901_080000_ab12cd34` → `job_id is None`. *Catches:* regex anchored wrongly, or the job id taken from the wrong group.
  - `test_calls_carry_names_keys_ref_and_lengths`: one call `("mcp__google__get_gmail_message_content", {"message_id": "m1", "body_format": "text"}, "x"*500)` → `ToolCall.name` as given, `arg_keys == ("body_format", "message_id")` (sorted), `ref == "m1"`, `result_chars == 500`. *Catches:* the call/result join keyed on the wrong column; `ref` not taken from `message_id`.
  - `test_prompt_tokens_include_cache_reads`: in=100, cache=900 → `prompt_tokens == 1000`. *Catches:* dropping cache reads, which are most of the tokens.
  - `test_untrusted_texts_excludes_skill_view_and_wiki_reads`: results of `skill_view`, `tool_describe`, `read_file {"path": "/root/hermes-wiki/a.md"}` are absent; results of `mcp__google__get_gmail_message_content`, `web_extract`, `read_file {"path": "/root/repos/x"}` are present. *Catches:* trusted/untrusted inverted, or `read_file` path rule missing.
  - `test_open_is_read_only`: `History.open(p)._conn.execute("insert into sessions(id, source, started_at) values ('x', 'cli', 0)")` raises `sqlite3.OperationalError` mentioning `readonly`. *Catches:* opening without `mode=ro`.
  - `test_busy_database_raises_history_unavailable` (Review Focus 1): hold an exclusive lock (`BEGIN EXCLUSIVE` on a second connection with `journal_mode=DELETE`), then `History.open(p).sessions(0)` raises `HistoryUnavailable` within 10 s (`timeout=5` on connect). *Catches:* a blocking read with no timeout.
- [ ] **Step 2:** `cd tuner && uv run pytest tests/test_history.py -q` → FAIL (module missing).
  - `test_tool_search_bridge_calls_are_unwrapped` (audit C1). Measured 2026-10-19 on the newest 3,000 assistant rows with Google calls: 7,542 of 7,561 `get_gmail_message_content` calls were bridged. The assistant `tool_calls` entry has `function.name == "tool_call"` and `arguments == {"name": "mcp__google__get_gmail_message_content", "arguments": {"message_id": "m9"}}`; the inner `arguments` may itself be a JSON string. The tool row has `tool_name = "mcp__google__get_gmail_message_content"`. Expected `ToolCall.name == "mcp__google__get_gmail_message_content"`, `arg_keys == ("message_id",)`, `ref == "m9"`. The same applies to `calls_with_args`. *Catches:* reading the wrapper's name/args, which would hide most Gmail traffic from the detector and the trials.
  - `test_tool_row_without_name_kept_as_unknown`: a tool row with `tool_name NULL` and no matching call id gives `name == "?"`, and does not raise. *Catches:* 275 such rows crashing the reader.
- [ ] **Step 3:** Implement `policy.py`, `model.py`, `history.py`.
  - **Join:** map `tool_call_id` on tool rows to the ids in the preceding assistant `tool_calls` JSON (`[{"id", "function": {"name", "arguments"}}]`; `arguments` may be a JSON string or a dict).
  - **Unwrap:** when the function name is `tool_call`, use the inner `name` and `arguments`.
  - **Name:** prefer the tool row's `tool_name` when it is set.
  - **Bad input:** unparseable arguments give `arg_keys=()` and `ref=None`.
- [ ] **Step 4:** Run the tests → PASS.
- [ ] **Step 5:** Commit `tuner/pyproject.toml tuner/hermes_tuner/{__init__,policy,model,history}.py tuner/tests/{factory,test_history}.py .github/workflows/ci.yml` — `feat(tuner): package skeleton and read-only history reader`.

### Task 2: Measurement

**Files:** Create `tuner/hermes_tuner/measure.py`, `tuner/tests/test_measure.py`

**Interfaces:**
- Consumes: `History.sessions`, `Session`.
- Produces: `JobStats(runs: int, median_cost: float, median_prompt_tokens: float, median_tool_calls: float, median_result_chars: float)`; `job_stats(sessions: list[Session], job_id: str) -> JobStats | None` (None when 0 runs); `chat_stats(sessions) -> JobStats | None` (source `telegram`); `weekly_spend(sessions, job_ids: set[str]) -> float`.

- [ ] **Step 1: Failing tests:**
  - `test_median_not_mean`: costs [0.1, 0.1, 5.0] → `median_cost == 0.1`. *Catches:* using mean (one runaway run would fake a regression).
  - `test_only_that_jobs_sessions_count`: two jobs interleaved → each `runs` counts its own. *Catches:* filtering on source instead of job id.
  - `test_failed_runs_with_zero_calls_are_excluded`: a run with 0 tool calls is skipped, whatever its cost (the October auth failures looked like this). A run with 1 call counts. *Catches:* outage runs lowering the baseline and faking savings.
  - `job_stats` and `chat_stats` take `since`/`until` bounds through their callers (the list passed in). Their docstrings say so; no test is needed beyond the above.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement with `statistics.median`. **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): per-job run statistics`.

### Task 3: Detector and scrubbed fixtures

**Files:** Create `tuner/hermes_tuner/detect.py`, `tuner/hermes_tuner/scrub.py`, `tuner/tests/test_detect.py`, `tuner/tests/test_scrub.py`

**Interfaces:**
- Consumes: `Session`, `ToolCall`, `job_stats`.
- Produces:
  - `Candidate(kind: Literal["sequence", "reread", "schema_lookup", "skill_load"], key: str, job_id: str | None, sessions: int, occurrences: int, result_chars: int, est_monthly_usd: float)`.
  - `detect(sessions: list[Session], now: float) -> list[Candidate]`, sorted by `est_monthly_usd` descending.
  - `scrub.dump(sessions: list[Session], out: Path, salt: bytes) -> None` writes gzip JSON; `ref` becomes `sha256(salt + ref)[:16]`; no other string field is kept besides tool names, arg keys, session ids, source and job id.
  - `scrub.load(path: Path) -> list[Session]`.

Detector rules (Claude's calls), over the last 28 days of sessions:
- `sequence`: n-grams of tool names, n = 3..5, counted per job (and job `None` for chats). Kept if seen in ≥ 10 sessions. `key = "seq:" + ">".join(names)`. Keep only the longest n-gram among those that share a session set.
- `reread`: for each job, a `ref` read by `mcp__google__get_gmail_message_content` in two consecutive runs. `occurrences` = repeat reads; `result_chars` = their characters. `key = "reread:" + job_id`.
- `schema_lookup`: sessions whose `tool_describe` count ≥ 2; kept if ≥ 20 such sessions. `key = "schema_lookup"`, `job_id=None`.
- `skill_load`: per skill name from `skill_view` calls (name needs `calls_with_args`; on scrubbed fixtures the key is `skill_load:?` and is skipped). Kept if loads × average characters ≥ 200,000. `key = "skill_load:" + name`.
- `est_monthly_usd` = Σ over the candidate's sessions of `est_cost × (candidate result_chars in session / all result_chars in session)`, scaled to 30 days.

- [ ] **Step 1: Failing tests** (synthetic sessions built in-test from `model` objects, mirroring the answer key):
  - `test_gmail_loop_found_for_briefing_job`: 12 daily runs of job `3bc4e11d29b1`, each `search > get×20 > modify×20` with **distinct refs per run**, get results 10,000 chars, run cost $0.12. Add 12 chat sessions of `web_search×3` costing $0.01 each. The top candidate must be a `sequence` with `job_id == "3bc4e11d29b1"` whose key contains `mcp__google__get_gmail_message_content`. *Catches:* n-grams not computed per job; ranking by count instead of money (the web n-gram appears as often but is worth less).
  - `test_reread_found_for_calendar_job`: 7 consecutive daily runs of `cc68e24f21cd`; run k (k = 1..7) reads refs `r{k}`..`r{k+6}`, one `get` per ref. Expected: a `reread` candidate with `occurrences == 36` (6 consecutive pairs × 6 shared refs). *Catches:* off-by-one in "consecutive", counting first reads, or comparing non-consecutive runs (which would give more than 36).
  - `test_schema_lookup_found`: 25 sessions each with 3 `tool_describe` → candidate present; 19 sessions → absent. *Catches:* threshold boundary (≥20) wrong.
  - `test_sequence_below_session_threshold_dropped`: 9 sessions → no candidate; 10 → candidate. *Catches:* `>` vs `≥`.
  - `test_sessions_older_than_28_days_ignored`. *Catches:* unbounded history window.
  - `test_scrub_round_trip_keeps_detection_identical`: `detect(load(dump(s)))` equals `detect(s)` (ignoring `skill_load`). *Catches:* scrub dropping a field the detector needs.
  - `test_scrub_output_has_no_long_strings`: decompress the dump, walk all JSON strings; every string is ≤ 64 chars and none equals a raw `ref` from the input. *Catches:* leaking result text or raw message ids into a public fixture.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): deterministic detector and scrubbed fixture format`.

### Task 4: Copy detector and limits

**Files:** Create `tuner/hermes_tuner/fingerprint.py`, `tuner/hermes_tuner/limits.py`, `tuner/tests/test_fingerprint.py`, `tuner/tests/test_limits.py`

**Interfaces:**
- Produces (`fingerprint.py`): `class CopyIndex` with `build(texts: Iterable[str]) -> CopyIndex` and `contains_copy(text: str) -> bool`. Winnowing:
  - normalise whitespace runs to one space;
  - k-grams of k = 24 chars, hashed with a **rolling** polynomial hash (base 257, modulus 2⁶¹ − 1), O(1) per position. A per-k-gram hash such as FNV is too slow on the roughly 14M characters in 14 days of results (ESTIMATE);
  - window w = 17; keep the rightmost minimum hash per window;
  - fingerprints stored in a sorted, deduplicated `array('Q')`; lookup by `bisect`.

  Guarantee: any shared run of ≥ 40 (k + w − 1) characters is detected. Shorter runs *may* be detected; nothing is guaranteed below 40.
- Produces (`model.py` addition): `Fix(id: str, kind: Literal["composed_tool", "job_prompt", "config", "skill", "new_reach"], target: str, payload: dict, candidate_key: str)`. Payloads:
  - `composed_tool`: `{"name", "description", "parameters", "calls": [..], "mutating": bool, "program": str, "job_prompt": str | None}`
  - `job_prompt`: `{"prompt": str, "add_toolsets": [..]}`
  - `config`: `{"key", "value"}`
  - `skill`: `{"path", "text"}`
  - `new_reach`: `{"change": {"type": "mcp_include_add", "server", "tool"} | {"type": "callable_add", "tool"}, "why": str}`
- Produces (`limits.py`): `LimitContext(copy_index: CopyIndex, scan: Callable[[str], list[str]], bundled_skills: frozenset[str], skills_root: Path, approved_callables: frozenset[str], existing_tools: frozenset[str])`; `check_fix(fix: Fix, ctx: LimitContext) -> list[str]`, returning reasons (empty = allowed).

- [ ] **Step 1: Failing tests:**
  - `test_every_40_char_copy_detected` (`test_fingerprint.py`): `rng = random.Random(1234)`; 50 trials, each with a corpus of 2,000 random letters and a text made of 300 fresh random letters with a 40-char slice of the corpus inserted at a random offset. Every trial → `True`. *Catches:* wrong k/w or tie handling breaking the guarantee. A fixed seed means no flakiness.
  - `test_no_shared_kgram_not_detected`: same seed; the text shares at most 23 consecutive characters with the corpus (shorter than k) → `False` in all 50 trials. *Catches:* an index that matches everything (for example, every hash 0). A 39-char negative is deliberately **not** asserted: winnowing does not guarantee it.
  - `test_whitespace_normalised`: corpus `"a  b\n c"*20`; text with single spaces → detected. *Catches:* missing normalisation.
  - `test_config_key_must_be_allowlisted`: `tools.tool_search.enabled` → ok; `display.foo` → reason; `mcp_servers.google.tools.include` → reason; `terminal.backend` → reason. *Catches:* allowlist as prefix match on the wrong side, or forbidden list unchecked.
  - `test_forbidden_beats_allowlist`: monkeypatch `policy.CONFIG_ALLOW_PREFIXES = ("",)` → `mcp_servers.x` still rejected. *Catches:* forbidden list consulted only when allowlist misses.
  - `test_composed_tool_calls_must_be_callable`: calls `["mcp__google__get_gmail_message_content"]` → ok; `["terminal"]` → reason; `["tuned_other"]` → reason; `["delegate_task"]` → reason; `"name"` without `tuned_` → reason. *Catches:* composition widening reach.
  - `test_approved_callable_extends_allowlist`: `approved_callables={"mcp__google__manage_gmail_label"}` makes that call ok. *Catches:* approvals ignored.
  - `test_job_prompt_only_for_editable_jobs`: `cc68e24f21cd` ok; `6fcdee8dee69` → reason; `8da9a6857256` → reason. *Catches:* editing jobs without a quality check.
  - `test_bundled_skill_rejected`: path under `skills_root/research/arxiv/SKILL.md` with `"arxiv" in bundled_skills` → reason; non-bundled → ok; path outside `skills_root` (`../../.env`) → reason. *Catches:* path traversal, bundled overwrite.
  - `test_scanner_findings_reject`: `scan` returns `["prompt_injection"]` → reason. *Catches:* scanner result ignored.
  - `test_copied_text_rejects_every_text_field`: a copy planted in each of `prompt`, `description`, `program`, skill `text`, `why` → reason each time. *Catches:* a field left out of the copy check.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): copy detector and fix limits`.

### Task 5: Hermes plugin — composed tools, sandbox relay, shadow guard

**Files:** Create `tuner/plugin/hermes-tuner/plugin.yaml`, `__init__.py`, `relay.py`, `stub.py`, `tuner/tests/test_relay.py`, `tuner/tests/test_plugin.py`

**Interfaces:**
- Consumes: `hermes_tuner.policy` (the plugin inserts the repo's `tuner/` dir into `sys.path`, resolved from `Path(__file__).resolve().parents[2]`).
- Produces:
  - `relay.run_program(program: str, args: dict, call: Callable[[str, dict], str], runner_argv: list[str], *, dry_run: bool, intents: list[dict], timeout_s: int = RUN_TIMEOUT_S, max_calls: int = MAX_CALLS, max_output: int = MAX_OUTPUT_BYTES) -> str` returns the JSON string given to the model.
  - Wire protocol, one JSON object per line:
    - host → stub: `{"program": str, "args": dict}`, then `{"result": str}` per call;
    - stub → host: `{"call": name, "args": dict}` or `{"done": value}` or `{"error": str}`.
  - When `dry_run` and `name in MUTATING_TOOLS`, the relay does not call `call`. It appends `{"tool": name, "args": args}` to `intents` and returns `{"dry_run": true}`.
  - Program contract: `def main(args: dict, call) -> object`, where `call(name, args)` returns the parsed JSON result (or the raw string if not JSON).
  - The relay refuses any call whose name is not in `allowed = CALLABLE_TOOLS ∪ approved` and returns `{"error": "<name> is not callable"}` to the program. `approved` is the set of `callable_add` tools in `/home/hermes/.hermes/broker/state/approved_proposals.json`, read at call time; the plugin never writes that file. In production `call` is `ctx.dispatch_tool(name, args)`.
  - `__init__.register(ctx)`:
    - registers each `~/.hermes/tuner/tools/*/tool.json` as `tool.json["name"]` in toolset `tuner`, with a handler `(args, **kw) -> str`;
    - registers hook `pre_tool_call` → `shadow_guard`;
    - registers hook `on_session_start` → re-scan the tools dir.
  - `shadow_guard(tool_name, args, session_id="", **_) -> dict | None` (hook kwargs measured in `hermes_cli/plugins.py:2164`: `tool_name, args, task_id, session_id, tool_call_id, turn_id, api_request_id, middleware_trace`) returns `{"action": "block", "message": "dry run: recorded"}` and appends to `~/.hermes/tuner/shadow/<session_id>.jsonl` when the session belongs to a shadow job and `tool_name in policy.SHADOW_BLOCK`.
  - `SHADOW_BLOCK = MUTATING_TOOLS | {"skill_manage", "memory", "cronjob", "send_message", "delegate_task", "execute_code"} | every mcp__compute__* tool not in the five read-only ones`. `execute_code` is blocked because its relayed calls may not pass through this hook.
  - The composed-tool handler forwards its own `session_id`/`task_id` kwargs to `ctx.dispatch_tool`. Shadow session = id matches `cron_<id>_…` where `<id>` is in `shadow_jobs.json`, **or** the job named `tuner-shadow-*` per a jobs.json lookup at hook time. The second check covers a lost ledger (Review Focus 4).
  - In a shadow session the handler forces `dry_run=True` and writes `intents` to the same jsonl.

- [ ] **Step 1: Failing tests** (`runner_argv = [sys.executable, "-u", str(stub_path)]` in tests):
  - `test_program_calls_relay_and_returns_value`: program calls `search` then `get` twice; fake `call` records names; result JSON equals `main`'s return. *Catches:* protocol framing broken.
  - `test_call_outside_allowlist_refused`: program calls `terminal` → result contains `"not callable"`, fake `call` never invoked. *Catches:* relay trusting the program.
  - `test_dry_run_records_mutations_without_calling`: `dry_run=True`, program calls `mcp__google__modify_gmail_message_labels` → `intents == [{"tool": ..., "args": ...}]`, fake `call` not invoked for it. *Catches:* shadow runs mutating the mailbox.
  - `test_timeout_kills_runner` (Review Focus 3): program `while True: pass`, `timeout_s=2` → returns error JSON within 5 s, and the child process is dead (`proc.poll() is not None`). *Catches:* hung program blocking the gateway thread.
  - `test_call_limit`: 51 calls, `max_calls=50` → error mentioning the limit, and exactly 50 forwarded. *Catches:* off-by-one in the call cap.
  - `test_output_cap`: return 60,000 chars → error, model sees ≤ 50,000 bytes. *Catches:* unbounded output into context.
  - `test_shadow_guard_blocks_by_name_without_ledger` (`test_plugin.py`, Review Focus 4): empty `shadow_jobs.json`; a fake jobs file with job `abc123abc123` named `tuner-shadow-x`; `shadow_guard("mcp__google__manage_event", {...}, session_id="cron_abc123abc123_20261020_074000")` → block, and a jsonl line is written. A non-shadow session → `None`. *Catches:* guard depending only on tuner state.
  - `test_shadow_guard_blocks_memory_and_gated_compute`: `memory`, `mcp__compute__run_task` and `execute_code` are blocked in a shadow session; `mcp__compute__night_status` is not. *Catches:* a block list limited to Gmail/file writes.
  - `test_registration_reads_tool_dir`: a fake ctx records `register_tool` calls; two tool dirs → two registrations with toolset `tuner`, and names starting `tuned_`. A dir whose name lacks the prefix is skipped. *Catches:* loader registering arbitrary names.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. Production `runner_argv` = `policy.SANDBOX_ARGV + ["python3", "-u", "-c", STUB_SOURCE]`. Kill with `proc.kill()`, then `docker rm -f` of the container name `tuned-<uuid>` passed via `--name`. **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): Hermes plugin for composed tools and shadow guard`.

### Task 6: Bridge and ledger (apply, versions, rollback)

**Files:** Create `tuner/hermes_tuner/bridge.py`, `tuner/hermes_tuner/bridge_entry.py`, `tuner/hermes_tuner/ledger.py`, `tuner/tests/test_ledger.py`

**Interfaces:**
- Produces (`bridge.py`): `class Bridge(Protocol)` with:
  - `scan(text) -> list[str]`
  - `get_job(job_id) -> dict | None`, `list_jobs() -> list[dict]`, `update_job(job_id, updates: dict) -> dict`, `create_job(prompt: str, schedule: str, name: str, deliver: str, enabled_toolsets: list[str] | None) -> dict`, `remove_job(job_id) -> bool`
  - `config_get(key) -> Any`, `config_set(key, value) -> None`
  - `oneshot(instructions: str, user_input: str, max_tokens: int) -> dict` with keys `text`, `input_tokens`, `output_tokens`, `estimated_cost_usd`
  - `restart_gateway() -> None`

  Also `class BridgeError(Exception)`; `class HermesBridge(Bridge)`, which runs `[HERMES_PY, bridge_entry.py, <op>]` with JSON on stdin/stdout, timeout 120 s, and raises `BridgeError` on non-zero exit or non-JSON output; `restart_gateway` runs `systemctl --user restart hermes-gateway`. And `class FakeBridge(Bridge)`, in-memory, for tests.
- `bridge_entry.py` operations map to:
  - `tools.threat_patterns.scan_for_threats(text, scope="context")`;
  - `cron.jobs.get_job`, `list_jobs`, `update_job`, `create_job`, `remove_job`;
  - `hermes config get/set` via `hermes_cli.config`. The builder confirms the function names in the pinned source; the CLI `hermes config set` is acceptable.
  - `agent.auxiliary_client.call_llm`, with the main provider/model from config, plus `agent.usage_pricing.estimate_usage_cost`.
- Produces (`ledger.py`):
  - `FixRecord(id, kind, target, candidate_key, state: Literal["rejected", "shadow_pending", "shadow_failed", "trial", "kept", "rolled_back", "owner_changed", "proposed", "approved", "applied", "dropped", "bridge_error"], created_at: float, applied_at: float | None, trial_runs: int, baseline_cost: float | None, reason: str, spend_usd: float, needs_restart: bool)`. `needs_restart` is True for `config` and `mcp_include_add` fixes, and for `composed_tool` fixes unless Task 11 Step 5 shows that the session-start re-scan works.
  - `class Ledger` with `load(root: Path) -> Ledger`, `save()`, `records`, `active_for(target: str) -> FixRecord | None`.
  - `apply(fix: Fix, ledger: Ledger, bridge: Bridge, root: Path, now: float) -> FixRecord`. Before applying, it writes `versions/<id>/before.json` (current value read through the bridge or file), then applies, then writes `after.json`.
  - `rollback(record: FixRecord, ledger, bridge, root, now) -> FixRecord`.

- [ ] **Step 1: Failing tests** (FakeBridge, tmp root):
  - `test_apply_job_prompt_saves_before_and_after`: after `apply`, `before.json` has the old prompt, the bridge job has the new one, and the record state is `trial`. *Catches:* no rollback point.
  - `test_rollback_restores_exact_previous_value`: config `tools.tool_search.enabled` `auto` → `off` → rollback → `auto`. *Catches:* rollback to a default rather than the saved value.
  - `test_rollback_skips_owner_edit` (Review Focus 2): after apply, FakeBridge job prompt is changed to `"owner text"`; rollback leaves `"owner text"` and sets state `owner_changed`. *Catches:* clobbering the owner's hand edit.
  - `test_skill_apply_writes_file_atomically_and_rolls_back`: `SKILL.md` content swapped, then restored byte-for-byte. *Catches:* partial writes; lost original.
  - `test_composed_tool_apply_writes_tool_dir_and_rollback_removes_it`. *Catches:* orphaned tools after rollback.
  - `test_bridge_error_marks_record_and_changes_nothing`: FakeBridge raises on `update_job` → state `bridge_error`, `before.json` exists, and no partial change is recorded as applied. *Catches:* half-applied fixes.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement (`bridge_entry.py` is exercised on the droplet in Task 11, not in CI). **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): bridge to Hermes and versioned fix ledger`.

### Task 7: Drafting

**Files:** Create `tuner/hermes_tuner/draft.py`, `tuner/tests/test_draft.py`

**Interfaces:**
- Consumes: `Candidate`, `Fix`, `Bridge.oneshot`.
- Produces:
  - `build_request(candidate: Candidate, job: dict | None, skill_text: str | None, tool_schemas: dict[str, dict], call_shape: list[tuple[str, tuple[str, ...]]]) -> tuple[str, str]` returns instructions and input.
  - `parse_draft(text: str, candidate: Candidate) -> Fix | None`.
  - `draft(candidate, ..., bridge) -> tuple[Fix | None, float]` returns the fix and its spend.

Instructions (fixed text, in the module) state:
- the five fix kinds and their payload schemas (Task 4);
- the program contract (Task 5);
- the callable allowlist;
- that a mutating composed tool must accept `dry_run`;
- that tool descriptions describe what the tool does from its structure;
- that the reply is a single JSON object.

`call_shape` is the tool-name and argument-key sequence only.

- [ ] **Step 1: Failing tests:**
  - (The no-result-text guarantee is tested end to end in Task 10, `test_no_result_text_reaches_drafting_model`. A unit test here could not fail, because `build_request` takes no sessions.)
  - `fix.id` is deterministic: `f"{yyyymmdd(now)}-{sha256(candidate.key + json.dumps(payload, sort_keys=True))[:8]}"`. Test `test_fix_id_stable_for_same_draft`. *Catches:* random ids making ledger lookups and shadow names unstable.
  - `test_parse_accepts_valid_job_prompt_fix`, `test_parse_rejects_unknown_kind`, `test_parse_rejects_non_json_and_trailing_prose` (returns `None`). *Catches:* lenient parsing turning prose into fixes.
  - `test_draft_returns_spend_even_when_unparseable`. *Catches:* failed drafts not counted against the cap.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): drafting request and parser`.

### Task 8: Shadow runs, quality comparison, trial verdicts

**Files:** Create `tuner/hermes_tuner/trial.py`, `tuner/tests/test_trial.py`

**Interfaces:**
- Consumes: `Bridge.create_job/remove_job/list_jobs`, `History.calls_with_args`, `job_stats`, `Ledger`.
- Produces:
  - `schedule_shadow(fix: Fix, job: dict, bridge: Bridge, root: Path) -> str` (shadow job id):
    - one-shot at the live job's `next_run_at − SHADOW_LEAD_MIN` minutes, name `SHADOW_PREFIX + fix.id`, `deliver="local"`;
    - prompt = the fix's new prompt (`job_prompt.prompt` or `composed_tool.job_prompt`);
    - `enabled_toolsets`: when the live job's value is a list, use that list plus `"tuner"` if a composed tool is involved. When it is `None` (as for morning-briefing), keep `None`, because a list would *narrow* the job to only those toolsets. Composed tools reach `None` jobs through the cron platform's toolset config, enabled by hand in Task 11 Step 5. The same rule applies to `job_prompt.add_toolsets` in `ledger.apply`;
    - records the id in `shadow_jobs.json`.
  - `triage_decisions(calls: list[tuple[str, dict]], intents: list[dict]) -> dict[str, str]` maps message id to `KEEP|TRASH|ARCHIVE`:
    - TRASH when `add_label_ids` contains `TRASH`;
    - ARCHIVE when `remove_label_ids` contains `INBOX`;
    - KEEP when read but not modified.
  - `compare_triage(old: dict, new: dict) -> Verdict(ok: bool, agreement: float, flips: list[str])`, over message ids in both; `ok = agreement ≥ 0.90 and not flips`. Flips are ids that are `KEEP` in old and `TRASH`/`ARCHIVE` in new.
  - `calendar_events(calls, intents) -> set[tuple[str, str]]` from `mcp__google__manage_event` calls with `action == "create"`: `(summary.casefold().strip(), start_time[:10])`. These are the argument names measured in the droplet DB on 2026-10-19: `action, summary, start_time, end_time, description, location, reminders, timezone`. Triage args measured the same day: `message_id`, `add_label_ids`, `remove_label_ids`.
  - `compare_calendar(old, new) -> Verdict`: `ok = old ⊆ new`.
  - `trial_verdict(record: FixRecord, stats_now: JobStats | None, prompt_baseline: float) -> Literal["continue", "keep", "rollback"]`:
    - `stats_now` covers only sessions started after `record.applied_at`;
    - `continue` while `stats_now is None or stats_now.runs < JOB_TRIAL_RUNS`;
    - compare costs when `record.baseline_cost is not None and record.baseline_cost > 0`. Otherwise (provider billing reported as included, so costs are $0) compare `median_prompt_tokens` against `prompt_baseline`;
    - `keep` if strictly lower, else `rollback`.
  - `record.baseline_cost` is set when the fix goes live: `job_stats` over that job's sessions in `[applied_at − 28 days, applied_at)`.
  - `cleanup_shadows(bridge, root, now) -> list[str]` removes any job named `tuner-shadow-*` whose run time has passed by over 1 h.

- [ ] **Step 1: Failing tests:**
  - `test_keep_to_trash_is_a_flip_even_at_high_agreement`: 99 agree, 1 KEEP→TRASH → `ok False`. *Catches:* averaging away the one failure the owner cares most about.
  - `test_agreement_boundary`: 9/10 agree, no flips → ok; 8/10 → not ok. *Catches:* threshold off-by-one.
  - `test_messages_only_new_version_saw_are_ignored`: new version skipped already-checked mail → not counted as disagreement. *Catches:* the filter fix failing its own check.
  - `test_calendar_missing_event_fails`, `test_calendar_title_case_and_space_insensitive`. *Catches:* strict string match failing on cosmetic differences; missing-event check absent.
  - `test_trial_continue_until_five_runs_then_judges`: 4 runs → `continue`; 5 runs cheaper → `keep`; 5 runs equal cost → `rollback`. *Catches:* `≤` vs `<`, early verdicts.
  - `test_zero_cost_baseline_falls_back_to_tokens`: `baseline_cost = 0.0`, every run cost 0.0, prompt tokens down 40% → `keep`; tokens up → `rollback`. *Catches:* a falsy-zero guard (`if record.baseline_cost:`) or a `0 < 0` comparison rolling back every fix once billing reads $0.
  - `test_runs_before_applied_at_not_counted`. *Catches:* the old version's runs counting toward the new version's trial.
  - `test_shadow_scheduled_before_live_run_with_local_delivery`: FakeBridge job `next_run_at` 08:00 → shadow schedule 07:40, `deliver == "local"`, `"tuner" in enabled_toolsets` for a composed-tool fix. *Catches:* shadow messaging the owner, or running after the live job changed the inbox.
  - `test_cleanup_removes_stale_shadow_jobs` (Review Focus 4). *Catches:* orphaned one-shot jobs.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS.
- [ ] **Step 5:** Commit — `feat(tuner): shadow runs and quality checks`.

### Task 9: New-reach proposals and the approvals bot

**Files:**
- Create: `tuner/hermes_tuner/proposals.py`, `tuner/tests/test_proposals.py`, `broker/hermes_broker/proposals.py`, `broker/tests/test_proposals.py`
- Modify: `broker/hermes_broker/approvals.py` (`_TOOL` regex, `drop` routing, `Announcer.tick` announces proposals and sends outbox files), `broker/hermes_broker/config.py` (optional `tuner_dir`, default `/home/hermes/.hermes/tuner`), `broker/hermes_broker/listen.py` (wire `ProposalStore`), `broker/tests/test_approvals.py`, `broker/tests/test_announcer.py`

**Interfaces:**
- Tuner side:
  - `write_proposal(fix: Fix, root: Path) -> str` writes `proposals/<id>.json` = `{"id", "change", "why", "hash"}`, where `hash = sha256(json.dumps(change, sort_keys=True, separators=(",", ":")))`.
  - `approved(root: Path, broker_state: Path) -> list[dict]` reads `broker_state/approved_proposals.json` read-only and returns entries whose hash equals the proposal file's current hash.
  - `apply_approved(entry, bridge, ledger) -> FixRecord`:
    - `mcp_include_add` → `config_set("mcp_servers.<server>.tools.include", old + [tool])`, the only path allowed to touch that key, plus `needs_restart`;
    - `callable_add` → recorded in the ledger; the plugin reads approved callables from the same broker file.
- Broker side:
  - `ProposalStore(path)` with `announce(proposal_id, change_hash, now, avoid) -> str | None` and `take(code, now) -> dict | None`, the same shape and 7-day TTL as `TaskStore`.
  - `record_approval(path, proposal_id, change_hash, now)` appends to `approved_proposals.json` atomically, mode 0600.
  - `parse_command` returns `("tool", "NNNN")` for `^tool\s+(\d{4})$` (case-insensitive).
  - `drop NNNN`: if the code is a live `ProposalStore` code → proposal dropped, otherwise → existing task path.
  - Codes avoid `grants.pending_codes ∪ tasks.live_codes ∪ proposals.live_codes`.
  - Announcement: `Tool proposal <id>\n\n<why>\n\nChange: <canonical change JSON>\n\n—— reply below ——\ncode NNNN\ntool NNNN | drop NNNN`, with any `—— reply below ——` inside `why` removed, as `format_announcement` does.
  - Outbox: each `tuner_dir/outbox/*.txt` is sent once, then renamed to `*.sent`. It is resent next tick when `_send` returns False.

- [ ] **Step 1: Failing tests:**
  - `test_tool_verb_parsed_and_forward_rejected` (broker). *Catches:* forwarded text approving a proposal.
  - `test_drop_routes_to_proposal_when_code_is_a_proposal`, and `test_drop_routes_to_task_otherwise`. *Catches:* `drop` on a proposal closing a GitHub issue.
  - `test_codes_unique_across_three_stores`: with `secrets.randbelow` patched to return taken codes first, the new code avoids all three sets. *Catches:* code collision approving the wrong thing.
  - `test_edited_proposal_not_approved` (tuner): approval hash recorded, then the proposal file's change is edited → `approved()` returns nothing. *Catches:* approving one change and applying another.
  - `test_separator_in_why_stripped`. *Catches:* fake reply instructions inside the proposal text.
  - `test_outbox_sent_once_and_retried_on_failure`. *Catches:* duplicate weekly lines or lost ones.
- [ ] **Step 2:** run both suites → FAIL. **Step 3:** implement. **Step 4:** `cd broker && uv run pytest -q` and `cd tuner && uv run pytest -q` → PASS (existing broker tests still pass).
- [ ] **Step 5:** Commit — `feat: new-reach proposals through the approvals bot`.

### Task 10: The daily cycle, budget, restart window, weekly line, CLI

**Files:** Create `tuner/hermes_tuner/restart.py`, `tuner/hermes_tuner/cycle.py`, `tuner/hermes_tuner/__main__.py`, `tuner/tests/test_restart.py`, `tuner/tests/test_cycle.py`

**Interfaces:**
- Produces:
  - `restart_allowed(now: datetime, last_activity: float, next_runs: list[float]) -> bool`, per Global Constraints.
  - `CycleConfig(root: Path, db: Path, broker_state: Path, skills_root: Path, dry_run: bool)`.
  - `run_cycle(cfg, bridge: Bridge, now: float) -> CycleReport(applied: list[str], kept: list[str], rolled_back: list[str], rejected: list[str], proposed: list[str], errors: list[str], spend_usd: float)`.
  - `weekly_line(ledger, sessions, now) -> str` gives exactly one line: `Hermes tuner: saved ~$X.XX this week (ESTIMATE), N applied, K kept, R rolled back; tuner spent $S.SS`. With errors it appends `; errors: <count> (see ~/.hermes/tuner/ledger.json)`.
  - CLI:
    - `python -m hermes_tuner run [--dry-run]` (dry run applies nothing and prints the report);
    - `python -m hermes_tuner detect --db PATH [--fixture PATH] [--now YYYY-MM-DD]` prints the top 10 candidates as JSON lines. `--now` anchors the 28-day window; the default is the newest session start;
    - `python -m hermes_tuner scrub --db PATH --out PATH`.

Cycle order, holding `fcntl.flock` on `root/.lock` (non-blocking; a second run exits 0 with "already running"):
1. `cleanup_shadows`.
2. Evaluate pending shadows: compare and either move to `trial` (apply) or `shadow_failed`.
3. Evaluate trials: `keep` or `rollback`. Config and skill fixes judge after `GLOBAL_TRIAL_DAYS`: roll back if the editable jobs' or chats' median cost rose.
4. Apply approved proposals.
5. Budget check: week spend from the ledger plus shadow-session costs must be under the cap, and the 28-day spend must not exceed the 28-day savings. If either fails, skip drafting.
6. `detect`. Take the top candidate per editable job with no active fix, plus the top `schema_lookup`/`skill_load` candidate when no global fix is active.
7. For each: draft → `check_fix` → `rejected`, or schedule a shadow (job fixes), or apply into trial (config/skill), or `write_proposal` (`new_reach`).
   - `bundled_skills` = the names before `:` on each line of `skills_root/.bundled_manifest`.
   - The copy index is built from `History.untrusted_texts(now − 14 days)`.
   - The old run compared with a shadow is the first live session of the same job that started after the shadow session.
8. Restart if any applied fix needs one and `restart_allowed`.
9. On the first run of an ISO week, write `outbox/weekly-<YYYY>-W<ww>.txt`.

- [ ] **Step 1: Failing tests:**
  - `test_restart_window_boundaries` (Europe/London, including a BST/GMT changeover date): 01:59 no, 02:00 yes, 04:59 yes, 05:00 no; activity 29 min ago no; a job due in 14 min no. *Catches:* timezone fixed to UTC; inclusive/exclusive bounds.
  - `test_cycle_respects_one_active_fix_per_job`: an active trial on `3bc4e11d29b1` → no new draft for it. *Catches:* overlapping changes that can't be told apart.
  - `test_budget_exhausted_skips_drafting_but_still_evaluates`: week spend 0.99 + projected 0.12 → no `oneshot` call; trials still judged. *Catches:* cap ignored, or the cap freezing rollbacks.
  - `test_spend_exceeding_savings_pauses_drafting`. *Catches:* a tuner that costs more than it saves.
  - `test_bridge_failure_applies_nothing_and_reports` (Review Focus 5): FakeBridge raises `BridgeError` on every call → report `errors` non-empty, ledger has no `trial` record, and the weekly line contains `errors:`. *Catches:* crashing or half-applying on a Hermes update.
  - `test_second_concurrent_run_exits`. *Catches:* two runs applying the same fix.
  - `test_dry_run_applies_nothing`: `--dry-run` → FakeBridge received no mutating call. *Catches:* the deploy-time dry run changing things.
  - `test_weekly_line_once_per_iso_week`. *Catches:* daily spam.
  - `test_no_result_text_reaches_drafting_model`: in a factory DB, every untrusted tool result contains `ZZ_SENTINEL_9f2c`, and the arg value `query` in search calls is `ZZ_SENTINEL_q`. Run the cycle with a FakeBridge that records every `oneshot` input. Neither sentinel appears in any recorded instructions or input. *Catches:* the cycle passing result text or argument **values** (which can carry email text) into drafting.
  - `test_weekly_saving_is_baseline_minus_actual`: one kept fix, baseline $0.12, three runs this week at $0.05 → line says `saved ~$0.21`. *Catches:* savings counted from the wrong runs or with the sign reversed.

`weekly_line` saving = Σ over job fixes in `trial` or `kept`, of Σ over that job's runs this ISO week of `(baseline_cost − run.est_cost)`. Config/skill fixes are not counted in the dollar figure, only in "applied". Config/skill trials: after `GLOBAL_TRIAL_DAYS`, keep only if the median per-run cost across chat sessions and editable-job runs since `applied_at` is strictly lower than over the 28 days before (token fallback as in Task 8); otherwise roll back.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS (full suite).
- [ ] **Step 5:** Commit — `feat(tuner): daily cycle, budget, restart window and weekly line`.

### Task 11: Droplet install, answer-key check, scrubbed fixture, runbook

This task touches the droplet. The orchestrator (not a subagent) runs every `ssh hermes-vm` step, showing the owner each command first. The droplet is deployed by hand (runbooks/dispatch.md).

**Files:** Create `droplet/install-tuner.sh`, `droplet/units/hermes-tuner.service`, `droplet/units/hermes-tuner.timer`, `runbooks/tuner.md`, `tuner/tests/fixtures/history-2026-10.json.gz` (scrubbed), `tuner/tests/test_answer_key.py`

- [ ] **Step 1:** Write `install-tuner.sh`:
  - `cd ~/hermes-harness/tuner && uv sync --no-dev`;
  - `ln -sfn ~/hermes-harness/tuner/plugin/hermes-tuner ~/.hermes/plugins/hermes-tuner`;
  - `mkdir -p -m 700 ~/.hermes/tuner`;
  - copy the units to `~/.config/systemd/user/` and run `systemctl --user daemon-reload`;
  - do **not** enable the timer.

  The service is `Type=oneshot`, `ExecStart=%h/hermes-harness/tuner/.venv/bin/python -m hermes_tuner run`. The timer is `OnCalendar=*-*-* 03:30:00 Europe/London`, `Persistent=false`. Shellcheck clean.
- [ ] **Step 2: Answer-key run on the droplet** (read-only):
  `ssh hermes-vm 'cd ~/hermes-harness/tuner && .venv/bin/python -m hermes_tuner detect --db ~/.hermes/state.db --now 2026-10-04'`.

  `--now 2026-10-04` puts the window before the credit outage (jobs failed 2026-10-04 to 10-17). With a window ending 10-19, fewer than 10 runs per job would qualify and the sequence threshold could hide the answer. `test_answer_key.py` uses the same anchor.
  The top 5 must include all three:
  - a `sequence` for job `3bc4e11d29b1` or `cc68e24f21cd` containing `mcp__google__get_gmail_message_content`;
  - `reread:cc68e24f21cd`;
  - `schema_lookup`.

  Paste the output into the PR body. If one is missing, return to Task 3 with the output; do not tune thresholds blindly.
- [ ] **Step 3: Scrubbed fixture:**
  - Run `scrub` on the droplet to `/tmp/h.json.gz`, restricted to the 2026-09 and 2026-10 sessions.
  - Check `ls -l` and `sha256sum` on the droplet.
  - Stream it back: `ssh hermes-vm 'cat /tmp/h.json.gz' > tuner/tests/fixtures/history-2026-10.json.gz`. The file is under 1 MB; verify locally with `sha256sum`.
  - Run `test_scrub_output_has_no_long_strings` logic on it: write `test_answer_key.py`, which asserts the same three candidates on the fixture and that no string in it exceeds 64 chars.
  - Commit only if both pass.
- [ ] **Step 4: Bridge smoke** on the droplet. These are read-only operations; nothing is written:
  - `bridge_entry.py scan` on the text `ignore previous instructions` → `["prompt_injection"]`;
  - `list_jobs` → 7 jobs;
  - `config_get tools.tool_search` → value printed.

  Also, spec §7.3 (no network from the sandbox): call `relay.run_program` with the production `SANDBOX_ARGV` and a program doing `urllib.request.urlopen("https://example.com", timeout=5)`. The result must be an error containing a resolution or connection failure. Paste it.
- [ ] **Step 5: Plugin enable and visibility check** (needs one gateway restart, with the owner's go-ahead, in the quiet window):
  - Plugins in `~/.hermes/plugins/` are **opt-in** (`hermes_cli/plugins.py:246`: only plugins named in `plugins.enabled` load). The owner adds `hermes-tuner` to `plugins.enabled`, by hand, once; the builder confirms the exact registry key in `plugins.py:309`. This is a `plugins.*` key, which the tuner itself may never change.
  - Install a no-op composed tool `tuned_ping` and restart. The owner asks Hermes in Telegram to list tools whose names start `tuned_`; the orchestrator confirms from `state.db` that the session called `tool_search`/`tool_describe` and found it;
  - run a one-shot cron job with `enabled_toolsets` including `tuner` and check that `tuned_ping` is callable;
  - record whether `on_session_start` re-scan made a second test tool visible without a restart, and set `needs_restart` for `composed_tool` fixes accordingly;
  - if the `tuner` toolset is not on for Telegram, the owner adds it by hand: `hermes config set platform_toolsets.telegram '["hermes-telegram","tuner"]'` (a one-time manual change, not a tuner fix). Likewise for the `cron` platform, so jobs whose `enabled_toolsets` is `None` (morning-briefing) see composed tools.
- [ ] **Step 6: Dry cycle:** `python -m hermes_tuner run --dry-run` on the droplet; paste the report.
- [ ] **Step 7:** Write `runbooks/tuner.md`:
  - what the tuner does and where its state lives;
  - how to read the ledger;
  - how to roll back a fix by hand (`versions/<id>/before.json`);
  - how to pause it (`systemctl --user disable --now hermes-tuner.timer`);
  - the `tool`/`drop` replies;
  - that enabling the timer is the owner's act after reviewing the dry-cycle report.
- [ ] **Step 8:** Commit `droplet/install-tuner.sh droplet/units/hermes-tuner.* runbooks/tuner.md tuner/tests/fixtures/history-2026-10.json.gz tuner/tests/test_answer_key.py` — `feat(tuner): droplet install, answer-key fixture and runbook`.

## Not in this plan

- Enabling `hermes-tuner.timer`. That is the owner's act after Step 6.
- Gmail server-side filters, fixes on tig-server or tig-gpu, model changes, and per-fix chat measurement (spec "Out of scope").
- The `SOUL.md` scanner block (separate small fix).
