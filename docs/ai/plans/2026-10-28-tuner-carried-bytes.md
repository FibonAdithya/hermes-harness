# Tuner carried-bytes, reducers and replay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tuner rank waste by the text the model reads, ship deterministic result reducers for calendar sync, replay reducers and composed tools against history before they go live, back off on repeated rejections, and roll back failing trials early.

**Architecture:**
- **History and detection.** `history.py` gains turn positions per call and a text-bearing replay view. `detect.py` gains a `bytes` signal and a turns-removed estimate for `sequence` candidates.
- **New modules.** `reducers.py` is a pure transform library, imported by both the host and the plugin. `replay.py` measures reducers and composed tools on recorded runs. `backoff.py` classifies past outcomes.
- **Lifecycle.** The cycle routes a calendar-sync `bytes` candidate to a reducer search instead of a model draft. Reducers then follow the existing shadow → trial → keep/rollback path as a new job fix kind.
- **Plugin.** It registers a `transform_tool_result` hook that applies live reducers.

**Tech Stack:** Python 3.12, uv, pytest, stdlib only (`difflib`, `html.parser`, `urllib.parse`). Everything lives in `tuner/`.

**Spec:** `docs/ai/specs/2026-10-28-tuner-carried-bytes-design.md`, which extends `docs/ai/specs/2026-10-19-hermes-tuner-design.md`. Read both.

All paths below are relative to `tuner/` unless they start with `docs/`. Run tests from `tuner/` with `uv run pytest -q <path>`.

## Global Constraints

**Data handling**
- No real email text enters the repository. Committed fixtures come from `scrub.py`, which keeps no result text or argument values, or are synthetic text written for the tests.
- Replay reads result text only inside the tuner process. What leaves it is counts, booleans and reason codes. That applies to ledger reasons, cycle errors, log lines, `cycle_state.json`, the weekly line and `replay-check` output.
- Reducers make no model calls.

**Reducer scope**
- `policy.REDUCER_JOBS = frozenset({"cc68e24f21cd"})`. Calendar sync only (owner's decision, 2026-10-28).
- `policy.REDUCER_TOOLS = frozenset({"mcp__google__get_gmail_message_content", "mcp__google__search_gmail_messages", "mcp__google__get_events"})`.
- `policy.MEETING_HOSTS = frozenset({"meet.google.com", "zoom.us", "teams.microsoft.com"})`. A subdomain matches too: `us02web.zoom.us` matches `zoom.us`.

**Thresholds**

| Setting | Value |
|---|---|
| `REPLAY_WINDOW_DAYS` | 28 |
| `USED_MIN_CHARS` | 8 |
| `BOILERPLATE_SHARE` | 0.5 (drop strings in more than half of a tool's results) |
| Used-string survival | 100% |
| `REDUCER_MIN_CUT` | 0.15 |
| `REDUCER_STEP_MIN` | 0.02 |
| `REDUCER_CAP_LADDER` | (16000, 8000, 4000, 2000) |
| `REPLAY_MIN_SESSIONS` | 3 |
| `MIN_DRAFT_MONTHLY_USD` | 0.05 (ESTIMATE) |
| Back-off | skip 7 days after 2 identical outcomes; park after 3 |
| Early rollback | 2 failures, or 10% of the fix's calls |
| `CHARS_PER_TOKEN` | 4 (ESTIMATE; `messages.token_count` is NULL on every tool row) |

**Code conventions**
- Plugin code never raises into Hermes. The hook returns `None` on any exception or non-match.
- Writes use `ledger.atomic_write` (files 0600, directories 0700).
- Tests inject dependencies (bridge fakes, a `run_program` callable, clocks). Never monkeypatch a default argument. A patched default once published to the live wiki.
- Keep existing behaviour for records and fixtures that predate a new field: dataclass defaults, `scrub` v2 still loads.
- Git: stage explicit paths only. Run `git status --short` before each commit.

## Review Focus

1. **A result that isn't JSON, is truncated JSON, or has a JSON leaf that isn't a string** (a number or null). The reducer must transform what it can and return valid output. It must never raise, and must never return an empty string for non-empty input. Test: Task 4, `test_apply_reducer_odd_inputs`.
2. **A reducer file that is corrupt, partly written by hand, or names an unknown op.** The hook must ignore that file and keep applying the others. Test: Task 7, `test_hook_skips_bad_reducer_file`.
3. **A huge result** (200k chars, e.g. a newsletter) compared against many later messages. Used-string extraction must stay bounded. Test: Task 5, `test_used_strings_bounded_time` (under 5 s).
4. **A replay window with no runs**, as during the 2026-10-04…10-17 credit outage. The search returns no reducer with reason code `no_runs` and doesn't crash the cycle. Test: Task 6, `test_search_no_runs`.
5. **The hook firing for a chat session, another job, a friend profile's cron id, or a shadow of a different fix.** Each must leave the result unchanged. Test: Task 7, `test_hook_scoping`.

---

### Task 1: Turn positions and key refs in history

**Files:**
- Modify: `hermes_tuner/model.py` (`ToolCall`, `Session`)
- Modify: `hermes_tuner/history.py` (`_calls`, `_chain_calls`, `sessions`)
- Modify: `hermes_tuner/scrub.py` (VERSION 3)
- Modify: `tests/factory.py` (`make_db` turn layout)
- Test: `tests/test_history.py`, `tests/test_scrub.py`

**Interfaces:**
- Produces:
  - `ToolCall.turn: int = 0`: the 1-based ordinal of the assistant row that issued the call, counted across the run (root first, then continuations).
  - `ToolCall.later_turns: int = 0`: the assistant rows after this tool row in the same `sessions` row.
  - `Session.turns: int = 0`: assistant rows in the run.
  - `ToolCall.ref` now falls back to `args["query"]` when there is no string `message_id`.
- `scrub.VERSION = 3` writes `turn`, `later_turns` and `turns`. Loading a v2 file gives 0 for all three.
- `make_db` accepts calls as `(name, args, result)` or `(name, args, result, turn)`. `turn` is a 1-based assistant row; the default is 1. Sessions accept `"final_turns": int` (default 0): assistant rows with no calls, appended at the end.

- [ ] **Step 1: Write the failing tests**

```python
def test_turn_positions(tmp_path):
    db = make_db(tmp_path / "s.db", [{"id": "cron_cc68e24f21cd_20261024_060034", "source": "cron",
        "started_at": 1.0, "final_turns": 1, "calls": [
            ("mcp__google__search_gmail_messages", {"query": "in:inbox"}, "x" * 10, 1),
            ("mcp__google__get_events", {}, "y" * 10, 1),
            ("mcp__google__get_gmail_message_content", {"message_id": "m1"}, "z" * 10, 2)]}])
    s = History.open(db).sessions(0)[0]
    assert [c.turn for c in s.calls] == [1, 1, 2]
    assert [c.later_turns for c in s.calls] == [2, 2, 1]
    assert s.turns == 3
    assert s.calls[0].ref == "in:inbox" and s.calls[2].ref == "m1"

def test_message_id_wins_over_query(tmp_path): ...  # both args present -> ref is the message_id

def test_later_turns_per_row_not_chain(tmp_path):
    # root ends with compression after 1 turn; continuation adds 2 turns.
    # The root's tool row has later_turns == 1 (its own row only), not 3.

def test_scrub_v3_roundtrip_and_v2_loads(tmp_path): ...  # v3 keeps turn/later_turns/turns; a v2 doc loads with 0s
```

- [ ] **Step 2:** Run `uv run pytest -q tests/test_history.py tests/test_scrub.py`. Expected: the new tests FAIL (`ToolCall` has no `turn`).
- [ ] **Step 3: Implement.**
  - In `_calls`, number assistant rows per `session_id` as they stream, and record the issuing row's ordinal in `issued`.
  - Compute `later_turns` per `sessions` row with a second pass over that session's assistant row ids, using bisect on ids greater than the tool row id.
  - In `_chain_calls`, offset `turn` by the assistant row counts of earlier chain members, so turns are unique within the run.
  - `Session.turns` is the sum of assistant rows over chain members.
  - Update the `scrub` dump and load; `_session` uses `.get(..., 0)` for the three new fields.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass, including `test_answer_key.py` on the v2 fixture.
- [ ] **Step 5:** Commit: `git add hermes_tuner/model.py hermes_tuner/history.py hermes_tuner/scrub.py tests/factory.py tests/test_history.py tests/test_scrub.py`, message `feat(tuner): turn positions and key refs per tool call`.

Mutations this must catch:
- `later_turns` counted across the merged chain (caught by `test_later_turns_per_row_not_chain`);
- off-by-one in `later_turns` (caught by `[2, 2, 1]`);
- `ref` ignoring `query` (caught by `test_turn_positions`).

---

### Task 2: `bytes` candidates and turns-removed estimate

**Files:**
- Modify: `hermes_tuner/detect.py`, `hermes_tuner/policy.py`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `ToolCall.turn`, `ToolCall.later_turns`, `Session.turns` (Task 1).
- Produces:
  - `Candidate.kind` gains `"bytes"`. The key is `f"bytes:{tool}"` and there is one candidate per (job, tool).
  - `detect(sessions, now, pricing, skill_names=None, aliases: Mapping[str, str] | None = None) -> list[Candidate]`. `aliases` maps a composed tool name to the single tool it calls; results are counted under that tool.
  - `carried_usd(chars: int, later_turns: int, pricing: Pricing) -> float` (module-level, used by Tasks 6 and 9). It returns the cost of a result's first send plus its carry:
    - `tokens = chars / policy.CHARS_PER_TOKEN`
    - `first = tokens * input_rate` if `later_turns >= 1`, else 0
    - `carry = tokens * cache_read_rate * max(later_turns - 1, 0)`
    - `cache_read_rate` falls back to `input_rate` when it is None, as in `measure.priced_breakdown`.
  - A `bytes` candidate's `est_monthly_usd` is the sum of `carried_usd` over the window × 30/28. It needs at least `MIN_SEQUENCE_SESSIONS` sessions.
  - A `sequence` candidate's `est_monthly_usd` becomes the turns-removed estimate. For each session, `saved_turns` = distinct `turn` values among the covered calls, minus the number of maximal runs of consecutive covered call indices, floored at 0. Each saved turn is priced at `priced_cost(s) / s.turns` (0 when `s.turns == 0`). Sum over the window, then × 30/28. When the fixture has `turns == 0` (v2), fall back to the old `_money`, so the answer-key tests keep their meaning.
  - `saved_turns(session: Session, gram: tuple[str, ...]) -> int`, module-level: the per-session count above, used by Task 12's acceptance test.
  - `policy.CHARS_PER_TOKEN = 4`, `policy.MIN_DRAFT_MONTHLY_USD = 0.05`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parallel_reads_save_no_turns():
    # 12 sessions, each: 8 get_gmail_message_content calls all with turn=5, s.turns=10
    cands = detect(sessions, now, NOUS_V4_PRO)
    seq = [c for c in cands if c.kind == "sequence"]
    assert all(c.est_monthly_usd == 0 for c in seq) or not seq

def test_serial_reads_save_turns():
    # same, but the 8 calls on turns 2..9: saved_turns = 8 - 1 = 7 per session
    seq = next(c for c in detect(sessions, now, NOUS_V4_PRO) if c.kind == "sequence")
    expect = sum(7 * priced_cost(s, NOUS_V4_PRO) / s.turns for s in sessions) * 30 / 28
    assert seq.est_monthly_usd == pytest.approx(expect)

def test_carried_usd():
    p = NOUS_V4_PRO  # input 0.94, cache read 0.08 per M
    assert carried_usd(4000, 0, p) == 0
    assert carried_usd(4000, 1, p) == pytest.approx(1000 * 0.94 / 1e6)
    assert carried_usd(4000, 3, p) == pytest.approx(1000 * (0.94 + 2 * 0.08) / 1e6)

def test_bytes_ranks_biggest_carried_tool_first(): ...  # search (big, early) outranks get_events (big, last turn)

def test_aliases_count_composed_results_under_called_tool(): ...  # tuned_x -> get_gmail: one bytes candidate, chars summed
```

- [ ] **Step 2:** Run `uv run pytest -q tests/test_detect.py`. Expected: FAIL.
- [ ] **Step 3:** Implement `carried_usd`, `_bytes(...)` and the new `sequence` estimate in `detect.py`. Add the constants to `policy.py`.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: everything in `test_detect.py` and `test_answer_key.py` passes; the answer-key tests go through the v2 fallback.

  **Expected ripple:** `make_db` (Task 1) now gives every session `turns >= 1`, with all calls in one turn by default. Under the new rule those sequences save 0 turns and stop being candidates. Some tests in `tests/test_cycle.py` (and any others) draft from a `sequence` candidate and will fail for that reason. Fix them by giving their sessions a serial turn layout (`make_db` 4-tuples, one call per turn), not by weakening the rule. List each test you changed in the commit message.
- [ ] **Step 5:** Commit `hermes_tuner/detect.py hermes_tuner/policy.py tests/test_detect.py`, message `feat(tuner): rank by carried bytes; sequences priced by turns removed`.

Mutations:
- `first` charged when `later_turns == 0` (caught by `carried_usd(4000, 0) == 0`);
- carry using `later_turns` instead of `later_turns - 1` (caught by the third assert);
- `saved_turns` not floored or not counting runs (caught by the parallel test).

---

### Task 3: Replay view of history

**Files:**
- Modify: `hermes_tuner/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ReplayCall:
    pos: int            # row order within the run
    call_id: str
    name: str
    args: dict
    text: str           # result with Hermes's untrusted wrapper removed (_unwrap)
    turn: int
    later_turns: int

@dataclass(frozen=True)
class ReplayRun:
    session_id: str
    started_at: float
    calls: tuple[ReplayCall, ...]
    said: tuple[tuple[int, str], ...]   # (pos, text): assistant content and each issued call's args as JSON

History.replay_runs(job_id: str, since: float, until: float | None = None) -> list[ReplayRun]
```

- Runs are merged compression chains, like `sessions()`. The dedup rule for copied tail rows is the same.

- [ ] **Step 1: Write the failing tests**
  - `test_replay_runs_texts_and_said`: the assistant row content `"Created Dentist 14:00"` appears in `said` at a `pos` greater than the earlier tool row's `pos`. A wrapped result loses its `<untrusted_tool_result source="…">` header and tail. Rows of other jobs are excluded.
  - `test_replay_runs_window`: `since` and `until` bound `started_at`.
- [ ] **Step 2:** Run `uv run pytest -q tests/test_history.py -k replay`. Expected: FAIL.
- [ ] **Step 3:** Implement with one query per chunk of session ids that selects `role, content, tool_calls, tool_call_id, tool_name`. Reuse `_issued_calls`, `_unwrap` and the chain helpers. Nothing is logged.
- [ ] **Step 4:** Run `uv run pytest -q tests/test_history.py`. Expected: PASS.
- [ ] **Step 5:** Commit `hermes_tuner/history.py tests/test_history.py`, message `feat(tuner): replay view of recorded runs`.

---

### Task 4: Transform library

**Files:**
- Create: `hermes_tuner/reducers.py`
- Test: `tests/test_reducers.py`

**Interfaces:**
- Produces:
  - `OPS: dict[str, Callable[[str, dict], str]]` for text ops: `html_to_text`, `shorten_urls`, `drop_quoted`, `drop_footer`, `collapse_ws`, `cap`.
  - `drop_fields` is a JSON-level op handled in `apply_reducer`.
  - `validate_spec(spec: object) -> list[str]`: empty means valid. A spec is `{"tool": str, "job": str, "steps": [{"op": str, ...params}]}`. Allowed params:
    - `shorten_urls`: `keep_hosts: list[str]`
    - `cap`: `chars: int`, which must be in `policy.REDUCER_CAP_LADDER`
    - `drop_fields`: `fields: list[str]`
    - every other op: none.
  - `apply_reducer(spec: dict, result: str) -> str`:
    - If `result` parses as JSON, walk it: apply text ops to every string value, apply `drop_fields` to dict keys at any depth, and serialise with `json.dumps(..., ensure_ascii=False)`.
    - Otherwise apply the text ops to the whole string.
    - Never raises. On any internal error it returns `result` unchanged.
- Op rules that the tests pin:
  - `shorten_urls` replaces `https?://host/...` with `host`. A URL whose host matches `keep_hosts` or `policy.MEETING_HOSTS` (suffix match on a dot boundary) is kept whole.
  - `drop_quoted` removes lines starting with `>`, and everything from a line matching `^On .{0,200} wrote:$` to the end.
  - `drop_footer` cuts from the first line in the last 40% of the text that contains one of these markers (case-insensitive) to the end: `unsubscribe`, `view in browser`, `manage preferences`, `you are receiving this`, `privacy policy`.
  - `cap` keeps the first N chars and appends `" [cut]"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_shorten_urls_keeps_meeting_hosts():
    t = "join https://us02web.zoom.us/j/123?pwd=x or see https://tracker.example.com/a/b?c=d"
    out = OPS["shorten_urls"](t, {"keep_hosts": []})
    assert "https://us02web.zoom.us/j/123?pwd=x" in out
    assert "tracker.example.com" in out and "/a/b?c=d" not in out

def test_drop_footer_only_in_tail():
    body = "Unsubscribe from nothing, this is the first line.\n" + "body\n" * 50 + "To unsubscribe click here\nlegal"
    out = OPS["drop_footer"](body, {})
    assert out.startswith("Unsubscribe from nothing") and "legal" not in out

def test_apply_reducer_json_leaves():
    r = json.dumps({"content": "<p>Hi <b>there</b></p>", "id": "m1", "n": 3, "headers": {"x": "y"}})
    spec = {"tool": T, "job": J, "steps": [{"op": "html_to_text"}, {"op": "drop_fields", "fields": ["headers"]}]}
    out = json.loads(apply_reducer(spec, r))
    assert out == {"content": "Hi there", "id": "m1", "n": 3}

def test_apply_reducer_odd_inputs():   # Review Focus 1
    spec = {"tool": T, "job": J, "steps": [{"op": "collapse_ws"}]}
    assert apply_reducer(spec, '{"a": "x   y"') == '{"a": "x y"'    # truncated JSON: text path
    assert apply_reducer(spec, "null") == "null"
    assert apply_reducer(spec, "plain   text") == "plain text"

def test_validate_spec_rejects():
    assert validate_spec({"tool": T, "job": J, "steps": [{"op": "eval"}]})
    assert validate_spec({"tool": T, "job": J, "steps": [{"op": "cap", "chars": 123}]})
    assert validate_spec({"tool": "web_extract", "job": J, "steps": []})
```

  Add one test per op (`html_to_text`, `drop_quoted`, `collapse_ws`, `cap`) with exact input and output strings.
- [ ] **Step 2:** Run `uv run pytest -q tests/test_reducers.py`. Expected: FAIL (no module).
- [ ] **Step 3:** Add `REDUCER_JOBS`, `REDUCER_TOOLS`, `MEETING_HOSTS` and `REDUCER_CAP_LADDER` to `policy.py`, with the values in Global Constraints. Then implement `hermes_tuner/reducers.py`. `html_to_text` uses `html.parser.HTMLParser`: text of data nodes, a newline for block tags, script and style dropped. `validate_spec` also checks `tool in policy.REDUCER_TOOLS` and `job in policy.REDUCER_JOBS`.
- [ ] **Step 4:** Run `uv run pytest -q tests/test_reducers.py`. Expected: PASS.
- [ ] **Step 5:** Commit `hermes_tuner/reducers.py hermes_tuner/policy.py tests/test_reducers.py`, message `feat(tuner): deterministic result-reducer transforms`.

---

### Task 5: Used strings

**Files:**
- Create: `hermes_tuner/replay.py` (this task adds the used-string functions)
- Test: `tests/test_replay.py`

**Interfaces:**
- Produces:
  - `norm(text: str) -> str`: lowercase, whitespace runs to one space.
  - `used_strings(result: str, later: Sequence[str], exclude: Sequence[str]) -> frozenset[str]`. These are the normalised maximal common substrings of length ≥ `USED_MIN_CHARS` between `norm(result)` and each `norm(later[i])`, found with `difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()`. A block is dropped when it appears in any `norm(exclude[j])`.
    - **Bound:** only the first 50,000 normalised chars of `result` and of each `later` text are compared. For a newsletter-sized result, used strings past that point are not found.
  - `survives(s: str, reduced: str) -> bool`: `s in norm(reduced)`.
  - `boilerplate(per_result: Sequence[frozenset[str]]) -> frozenset[str]`: strings present in more than `BOILERPLATE_SHARE` of the results.

- [ ] **Step 1: Write the failing tests**

```python
def test_used_strings_finds_verbatim_reuse():
    res = "Subject: Dentist appointment on 2026-10-30 at 14:00, 12 High Street"
    got = used_strings(res, ['{"summary": "Dentist appointment", "location": "12 High Street"}'], [])
    assert "dentist appointment" in got and "12 high street" in got

def test_used_strings_excludes_prompt_and_short():
    got = used_strings("calendar sync event: abc", ['calendar sync event: abc'], ["Calendar sync event"])
    assert "calendar sync event" not in got and all(len(s) >= 8 for s in got)

def test_survives_after_reduction():
    assert survives("12 high street", "Visit   12 High Street today")

def test_used_strings_bounded_time():   # Review Focus 3
    big = ("lorem ipsum dolor " * 12000)
    t0 = time.monotonic(); used_strings(big, [big[:5000]] * 20, []); assert time.monotonic() - t0 < 5
```

- [ ] **Step 2:** Run `uv run pytest -q tests/test_replay.py`. Expected: FAIL.
- [ ] **Step 3:** Implement in `hermes_tuner/replay.py`, with the constants in `policy.py`.
- [ ] **Step 4:** Run `uv run pytest -q tests/test_replay.py`. Expected: PASS.
- [ ] **Step 5:** Commit `hermes_tuner/replay.py hermes_tuner/policy.py tests/test_replay.py`, message `feat(tuner): used-string extraction for replay`.

Mutations:
- dropping `autojunk=False` (repeated text gets junked and reuse is missed by the first test on longer inputs; add a 300-char repeated-prefix case);
- min length 6 instead of 8 (caught by the second test).

---

### Task 6: Reducer search on replay

**Files:**
- Modify: `hermes_tuner/replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `ReplayRun` (Task 3); `apply_reducer` and `validate_spec` (Task 4); `used_strings`, `survives`, `boilerplate` (Task 5); `carried_usd` (Task 2).
- Produces:

```python
@dataclass(frozen=True)
class ReducerResult:
    spec: dict | None
    reason: str          # "ok" | "no_runs" | "too_few_sessions" | "cut_below_min" | "lost_used_string"
    sessions: int
    results: int
    bytes_before: int
    bytes_after: int
    used_total: int
    used_kept: int
    est_saving_per_run: float   # carried_usd(before) - carried_usd(after), summed, / sessions

search_reducer(runs: list[ReplayRun], tool: str, job: str, exclude: Sequence[str],
               pricing: Pricing) -> ReducerResult
```

- The greedy search, as spec §3 describes:
  - **Candidate steps:** `html_to_text`, `shorten_urls` with `keep_hosts` = hosts found in used strings, `drop_quoted`, `drop_footer`, `collapse_ws`, `drop_fields` (fields that never hold a used string, top-level keys only), and `cap` at each ladder value.
  - **Each round:** add the step that cuts the most bytes while 100% of used strings survive. Stop when the best step cuts less than `REDUCER_STEP_MIN` of `bytes_before` more.
  - **Result:** `spec` is set only when the total cut is at least `REDUCER_MIN_CUT` and `sessions >= REPLAY_MIN_SESSIONS`.
  - **Determinism:** ties go to the step listed first above.

- [ ] **Step 1: Write the failing tests** on a synthetic `ReplayRun` list built in-test. Use made-up emails with HTML, tracking URLs, a zoom link and a footer, where the model's later `manage_event` args quote the subject and the zoom link.

```python
def test_search_cuts_and_keeps_used():
    r = search_reducer(runs, GMAIL_GET, CAL, exclude=[], pricing=NOUS_V4_PRO)
    assert r.reason == "ok" and r.used_kept == r.used_total > 0
    assert r.bytes_after <= 0.85 * r.bytes_before
    ops = [s["op"] for s in r.spec["steps"]]
    assert "html_to_text" in ops and "shorten_urls" in ops

def test_search_refuses_cap_that_cuts_used_text(): ...  # used string at char 9000 -> no cap below 16000 chosen
def test_search_no_runs():   # Review Focus 4
    assert search_reducer([], GMAIL_GET, CAL, [], NOUS_V4_PRO).reason == "no_runs"
def test_search_too_few_sessions(): ...  # 2 runs -> spec None, reason "too_few_sessions"
def test_search_is_deterministic(): ...  # same input twice -> identical spec
```

- [ ] **Step 2:** Run `uv run pytest -q tests/test_replay.py -k search`. Expected: FAIL.
- [ ] **Step 3:** Implement `search_reducer`. Used strings per result are computed once against `said` entries with `pos > call.pos`. Boilerplate is removed across the tool's results.
- [ ] **Step 4:** Run `uv run pytest -q tests/test_replay.py`. Expected: PASS.
- [ ] **Step 5:** Commit `hermes_tuner/replay.py tests/test_replay.py`, message `feat(tuner): greedy reducer search on replay`.

---

### Task 7: `reducer` fix kind: ledger, limits, plugin hook

**Files:**
- Modify: `hermes_tuner/model.py` (`Fix.kind` adds `"reducer"`)
- Modify: `hermes_tuner/budget.py` (`JOB_KINDS` adds `"reducer"`)
- Modify: `hermes_tuner/ledger.py`: `needs_restart` returns False for reducers. `apply` writes `reducers/<fix id>.json`. `rollback` deletes it. Add `write_shadow_reducer(root: Path, payload: dict, fix_id: str) -> None`, `remove_reducer(root: Path, fix_id: str) -> None` and `go_live_reducer(root: Path, fix_id: str) -> None` (rewrites without the shadow flags).
- Modify: `hermes_tuner/limits.py` (`_check_reducer(fix) = validate_spec(fix.payload)` plus `_check_job_target`)
- Modify: `plugin/hermes-tuner/__init__.py`: add `reduce_result(tool_name, args, result, session_id="", tool_call_id="", **_) -> str | None`, registered on `transform_tool_result`, and update the docstring's write list.
- Test: `tests/test_ledger.py`, `tests/test_limits.py`, `tests/test_plugin.py`

**Interfaces:**
- Reducer file: `{"spec": <validated spec>, "fix_id": str, "shadow_only": bool, "shadow_fix": str | None}`.
- Hook behaviour:
  - It loads `$HERMES_HOME/tuner/reducers/*.json` and caches them keyed by `(name, mtime_ns)`.
  - It skips any file that fails `json.loads` or `validate_spec`.
  - It finds the spec whose `tool == tool_name`. Scope:
    - in a shadow session (`is_shadow_session`), only the file whose `shadow_fix` equals that shadow's fix id;
    - otherwise only files with `shadow_only == False` whose `spec["job"]` equals the id matched by `_CRON_SESSION` on `session_id`.
  - It applies `apply_reducer`, then appends one line to `$HERMES_HOME/tuner/reducer_events/<fix_id>.jsonl`: `{"session_id", "tool_call_id", "before": len(result), "after": len(out)}`, or `{"session_id", "tool_call_id", "error": "<ExceptionType>"}` on failure.
  - It returns `out`, or `None` on any exception or non-match.
  - Event files use `atomic`-style append: open with `O_APPEND`, mode 0600.

- [ ] **Step 1: Write the failing tests**
  - `test_apply_and_rollback_reducer` (ledger): `apply` creates `reducers/<id>.json` with `shadow_only` False. `rollback` removes it, and the record state becomes `rolled_back`.
  - `test_reducer_limits`: an invalid op, a job outside `REDUCER_JOBS`, and a tool outside `REDUCER_TOOLS` are all rejected with reasons. A valid spec gives `[]`.
  - `test_hook_scoping` (Review Focus 5). With one live reducer for `cc68e24f21cd`:
    - `session_id="cron_cc68e24f21cd_20261029_060000"` → transformed, and one event line is written;
    - `"20261029_101500_abc123"` (chat) → `None`;
    - `"cron_3bc4e11d29b1_…"` → `None`;
    - a shadow session for another fix → `None`;
    - `tool_name="web_extract"` → `None`.
  - `test_hook_skips_bad_reducer_file` (Review Focus 2): a truncated JSON file and a file with `{"op": "eval"}` sit next to a good one. The good one still applies, and nothing raises.
  - `test_hook_fail_open`: force `apply_reducer` to raise by passing an injected spec loader in the test. The hook returns `None` and an `error` event is written.
- [ ] **Step 2:** Run `uv run pytest -q tests/test_ledger.py tests/test_limits.py tests/test_plugin.py`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement. The plugin imports `hermes_tuner.reducers` the way it already imports `policy`.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit the listed files, message `feat(tuner): reducer fix kind and transform_tool_result hook`.

---

### Task 8: Cycle routes calendar-sync `bytes` candidates to the reducer search

**Files:**
- Modify: `hermes_tuner/cycle_draft.py` (`JOB_CANDIDATES` adds `"bytes"`; `_select` drops candidates under `MIN_DRAFT_MONTHLY_USD`; new `_reduce_one(cand) -> tuple[float, bool]`)
- Modify: `hermes_tuner/cycle.py`:
  - `_shadow_tool` / `_remove_shadow_tool` also handle reducers (remove the reducer file);
  - `_go_live` calls `go_live_reducer`;
  - `_tool_check` for a reducer fails with `"shadow never reduced a result"` when no event line for this fix id has the shadow's session id.
- Modify: `hermes_tuner/ledger.py` (`FixRecord.est_saving_per_run: float | None = None`)
- Test: `tests/test_cycle.py`

**Interfaces:**
- Consumes:
  - `History.replay_runs` (Task 3);
  - `search_reducer` (Task 6);
  - `write_shadow_reducer`, `go_live_reducer`, `remove_reducer` (Task 7);
  - `detect(..., aliases=...)` (Task 2). Build `aliases` from `tools/*/tool.json` whose `calls` has length 1.
- Routing in `draft_fixes`: if `cand.kind == "bytes"` and `cand.job_id in REDUCER_JOBS` and the tool is in `REDUCER_TOOLS`, call `_reduce_one` instead of `_draft_one`. `_reduce_one`:
  - gets the job (as `_draft_one` does);
  - builds `exclude = [job["prompt"]]`;
  - calls `search_reducer(self.history.replay_runs(job, now - 28*DAY), tool, job, exclude, pricing)`;
  - on `spec is None`, records a rejected placeholder with spend 0 and reason `f"reducer search: {result.reason}"`;
  - otherwise makes `Fix(id=f"{day}-r{sha256(key+spec)[:8]}", kind="reducer", target=job, payload=spec, candidate_key=cand.key)`, sets `est_saving_per_run`, and goes through `_act`.
  - Spend is 0. The shadow's job cost is counted as today.

- [ ] **Step 1: Write the failing tests** (`FakeBridge`, `make_db` and synthetic text, as the existing cycle tests do):
  - `test_cycle_shadows_reducer_for_calendar_sync`: the record state is `shadow_pending`, kind is `reducer`, `reducers/<id>.json` has `shadow_only: true`, and no `oneshot` call was made on the bridge.
  - `test_reducer_goes_live_after_good_shadow`: the shadow has an event line, and the calendar comparison is ok → `trial`, and the file has `shadow_only: false`.
  - `test_reducer_shadow_without_events_fails`: → `shadow_failed` with reason `"shadow never reduced a result"`, and the file is removed.
  - `test_morning_briefing_bytes_goes_to_model`: a `bytes` candidate for `3bc4e11d29b1` calls `bridge.oneshot` and never searches.
  - `test_select_skips_cheap_candidates`: a candidate at $0.04/month is not drafted.
- [ ] **Step 2:** Run `uv run pytest -q tests/test_cycle.py`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `hermes_tuner/cycle_draft.py hermes_tuner/cycle.py hermes_tuner/ledger.py tests/test_cycle.py`, message `feat(tuner): cycle drafts reducers for calendar sync from replay`.

---

### Task 9: Composed tools: `replay_inputs`, limits in the prompt, replay gate

**Files:**
- Modify: `hermes_tuner/draft.py`:
  - `INSTRUCTIONS` gains the limits and `replay_inputs`;
  - `_clean_composed` requires `replay_inputs`;
  - `build_request` takes `turn_layout: list[int]`.
- Modify: `hermes_tuner/limits.py`: a composed tool without a valid `replay_inputs` is rejected. Valid means each entry has `{"from_call": <one of payload.calls>, "arg": <identifier>, "collect": "list" | "first"}`.
- Modify: `hermes_tuner/replay.py`: `replay_composed(payload: dict, runs: list[ReplayRun], run_program: Callable[..., str]) -> ComposedReplay`.
- Modify: `hermes_tuner/cycle_draft.py`: in `_act`, a composed tool that passes `check_fix` is replayed before `_shadow`. `DraftSteps.run_program` is injected; the default wraps `relay.run_program` loaded via `importlib.util.spec_from_file_location` as `tests/test_relay.py` does.
- Test: `tests/test_draft.py`, `tests/test_limits.py`, `tests/test_replay.py`, `tests/test_cycle.py`

**Interfaces:**
- `ComposedReplay(sessions: int, limit_hits: int, errors: int, in_bytes: int, out_bytes: int, reason: str)`.
- Per run:
  - Build one input from all of that run's calls to tools in `payload["calls"]`, using `replay_inputs`. This is the worst case: the model might have batched smaller.
  - Call `run_program(program, args, call=<stub>, dry_run=True, ...)`. The stub returns the recorded `text` for the same name and `json.dumps(args, sort_keys=True)`. A miss raises, and the run is skipped.
  - A limit hit is an error output whose message starts with `output exceeds`, `tool call limit` or `program timed out`.
- **Gate:**
  - reject on `limit_hits > 0` with reason `replay: limit hit in {n} of {m} sessions`;
  - reject on `errors > 0`;
  - reject on `sessions < REPLAY_MIN_SESSIONS`;
  - reject when the estimate is at most `MIN_DRAFT_MONTHLY_USD` per month. The estimate is `carried_usd` of `(in_bytes - out_bytes)` plus the candidate's turns-removed estimate.
- Prompt text added to `INSTRUCTIONS`, verbatim:
  - `"The program's JSON output must stay under {MAX_OUTPUT_BYTES} bytes, at most {MAX_CALLS} tool calls and {RUN_TIMEOUT_S} seconds; return only what the job needs, not whole tool results."`
  - `"turn_layout lists how many of the observed calls the agent already sends per turn; merging calls that already share a turn saves nothing."`

- [ ] **Step 1: Write the failing tests**
  - `test_replay_composed_hits_output_cap` (acceptance check 3 on synthetic data). A batch program, with `replay_inputs` `message_ids ← message_id`, runs over 3 runs of 8 recorded 7,000-char emails. The injected `run_program` is the real `relay.run_program` with the local stub runner from `test_relay.py`. Expect `limit_hits >= 1`, and the cycle records the draft as `rejected` with `"replay: limit hit"` in its reason.
  - `test_clean_composed_requires_replay_inputs`.
  - `test_request_carries_limits_and_layout`: the request JSON has `turn_layout == [4, 1, 3]` for the fixture session, and `INSTRUCTIONS` contains `str(policy.MAX_OUTPUT_BYTES)`.
- [ ] **Step 2:** Run the new tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit the listed files, message `feat(tuner): replay composed tools before shadow; tell the drafter the limits`.

---

### Task 10: Back-off and drafting history

**Files:**
- Create: `hermes_tuner/backoff.py`
- Modify: `hermes_tuner/ledger.py` (`FixRecord.input_hash: str | None = None`)
- Modify: `hermes_tuner/cycle_draft.py`: set `input_hash` on every record; skip or park in `_select`; pass `history` to `draft`.
- Modify: `hermes_tuner/draft.py` (`build_request(..., history: list[dict])`)
- Test: `tests/test_backoff.py`, `tests/test_cycle.py`

**Interfaces:**
- `reason_class(rec: FixRecord) -> str`. Classes come from fixed substrings in the reason:
  - `"copy"`: copied from untrusted tool results
  - `"replay_limit"`
  - `"replay"`: other replay rejections
  - `"timeout"`: drafting call failed or timed out
  - `"unparseable"`
  - `"shadow"`: state `shadow_failed`
  - `"limits"`: any other rejection
  - `"trial"`: rolled back
  - `""` for kept, active or proposed records
- `copy_fields(rec: FixRecord) -> list[str]`: the `payload.<field>` names in a copy rejection.
- `input_hash(job: dict | None, skill_text: str | None) -> str`: sha256 hex of `json.dumps([job.get("prompt") if job else None, skill_text])`.
- `backoff(records: list[FixRecord], candidate_key: str, current_hash: str, now: float) -> Literal["ok", "skip", "parked"]`:
  - look at the candidate's records newest first, ignoring class `""`;
  - `"parked"` when the 3 newest share `(kind, class)` and the newest's `input_hash == current_hash`;
  - `"skip"` when the 2 newest share `(kind, class)` and the newest is less than 7 days old;
  - otherwise `"ok"`.
  - Placeholders use their tag as `kind`, so they count.
- Drafting history: up to 3 dicts `{"kind", "state", "reason_class", "fields"}`, newest first.

- [ ] **Step 1: Write the failing tests**

```python
def test_three_copy_rejections_park():      # acceptance check 5
    recs = [rej("composed_tool", "payload.program: contains text copied from untrusted tool results", t, h)
            for t in (DAY * 1, DAY * 2, DAY * 3)]
    assert backoff(recs, KEY, h, DAY * 4) == "parked"
    assert backoff(recs, KEY, "other-hash", DAY * 4) == "skip"      # inputs changed: not parked, newest 1 day old
    assert backoff(recs, KEY, "other-hash", DAY * 11) == "ok"       # and 8 days later it may draft again

def test_two_same_skip_then_ok_after_7_days(): ...
def test_different_kinds_do_not_back_off(): ...   # composed_tool then job_prompt -> "ok"
def test_cycle_sends_no_draft_when_parked(): ...  # FakeBridge.oneshot call count == 0 on the 4th cycle
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit the listed files, message `feat(tuner): back off repeated identical rejections; show the drafter past outcomes`.

---

### Task 11: Early rollback, `trial_runs`, estimate vs measured

**Files:**
- Modify: `hermes_tuner/trial.py`: add `early_failures(...)`.
- Modify: `hermes_tuner/cycle.py` (`judge_trials`)
- Modify: `hermes_tuner/ledger.py` (`FixRecord.measured_saving_per_run: float | None = None`)
- Modify: `hermes_tuner/budget.py` (`weekly_line`)
- Test: `tests/test_trial.py`, `tests/test_cycle.py`, `tests/test_budget.py` (create if absent, or extend the existing weekly-line tests)

**Interfaces:**
- `early_failures(kind: str, own_results: list[str], reduce_errors: int, trial_rereads: list[int], baseline_rereads: list[int]) -> tuple[int, int]` returns `(failures, calls)`:
  - **composed tool:** `failures` = results where `call_failed` is true; `calls` = `len(own_results)`.
  - **reducer:** `failures = reduce_errors` plus the number of trial runs whose re-reads exceed the baseline mean; `calls` = reduced results.
  - **re-reads in a run:** sum over (tool, ref) pairs of `count - 1`, for calls to the reducer's tool with a non-None ref.
- `judge_trials`:
  - before the verdict, roll back when `failures >= 2` or `calls > 0 and failures / calls >= 0.10`, with reason `f"trial: {failures} failures in {calls} calls (early)"`;
  - always set `rec.trial_runs` to the run count used;
  - on keep or rollback, set `measured_saving_per_run = baseline median − trial median` (priced, as `trial_verdict` does).
- `weekly_line` appends `f"; estimates: {_money(est)} vs measured {_money(meas)} per run over {n} fixes"` when any fix decided in the last 28 days has both fields.

- [ ] **Step 1: Write the failing tests**
  - `test_batch_tool_rolls_back_early`: 41 own results, 14 of them `{"error": "output exceeds 50000 bytes"}` → `(14, 41)`, and the cycle rolls back with `"(early)"` in the reason.
  - `test_reducer_rereads_vs_baseline`: baseline re-reads `[0, 1, 0]`, trial `[0, 3]` → `failures == 1`.
  - `test_trial_runs_written`: after `judge_trials`, `rec.trial_runs == 4` for 4 post-live runs.
  - `test_weekly_line_estimates`: one kept fix with est 0.02 and measured 0.01 → the line ends with `estimates: ~$0.02 vs measured ~$0.01 per run over 1 fixes`. Match the `_money` format the existing tests use.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Reducer event lines are read from `tuner/reducer_events/<fix id>.jsonl` for the trial runs' session ids.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit the listed files, message `feat(tuner): early rollback on own failures; record trial runs and estimate accuracy`.

---

### Task 12: `replay-check` CLI, new fixture, droplet acceptance

**Files:**
- Modify: `hermes_tuner/__main__.py`. Add `replay-check --job <id> [--tool <name>] [--composed <tool dir>] [--replay-input <param>=<tool>.<arg>:<list|first>]`. `--replay-input` may repeat. It supplies `replay_inputs` for a tool dir that predates the field. It prints one JSON object per tool, containing the `ReducerResult` or `ComposedReplay` fields except `spec`. When there is a spec, it prints `"ops"` (op names only).
- Create: `tests/fixtures/history-2026-10-28.json.gz` (scrub v3, produced on the droplet).
- Modify: `tests/test_answer_key.py` (add acceptance checks 1 and 2 on the new fixture, with `needs_fixture` skip)
- Test: `tests/test_cli.py` (create if absent)

**Steps:**
- [ ] **Step 1: Write the failing tests.**
  - `test_replay_check_prints_counts_only`: run the CLI on a synthetic db and assert no line of output contains any 20-char substring of the synthetic email texts. This is the privacy guard.
  - `test_fixture_bytes_ranking` (acceptance check 1): the top `bytes` candidate is `search_gmail_messages` for `cc68e24f21cd` and `get_gmail_message_content` for `3bc4e11d29b1`.
  - `test_fixture_parallel_sequence_zero` (acceptance check 2): the 10-24 calendar run's `get_gmail_message_content` window has `saved_turns == 0`.
- [ ] **Step 2:** Implement the CLI.
- [ ] **Step 3: Produce the fixture on the droplet.** Deploy the branch to a scratch checkout there, not `~/hermes-harness`, which `harness-pull` resets.
  - Run `python -m hermes_tuner scrub --since 2026-09-15 --until 2026-10-29 --out /tmp/h.json.gz`, then `sha256sum /tmp/h.json.gz; ls -l /tmp/h.json.gz`.
  - Copy it with `ssh hermes-vm 'cat /tmp/h.json.gz' > tests/fixtures/history-2026-10-28.json.gz` and check that the local `sha256sum` matches.
  - Run the existing `test_fixture_has_no_long_strings` against it.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all pass, including the fixture tests.
- [ ] **Step 5: Droplet acceptance (checks 3 and 4, plus builder items).** Run each of these in the scratch checkout and save the raw output for the PR body:
  - `python -m hermes_tuner replay-check --job cc68e24f21cd`. Expect: `get_gmail_message_content` `reason: ok`, `bytes_after <= 0.75 * bytes_before`, `used_kept == used_total`. Expect a pass or fail line for `search_gmail_messages`.
  - `python -m hermes_tuner replay-check --job cc68e24f21cd --composed ~/.hermes/tuner/tools/tuned_batch_fetch_gmail_contents`. Expect `limit_hits >= 1`. The live tool predates `replay_inputs`, so pass `--replay-input message_ids=get_gmail_message_content.message_id:list` on the CLI for this check only.
  - **Builder items from spec "Left to the builder":** whether the hook sees the underlying tool name or `tool_call`, and whether `result` is wrapped. Settle both with a throwaway plugin hook in a scratch `HERMES_HOME` profile that logs `tool_name` and the result's first 40 chars for one `get_events` call, then delete it. Also check whether `ctx.dispatch_tool` results pass through `transform_tool_result`, and whether the plugin re-reads reducers without a restart. Write the answers into the PR body. If the hook sees wrapped text, add an `_unwrap` step to `reduce_result` and its test before merging.
  - **The six copy-check rejections:** run a one-off script that prints, for each record, the matched span's length and which untrusted tool it came from, never the span. Record whether they are false positives.
- [ ] **Step 6:** Commit `hermes_tuner/__main__.py tests/fixtures/history-2026-10-28.json.gz tests/test_answer_key.py tests/test_cli.py`, message `feat(tuner): replay-check CLI and 2026-10-28 fixture`.

Live acceptance check 7 (rollback seen, a reducer in trial within 14 days, verdict with estimate) happens after merge and deploy. It is tracked in the PR, not in this plan.
