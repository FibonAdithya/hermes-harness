# Tuner self-repair, step 1 (undo loop) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The tuner rolls back a job fix at its next cycle when the fix's runs show harm, tells the owner in one line, teaches the drafting model the reason as a fixed class, keeps the owner's prompt text unchanged (P2), and gathers the input snapshot later repairs need.

**Architecture:** A new pure module `harm.py` classifies tool results and holds a registry of harm signals; the shadow and trial steps in `cycle.py` loop over that registry. A new pure module `prompt_block.py` splices a tuner-owned block into a job prompt; drafting writes only the block and `limits.check_fix` verifies the owner's text is untouched. All of it runs on the droplet; nothing on the box, broker or plugin changes.

**Tech Stack:** Python 3.11, pytest, uv. A fresh worktree has an empty venv: run `cd tuner && uv sync --extra dev` once first, or `uv run pytest` fails with `Failed to spawn: pytest` (MEASURED 2026-11-04). Then `uv run pytest -q` (923 passed at 7f55593, MEASURED 2026-11-04).

**Spec:** `docs/ai/specs/2026-11-04-tuner-self-repair-design.md` (§2 and build-order step 1). Steps 2–5 of the spec get their own plans after this ships.

## Global Constraints

- All code under `tuner/`. Do not touch `broker/`, `box/`, `executor/`, `tuner/plugin/`, `.github/`, or `tuner/hermes_tuner/fingerprint.py`.
- Harm signals, in this order: `SIGNALS = ("unknown_tool", "server_tripped", "self_inflicted_errors", "run_failed")`.
- Harm applies only to `JOB_KINDS` (`budget.JOB_KINDS`: job_prompt, composed_tool, reducer).
- Kill switch: `policy.HARM_CHECKS = True`; when False, no harm check runs anywhere.
- Thresholds (measured basis in Task 1): `SELF_ERR_MIN = 3`, `SELF_ERR_FACTOR = 2.0`, `SELF_ERR_MARGIN = 0.05`, outage window `OUTAGE_WINDOW_S = 6 * 3600`.
- Reason strings: shadow `f"harm: {signal}"`; trial `f"harm: {signal} (early)"`. Reason classes: `f"harm_{signal}"`.
- Block markers, exact: `START = "--- tuner notes (written by hermes-tuner; replaced on each edit) ---"`, `END = "--- end tuner notes ---"`.
- Notice file: `<tuner root>/outbox/rollback-<fix id>.txt`, one line, no trailing text beyond `\n`.
- Error texts matched (read from source on the droplet at Hermes 7bfbfa3e, and from 28 days of state.db, 2026-11-04). Each is the string value of the top-level `"error"` key of the result:
  - unknown tool: `"'<name>' is not a deferrable tool."` (tools/tool_search.py:899,1019), `"Unknown tool: <name>"` (tools/registry.py:615), and the tuner plugin's `"<name> is no longer available"` (tuner/plugin/hermes-tuner/__init__.py:307). **The deferrable message is also returned for registered core tools** (`is_deferrable_tool_name` is False for `_HERMES_CORE_TOOLS` and the bridge tools), so it only means "unknown" when the name is not registered.
  - server cut off: `"MCP server '<s>' is unreachable after"` (tools/mcp_tool.py:4701).
  - self-inflicted argument errors (`tool_error`): `"Error calling tool '<t>': ..."` (from the Google MCP server; not in Hermes source), `"<n> validation error(s) for call[<t>]"` (pydantic, via the MCP server), `"tool_call to '<t>' is missing required argument(s)"` (Hermes tool_call bridge).
  - upstream errors (`upstream_error`, never harm): `"Error calling tool '<t>': API error in ..."` (Google API failures, e.g. `HttpError`). These are outages, not the run's fault.
- **Hermes's guardrail suffix.** On repeated failures Hermes appends `"\n\n[Tool loop warning: <code>; count=<n>; <message>]"` (or `Tool loop hard stop`) after the result JSON (agent/tool_guardrails.py:521). In the 11-04 incident run, 1 of the 3 argument errors and both unreachable results carry it, so whole-text `json.loads` misses them (MEASURED 2026-11-04). `classify` strips exactly one such suffix, anchored at the end, before parsing; any other trailing text still makes the result unclassifiable.
- **Attribution of `unknown_tool`.** From 10-30 to 11-03 every briefing run called the rolled-back `tuned_batch_fetch_gmail_contents` (still listed until the 11-03 gateway restart) and got "is no longer available" (MEASURED 2026-11-04). Those calls were not caused by any fix in trial. So an unknown name counts against a fix only when the fix's own payload text names it (full name, or its last `__` segment, as a whole word), and it is not in `RunEvidence.tools` (registered ∪ live). The 11-04 bad prompt names `tuned_batch_fetch_gmail_contents` (MEASURED: `versions/20261103-23272612/after.json` prompt contains it), so incident A still fires.
- Measured replay of the corrected rules over 28 days of briefing (`3bc4e11d29b1`) and calendar (`cc68e24f21cd`) runs (2026-11-04): with the thresholds below, `self_inflicted_errors` and `server_tripped` fire only on `cron_3bc4e11d29b1_20261104_080057`; briefing baseline `tool_error` rate is about 0.01 per result.
- Tests never monkeypatch default arguments (memory: a patched default hit the live wiki once). Patching a module constant read at call time (`policy.HARM_CHECKS`) is fine.
- Every new test docstring ends with `Catches: <mutation>`, as the repo's tests already do.
- Never commit droplet data (repo is public): fixtures are synthetic.

## Review Focus

1. **Email text that looks like an error.** A gmail result whose body contains `{"error": "Unknown tool: x"}` must not classify as an error, or an attacker could roll back good fixes by email. → Task 1 test `test_classify_ignores_error_text_inside_content`.
2. **A prompt that already has a tuner block** (from an earlier kept fix) must have it replaced, not duplicated. → Task 5 `test_splice_replaces_existing_block`.
3. **Owner prompt whitespace** (trailing newline or spaces, empty prompt) must survive splicing byte for byte. → Task 5 `test_splice_keeps_owner_text_byte_for_byte`.
4. **Bridge failure while reading job status** must not stop the trial judging; the other signals still apply. → Task 3 `test_trial_harm_survives_job_status_failure`.
5. **Dry run** must plan a harm rollback without rolling back or writing a notice. → Task 4 `test_dry_run_harm_rollback_writes_no_notice`.
6. **Hermes's loop-warning suffix** on stored results: the classifier must see through it, and only it. → Task 1 `test_classify_strips_hermes_loop_suffix`.
7. **Attribution.** A stale or hallucinated tool name the fix never mentioned, or a registered core tool, must not roll a fix back; a Google API failure is not the run's fault. → Task 1 `test_unknown_tool_needs_the_fix_to_name_it`, `test_unknown_tool_excuses_registered_core_tools`, `test_upstream_errors_are_not_harm`; Task 3 `test_trial_ignores_a_stale_tool_the_fix_does_not_name`; Task 7 Step 4b on live data.

---

### Task 1: `harm.py` — classify results and the signal registry

**Files:**
- Create: `tuner/hermes_tuner/harm.py`
- Modify: `tuner/hermes_tuner/policy.py` (add `HARM_CHECKS = True` next to the other switches)
- Test: `tuner/tests/test_harm.py`

**Interfaces:**
- Consumes: `history._unwrap(text: str, tools: frozenset[str]) -> str` (strips Hermes's untrusted-result wrapper).
- Produces:
  - `SIGNALS: tuple[str, ...]` (Global Constraints order)
  - `SELF_ERR_MIN, SELF_ERR_FACTOR, SELF_ERR_MARGIN, OUTAGE_WINDOW_S`
  - `@dataclass(frozen=True) class RunEvidence: results: tuple[tuple[str, str], ...]; status: str | None = None; delivery_failed: bool = False; fix_kind: str = ""; tools: frozenset[str] = frozenset(); fix_text: str = ""` — `results` is `History.call_results` order; `status` is the job's `last_status` when it describes this run, else None; `tools` is registered ∪ live tool names (empty means unknown: nothing is excused); `fix_text` is every string in the fix's payload, joined with `"\n"`.
  - `@dataclass(frozen=True) class Baseline: error_rates: tuple[float, ...] = (); outage: bool = False`
  - `classify(name: str, text: str) -> tuple[str, str] | None` → `(cls, subject)` with `cls` in `"unknown_tool" | "server_unreachable" | "tool_error" | "upstream_error"`; `subject` is the unknown tool's name for `unknown_tool`, the server for `server_unreachable`, else `name`. None for anything else.
  - `error_rate(results: Iterable[tuple[str, str]]) -> float` — share of results classified `tool_error` (not `upstream_error`); 0.0 for none.
  - `names_tool(fix_text: str, tool: str) -> bool` — True when `tool`, or its last `__` segment, occurs in `fix_text` bounded by non-word characters.
  - `CHECKS: dict[str, Callable[[RunEvidence, Baseline], bool]]`, keys exactly `SIGNALS`
  - `signals(run: RunEvidence, base: Baseline) -> list[str]` — names of checks that fire, in `SIGNALS` order.

Measured basis for the thresholds (2026-11-04, state.db, 28 days, corrected classifier; see Global Constraints): `self_inflicted_errors` and `server_tripped` fire only on the 11-04 bad run; other briefing runs have at most 1 `tool_error` (the 11-02 run's 2 `get_events` failures are `upstream_error`). The 11-04 run: 2 unknown-tool lookups, 3 argument errors, 2 unreachable, 35 other results.

- [ ] **Step 1: Write the failing tests in `tests/test_harm.py`**

Helpers: `WRAP(name, body)` builds Hermes's wrapper exactly as `history._WRAP_HEAD`/`_WRAP_NOTE`/`_WRAP_TAIL` define it around `body`; `LOOP = "\n\n[Tool loop warning: same_tool_failure_warning; count=3; mcp__google__get_gmail_message_content has failed 3 times this turn.]"` (Hermes's guardrail suffix, appended after the wrapper's tail as stored); `UNKNOWN = '{"error": "\'mcp__google__tuned_x\' is not a deferrable tool. If you see it in the tools list already, call it directly; otherwise check the spelling against tool_search."}'`; `ARG_ERR = WRAP(GET, '{"error": "Error calling tool \'get_gmail_message_content\': expected_sha256 and page_size require page_offset."}')`; `DOWN = WRAP(GET, '{"error": "MCP server \'google\' is unreachable after 3 consecutive failures"}')`; `API_ERR = WRAP(GET, '{"error": "Error calling tool \'get_events\': API error in get_events: <HttpError 503>"}')`; `OK = WRAP(GET, "Subject: hi")`; `FIX = "Fetch mail with tuned_x."`.

Before writing `WRAP`, check from `history._unwrap` where the guardrail suffix sits relative to the wrapper tail in stored results (the incident run's unwrapped text ends with the suffix, MEASURED); build `LOOP` cases the same way.

```python
def test_classify_unknown_tool():
    """Catches: a missing unknown-tool pattern (tool_describe, registry or plugin form),
    or the subject not being the unknown name."""
    assert classify("tool_describe", UNKNOWN) == ("unknown_tool", "mcp__google__tuned_x")
    assert classify("tuned_x", '{"error": "Unknown tool: tuned_x"}') == ("unknown_tool", "tuned_x")
    assert classify("tuned_x", '{"error": "tuned_x is no longer available"}') == ("unknown_tool", "tuned_x")

def test_classify_unwraps_mcp_errors():
    """Catches: matching on the raw text without stripping the wrapper, or API
    failures counted as the run's own errors."""
    assert classify(GET, ARG_ERR)[0] == "tool_error"
    assert classify(GET, DOWN) == ("server_unreachable", "google")   # checked before tool_error
    assert classify(GET, API_ERR)[0] == "upstream_error"
    assert classify("mcp__google__search_gmail_messages", WRAP("mcp__google__search_gmail_messages",
        '{"error": "1 validation error for call[search_gmail_messages]\\nmax_results\\n  Unexpected keyword argument"}'))[0] == "tool_error"

def test_classify_strips_hermes_loop_suffix():
    """The real incident shape. Catches: whole-text json.loads (misses 3 of the 7
    error results in the 11-04 run), or stripping any trailing text."""
    assert classify(GET, ARG_ERR + LOOP)[0] == "tool_error"
    assert classify(GET, DOWN + LOOP)[0] == "server_unreachable"
    assert classify(GET, ARG_ERR + "\n\nmore text") is None
    assert classify(GET, ARG_ERR + LOOP + LOOP) is None

def test_classify_ignores_error_text_inside_content():
    """Review Focus 1. Catches: substring matching anywhere in the result."""
    assert classify(GET, WRAP(GET, 'Subject: hi\n{"error": "Unknown tool: x"}')) is None
    assert classify("tuned_x", '{"result": {"error": "Unknown tool: x"}}') is None
    assert classify(GET, OK) is None

def test_signals_on_the_incident_a_run():
    """The 11-04 briefing run as stored: 2 unknown-tool lookups, 3 argument errors
    (the third with the loop suffix), 2 unreachable (both with it), 35 ok.
    Catches: a signal missing from CHECKS, order not SIGNALS, or the suffix not stripped."""
    res = (("tool_describe", UNKNOWN),) * 2 + ((GET, ARG_ERR),) * 2 + ((GET, ARG_ERR + LOOP),) \
        + ((GET, DOWN + LOOP),) * 2 + ((GET, OK),) * 35
    assert signals(RunEvidence(res, fix_text=FIX), Baseline((0.0,) * 17)) == [
        "unknown_tool", "server_tripped", "self_inflicted_errors"]

def test_unknown_tool_needs_the_fix_to_name_it():
    """10-30..11-03: a stale rolled-back tool called by every run, named by no fix.
    Catches: unknown_tool firing on any unknown name."""
    stale = (("tuned_old", '{"error": "tuned_old is no longer available"}'),)
    assert "unknown_tool" not in signals(RunEvidence(stale, fix_text=FIX), Baseline())
    assert "unknown_tool" in signals(RunEvidence(stale, fix_text="Use tuned_old."), Baseline())
    assert not names_tool("Use tuned_older.", "tuned_old")

def test_unknown_tool_excuses_registered_core_tools():
    """Hermes says "not a deferrable tool" for core tools too. Catches: ignoring
    RunEvidence.tools."""
    core = (("tool_describe", '{"error": "\'terminal\' is not a deferrable tool. If you see it in the tools list already, call it directly."}'),)
    assert "unknown_tool" not in signals(RunEvidence(core, fix_text="Run terminal.", tools=frozenset({"terminal"})), Baseline())
    assert "unknown_tool" in signals(RunEvidence(core, fix_text="Run terminal."), Baseline())

def test_upstream_errors_are_not_harm():
    """A Google API failure followed by the server being cut off is an outage.
    Catches: upstream_error counted in self_inflicted_errors or server_tripped."""
    res = ((GET, API_ERR),) * 5 + ((GET, DOWN),) + ((GET, OK),) * 5
    assert signals(RunEvidence(res), Baseline()) == []

def test_server_tripped_needs_the_run_s_own_errors_first():
    """Catches: firing on a plain outage, or on errors from another server."""
    other = "mcp__compute__run_task"
    assert "server_tripped" not in signals(RunEvidence(((GET, DOWN), (GET, OK))), Baseline())
    assert "server_tripped" not in signals(
        RunEvidence(((other, WRAP(other, '{"error": "Error calling tool \'run_task\': x"}')), (GET, DOWN))), Baseline())
    assert "server_tripped" in signals(RunEvidence(((GET, ARG_ERR), (GET, DOWN))), Baseline())

@pytest.mark.parametrize("errors, total, base_mean, fired", [
    (2, 38, 0.0, False),   # the 11-02 run: under SELF_ERR_MIN
    (3, 40, 0.0, True),
    (3, 60, 0.0, False),   # rate 0.05 == threshold: strictly above only
    (3, 10, 0.2, False),   # 0.30 <= 2*0.2 + 0.05
    (5, 10, 0.2, True)])
def test_self_inflicted_errors_threshold(errors, total, base_mean, fired):
    """Catches: SELF_ERR_MIN/FACTOR/MARGIN off by one, or >= instead of >."""
    res = ((GET, ARG_ERR),) * errors + ((GET, OK),) * (total - errors)
    assert ("self_inflicted_errors" in signals(RunEvidence(res), Baseline((base_mean,) * 4))) is fired

def test_run_failed_unless_outage():
    """Catches: run_failed ignoring the outage flag, or firing on unknown status."""
    assert "run_failed" in signals(RunEvidence((), status="error"), Baseline())
    assert "run_failed" in signals(RunEvidence((), status="ok", delivery_failed=True), Baseline())
    assert "run_failed" not in signals(RunEvidence((), status="error"), Baseline(outage=True))
    assert "run_failed" not in signals(RunEvidence((), status=None), Baseline())
```

- [ ] **Step 2: Run and confirm they fail**

Run: `cd tuner && uv run pytest -q tests/test_harm.py`
Expected: FAIL, `ModuleNotFoundError: hermes_tuner.harm`.

- [ ] **Step 3: Implement `harm.py`**

`classify`: unwrap with `history._unwrap(text, frozenset({name}))`, strip, remove one trailing match of `LOOP_SUFFIX = re.compile(r"\n\n\[Tool loop (?:warning|hard stop): [a-z_]+; count=\d+; [^\]\n]*\]\Z")` (if the unwrap leaves whitespace after it, strip first), strip again, `json.loads`; only a dict whose `"error"` value is a string counts (anything else, including a parse failure, is None). Then match the error string, anchored at its start, in order: unreachable (`^MCP server '([^']+)' is unreachable after`), unknown tool (`^'([^']+)' is not a deferrable tool\.`, `^Unknown tool: (\S+)$`, `^(\S+) is no longer available$`), `^Error calling tool '[^']*': API error in ` → `upstream_error`, then `^Error calling tool '`, `^\d+ validation errors? for call\[`, `^tool_call to '[^']+' is missing required argument` → `tool_error`. `unknown_tool` fires when some result is `("unknown_tool", n)` with `names_tool(run.fix_text, n)` and `n not in run.tools`. `server_tripped`: walk results in order, track servers (`name.split("__")[1]` for `mcp__<server>__<tool>`) that returned `tool_error` (not `upstream_error`); fire on a `server_unreachable` result whose subject is already in that set. `self_inflicted_errors`: `tool_error` count ≥ `SELF_ERR_MIN` and `error_rate > SELF_ERR_FACTOR * mean(base.error_rates or (0.0,)) + SELF_ERR_MARGIN`. `run_failed`: `(status == "error" or delivery_failed) and not base.outage`. `harm.py` reads `policy.HARM_CHECKS` at call time (`from . import policy`, not `from .policy import HARM_CHECKS`), so the switch test's monkeypatch takes effect.

- [ ] **Step 4: Run and confirm they pass**

Run: `cd tuner && uv run pytest -q tests/test_harm.py` → all pass.

- [ ] **Step 5: Commit**

```bash
git add tuner/hermes_tuner/harm.py tuner/hermes_tuner/policy.py tuner/tests/test_harm.py
git commit -m "feat(tuner): harm signals over a run's tool results"
```

---

### Task 1b: the registered-tools bridge op

Moved ahead of Task 2 because `unknown_tool` needs the registered names to excuse core tools (Global Constraints). Task 6 keeps the `LimitContext` part.

**Files:**
- Modify: `tuner/hermes_tuner/bridge_entry.py` (op `registered_tools`, pure helper `_registered`), `tuner/hermes_tuner/bridge.py` (protocol method, `HermesBridge.registered_tools`, `FakeBridge(tools=...)`), `tuner/hermes_tuner/cycle.py` (`_Cycle._registered_tools`)
- Test: the existing bridge_entry / bridge test files, `tuner/tests/test_cycle.py`

**Interfaces:**
- Produces:
  - `bridge_entry._registered(native: list[str], mcp_servers: dict) -> dict` → `{"native": sorted(native), "mcp": sorted(f"mcp__{s}__{t}" for each server's tools.include), "mcp_complete": bool}` (`mcp_complete` False when any server has no `tools.include` list). Op `registered_tools()` calls it with `model_tools.get_all_tool_names()` and the config's `mcp_servers`.
  - `Bridge.registered_tools(self) -> dict` (same shape); `FakeBridge(..., tools: dict | None = None)` returns `tools` (default `{"native": [], "mcp": [], "mcp_complete": False}`) or raises if `"registered_tools"` is in `fail`.
  - `_Cycle._registered_tools(self) -> frozenset[str]` — `native ∪ mcp` from one bridge call per cycle (cached); `frozenset()` on `BridgeError`, logged once as `self.err(f"registered_tools: {exc}")`, with `self.registered_ok = False` (True after a successful call) so Task 6 can pass None to the limits instead of an empty set.

Measured on the droplet (2026-11-04): outside the gateway, `model_tools.get_all_tool_names()` returns 81 names, including `terminal`, none starting `mcp__`, and not `tool_describe`. So MCP names must come from the config includes, as above.

- [ ] **Step 1: Write the failing tests**

```python
def test_registered_names_mcp_tools_from_includes():
    """Catches: wrong mcp__<server>__<tool> format, or mcp_complete True with a server
    that has no include list."""
    out = _registered(["terminal"], {"google": {"tools": {"include": ["get_events"]}}, "compute": {}})
    assert out == {"native": ["terminal"], "mcp": ["mcp__google__get_events"], "mcp_complete": False}

def test_registered_tools_cached_and_fail_soft(env):
    """Catches: one bridge call per check instead of per cycle, or a BridgeError escaping."""
    # FakeBridge(tools={"native": ["terminal"], "mcp": [GET], "mcp_complete": True}); call
    # _Cycle._registered_tools twice -> frozenset({"terminal", GET}), bridge.calls counts one call.
    # FakeBridge(fail={"registered_tools"}) -> frozenset(), one "registered_tools:" error.
```

- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run the whole suite** → all pass.
- [ ] **Step 5: Check the op on the droplet** (read-only): `ssh hermes-vm 'cd ~/hermes-harness && git fetch -q && git show origin/<branch>:tuner/hermes_tuner/bridge_entry.py > /tmp/be.py && echo {} | ~/.hermes/hermes-agent/venv/bin/python /tmp/be.py registered_tools'`. Expected: `"result"` with `native` containing `terminal`, `mcp` containing `mcp__google__get_events`, and no `tuned_batch_fetch_gmail_contents`. If it fails, record the error in the PR and stop: Task 2 depends on it.
- [ ] **Step 6: Commit** — `feat(tuner): bridge op listing registered tools`.

---

### Task 2: fail a shadow that shows harm

**Files:**
- Modify: `tuner/hermes_tuner/cycle.py` (`evaluate_shadows`, new `_harm` helper)
- Test: `tuner/tests/test_cycle.py` (new section `# --- harm checks ---`; `shadow_setup` gains `prompt="new prompt"`, used for the job_prompt fix's `payload["prompt"]`)

**Interfaces:**
- Consumes: Task 1 `RunEvidence`, `Baseline`, `signals`, `error_rate`; Task 1b `_registered_tools`; `History.call_results(session_id) -> list[tuple[str, str]]`.
- Produces: `_Cycle._harm(self, rec: FixRecord, run: Session, before: float, status: str | None = None, delivery_failed: bool = False, outage: bool = False) -> str | None` — the first signal for `run`, using as baseline the target job's runs (`s.job_id == rec.target`, `s.calls`, not shadow ids) that started in `[before - trial.BASELINE_DAYS * DAY, before)`. It fills `RunEvidence.fix_kind = rec.kind`, `tools = self._live_tools() | self._registered_tools()`, and `fix_text` = every str leaf of `self.ledger.fix(rec.id).payload` (excluding the `_base` key, which is the old prompt) joined with `"\n"` (an unreadable fix gives `""`). None when `policy.HARM_CHECKS` is False or `rec.kind not in JOB_KINDS`. Lets `HistoryUnavailable` propagate.

- [ ] **Step 1: Write the failing tests**

Add a helper that reuses the passing arguments of `test_shadow_pass_applies_with_priced_baseline_cost`, so the quality compare passes and only harm can fail the shadow. Its prompt names `tuned_x`, the tool `UNKNOWN` reports missing:

```python
def passing_shadow(env, extra_calls=(), prompt="Fetch mail with tuned_x."):
    return shadow_setup(env, old_mod=[(MOD, {"message_id": "m2", "add_label_ids": ["TRASH"]}, "ok")],
                        new_mod=[{"message_id": "m2", "add_label_ids": ["TRASH"]}],
                        extra_calls=list(extra_calls), prompt=prompt)
```

```python
def test_shadow_that_looks_up_a_missing_tool_fails(env):
    """Incident A at shadow time. Catches: harm not wired into evaluate_shadows."""
    fix = passing_shadow(env, [("tool_describe", {}, UNKNOWN)])
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert (rec.state, rec.reason) == ("shadow_failed", "harm: unknown_tool")

def test_shadow_with_baseline_level_errors_goes_live(env):
    """Two argument errors, as in an ordinary run. Catches: any error failing a shadow."""
    fix = passing_shadow(env, [(GET, {}, ARG_ERR)] * 2)
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"

def test_harm_checks_switch_off(env, monkeypatch):
    """Catches: policy.HARM_CHECKS ignored."""
    monkeypatch.setattr(policy, "HARM_CHECKS", False)
    fix = passing_shadow(env, [("tool_describe", {}, UNKNOWN)])
    run_cycle(env, FakeBridge(jobs=jobs()), NOW)
    assert Ledger.load(env.root).get(fix.id).state == "trial"
```

Put `UNKNOWN` and `ARG_ERR` (plain JSON is enough here; `classify` accepts unwrapped text) at module level next to `TOOL_ERR`.

- [ ] **Step 2: Run and confirm the first fails** — `uv run pytest -q tests/test_cycle.py -k "missing_tool or baseline_level or switch_off"`; expected: the first FAILS (state `trial`), the others pass.

- [ ] **Step 3: Implement** — in `evaluate_shadows`, right after the `_tool_check` block: `sig = self._harm(rec, shadow, before=shadow.started_at)`; if set, `self._shadow_failed(rec, f"harm: {sig}")` and `continue`. Wrap in the same `HistoryUnavailable` handling `_early_failures` uses (log via `self.err(f"harm check {rec.id}: ...")`, then carry on without harm for that record).

- [ ] **Step 4: Run** the three tests, then `uv run pytest -q tests/test_cycle.py` → all pass.

- [ ] **Step 5: Commit** — `git add tuner/hermes_tuner/cycle.py tuner/tests/test_cycle.py` / `feat(tuner): fail a shadow that shows harm`.

---

### Task 3: roll back a job fix in trial on harm

**Files:**
- Modify: `tuner/hermes_tuner/cycle.py` (`judge_trials`, new `_harm_in_trial`)
- Test: `tuner/tests/test_cycle.py`

**Interfaces:**
- Consumes: Task 2 `_harm`; `Bridge.list_jobs`; `_trial_runs(rec)`.
- Produces: `_Cycle._harm_in_trial(self, rec: FixRecord, runs: list[Session]) -> str | None` — the first signal over `runs` (oldest first). One `list_jobs()` call per cycle (cached) gives both the target job's status and the outage check; do not use `get_job` here, because the rollback itself needs `get_job` (`ledger._rollback_job`) and a test that fails `get_job` would then test nothing. Job status (`last_status` in Hermes is `"ok"`/`"error"`; `last_delivery_error` is overwritten each run, None on successful delivery — Hermes cron/jobs.py:1665-1668, read 2026-11-04) applies only to the newest run, and only when `last_run_at` (ISO or epoch, parse with `trial._epoch`) is at or after that run's `started_at`. `outage` is True when another job from `list_jobs()` has `last_status == "error"` with `last_run_at` within `OUTAGE_WINDOW_S` of the run's start. A `BridgeError` logs `self.err(f"harm status {rec.id}: {exc}")` and leaves status None and outage False; the result-based signals still apply. Baseline cutoff is `rec.live_from`.

- [ ] **Step 1: Write the failing tests**

Helper `job_prompt_trial(env)`: like `composed_trial` but `passing_shadow(env)` (job_prompt, prompt names `tuned_x`), returning `(fix, bridge)` with the record in `trial` and `live_from = NOW`. Helper `later_run(calls, day=1)`: one triage run at `NOW - 3*3600 + 7*3600 + day*DAY` with those `(name, args, result)` calls plus one `(GET, {"message_id": "m"}, "b")`.

```python
def test_job_prompt_trial_rolls_back_on_unknown_tool(env):
    """Incident A: a live job_prompt fix whose first run looks up a missing tool.
    Catches: harm checks still limited to composed tools and reducers."""
    fix, bridge = job_prompt_trial(env)
    env.db.unlink(); build(env, SETUP_SESSIONS[env.root] + [later_run([("tool_describe", {}, UNKNOWN)])])
    run_cycle(env, bridge, NOW + 2 * DAY)
    rec = Ledger.load(env.root).get(fix.id)
    assert (rec.state, rec.reason, rec.trial_runs) == ("rolled_back", "harm: unknown_tool (early)", 1)
    assert bridge.jobs[TRIAGE]["prompt"] == "Triage the inbox."

@pytest.mark.parametrize("cal_status, rolled", [("ok", True), ("error", False)])
def test_trial_run_failed_unless_another_job_failed_too(env, cal_status, rolled):
    """Catches: run_failed rolling back during an outage that hit every job."""
    fix, bridge = job_prompt_trial(env)
    t = NOW - 3 * 3600 + 7 * 3600 + DAY
    env.db.unlink(); build(env, SETUP_SESSIONS[env.root] + [later_run([])])
    bridge.jobs[TRIAGE] |= {"last_status": "error", "last_run_at": iso(t + 60)}
    bridge.jobs[CAL] |= {"last_status": cal_status, "last_run_at": iso(t - 3600)}
    run_cycle(env, bridge, NOW + 2 * DAY)
    rec = Ledger.load(env.root).get(fix.id)
    assert (rec.state == "rolled_back") is rolled
    if rolled:
        assert rec.reason == "harm: run_failed (early)"

def test_trial_harm_survives_job_status_failure(env):
    """Review Focus 4. Catches: a list_jobs failure aborting judge_trials or
    suppressing the result-based signals."""
    fix, bridge = job_prompt_trial(env)
    env.db.unlink(); build(env, SETUP_SESSIONS[env.root] + [later_run([("tool_describe", {}, UNKNOWN)])])
    bridge.fail = {"list_jobs"}
    report = run_cycle(env, bridge, NOW + 2 * DAY)
    assert Ledger.load(env.root).get(fix.id).state == "rolled_back"
    assert any(e.startswith(f"harm status {fix.id}:") for e in report.errors)

def test_trial_ignores_a_stale_tool_the_fix_does_not_name(env):
    """The 10-30..11-03 pattern. Catches: fix_text not passed in the trial path."""
    fix, bridge = job_prompt_trial(env)
    stale = '{"error": "tuned_old is no longer available"}'
    env.db.unlink(); build(env, SETUP_SESSIONS[env.root] + [later_run([("tuned_old", {}, stale)])])
    run_cycle(env, bridge, NOW + 2 * DAY)
    assert Ledger.load(env.root).get(fix.id).state == "trial"

def test_config_trial_ignores_harm(env):
    """Catches: harm applied to config or skill fixes, whose runs are every chat."""
    fix = config_trial(env, applied_days_ago=2)
    chat = {"id": "20261019_010000_ab12cd", "source": "telegram", "started_at": NOW - 2 * 3600,
            "cost": 0.05, "in": 10_000, "out": 10,
            "calls": [("tool_describe", {"name": "x"}, UNKNOWN)]}
    build(env, [chat])
    run_cycle(env, FakeBridge(jobs=jobs(), config={CFG_KEY: 0.7}), NOW)
    rec = Ledger.load(env.root).get(fix.id)
    assert rec.state == "trial" and not (rec.reason or "").startswith("harm:")
```

`applied_days_ago=2` keeps the config trial short of `GLOBAL_TRIAL_DAYS`, so only a harm rollback could end it.

`list_jobs` is also called by the restart step; confirm with a quick read that a failing `list_jobs` there is caught (it is inside a `try` at cycle.py:757 at 7f55593) so the test's other errors do not hide the one asserted.

- [ ] **Step 2: Run and confirm the first two fail** — `uv run pytest -q tests/test_cycle.py -k "unknown_tool or run_failed or status_failure or ignores_harm"`.

- [ ] **Step 3: Implement** — in `judge_trials`, inside `if rec.live_from is not None and rec.kind in JOB_KINDS:` and before the existing early-failure check: `sig = self._harm_in_trial(rec, runs)`; if set, `self._rollback(rec, f"harm: {sig} (early)", runs)` and `continue`. Handle `HistoryUnavailable` like `_early_failures`.

- [ ] **Step 4: Run** the new tests, then the whole suite: `uv run pytest -q > /tmp/t3.log 2>&1; tail -3 /tmp/t3.log` → all pass.

- [ ] **Step 5: Commit** — `feat(tuner): roll back a job fix in trial when its runs show harm`.

---

### Task 4: harm reason classes, drafting history, rollback notices

**Files:**
- Modify: `tuner/hermes_tuner/backoff.py` (`reason_class`), `tuner/hermes_tuner/draft.py` (`REASON_CLASSES`, `INSTRUCTIONS`), `tuner/hermes_tuner/cycle.py` (`_rollback`)
- Test: `tuner/tests/test_backoff.py`, `tuner/tests/test_cycle.py`

**Interfaces:**
- Consumes: `harm.SIGNALS`; Task 3 rollback reasons.
- Produces: `reason_class(rec)` returns `f"harm_{signal}"` for any record whose reason starts with `"harm: "` and whose next word is in `SIGNALS` (checked before the state branches); otherwise unchanged. `draft.REASON_CLASSES` gains the four `harm_*` classes. `_rollback` writes the notice only when `out.state == "rolled_back"` and not in a dry run. An `owner_changed` outcome writes no notice: nothing was rolled back, and "tuner rolled back" would be false.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("state, reason, cls", [
    ("rolled_back", "harm: unknown_tool (early)", "harm_unknown_tool"),
    ("shadow_failed", "harm: server_tripped", "harm_server_tripped"),
    ("rolled_back", "harm: bogus (early)", "trial"),
    ("rolled_back", "trial: not cheaper than baseline", "trial")])
def test_reason_class_maps_harm_reasons(state, reason, cls):
    """Catches: harm reasons falling into the generic trial/shadow class, or an
    unknown signal name becoming a class."""

def test_harm_classes_reach_the_drafting_request(env):
    """Catches: harm_* missing from REASON_CLASSES, so the class is dropped from history."""
    # a rolled_back record with reason "harm: unknown_tool (early)" for the candidate,
    # then a drafting cycle; the request's history[0]["reason_class"] == "harm_unknown_tool"

def test_rollback_writes_one_outbox_line(env):
    """Catches: no notice, a file name the listener's *.txt glob misses, or two lines."""
    # job_prompt_trial + the unknown-tool run (Task 3), then:
    assert (env.root / "outbox" / f"rollback-{fix.id}.txt").read_text() == \
        f"tuner rolled back {fix.id} (morning-briefing): harm: unknown_tool (early)\n"

def test_owner_changed_writes_no_notice(env):
    """Catches: a notice written for every _rollback call, not only a real rollback."""
    # job_prompt_trial + the unknown-tool run, then change bridge.jobs[TRIAGE]["prompt"]
    # to an owner edit before the cycle; record ends owner_changed, no rollback-*.txt

def test_dry_run_harm_rollback_writes_no_notice(env):
    """Review Focus 5. Catches: dry run rolling back or writing the outbox."""
    # same setup, then run_cycle(dataclasses.replace(env, dry_run=True), bridge, NOW + 2 * DAY)
    # record still "trial", f"rollback {fix.id}" in report.planned, no rollback-*.txt
```

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement.** Notice text: `f"tuner rolled back {rec.id} ({name}): {out.reason}\n"`, where `name` is the job's `name` from the job read this cycle if there is one, else `rec.target`; write it atomically with `ledger.atomic_write`. In `INSTRUCTIONS`, after the `history lists earlier drafts…` sentence, add this exact sentence: `A reason_class starting with "harm_" means the fix was undone because its runs went wrong: harm_unknown_tool, the agent tried a tool that does not exist; harm_server_tripped, its calls failed until a server was cut off; harm_self_inflicted_errors, many of its calls were malformed; harm_run_failed, the run failed. Do not repeat that design.`

- [ ] **Step 4: Run** the new tests and the whole suite → all pass.

- [ ] **Step 5: Commit** — `feat(tuner): harm reason classes and rollback notices`.

---

### Task 5: P2 — the tuner edits only its own block of a job prompt

**Files:**
- Create: `tuner/hermes_tuner/prompt_block.py`
- Modify: `tuner/hermes_tuner/draft.py` (`INSTRUCTIONS` items 1 and 2, `_clean_job_prompt`), `tuner/hermes_tuner/cycle_draft.py` (`_draft_one`, `_limits`), `tuner/hermes_tuner/limits.py` (`LimitContext.job_prompt`, `_check_job_prompt`, `_check_composed`)
- Test: `tuner/tests/test_prompt_block.py`, `tuner/tests/test_limits.py`, `tuner/tests/test_cycle.py`

**Interfaces:**
- Produces:
  - `START: str`, `END: str` (Global Constraints)
  - `strip_block(text: str) -> str` — removes `"\n\n" + START` … `END` (the first well-formed block) and nothing else; text without a well-formed block is returned unchanged.
  - `splice(current: str, block: str) -> str` — `strip_block(current) + "\n\n" + START + "\n" + block.strip() + "\n" + END`.
  - `check(new: str, current: str | None) -> str | None` — reason or None, see Step 1.
  - `with_prompt(fix: Fix, job: dict | None) -> Fix` — job_prompt payload `{"block", "add_toolsets"}` → `{"prompt": splice(job["prompt"], block), "add_toolsets"}`; composed_tool `job_prompt` (a block, str) → `splice(job["prompt"], it)`; None stays None. When the job prompt is not a str, `prompt`/`job_prompt` is set to the bare block, which `check` then rejects.
  - `LimitContext.job_prompt: str | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("owner", ["Triage.", "Triage.\n", "Triage.  ", ""])
def test_splice_keeps_owner_text_byte_for_byte(owner):
    """Review Focus 3. Catches: rstrip/strip applied to the owner's text."""
    assert strip_block(splice(owner, " Use batch reads. ")) == owner

def test_splice_replaces_existing_block():
    """Review Focus 2. Catches: appending a second block."""
    assert splice(splice("Triage.", "a"), "b") == splice("Triage.", "b")

def test_check_rejects_dropped_owner_rules():
    """Incident A's second failure. Catches: comparing only the block."""
    cur = "Triage. Always keep security alerts."
    assert check(splice(cur, "x").replace(" Always keep security alerts.", ""), cur) is not None

@pytest.mark.parametrize("new", [
    lambda c: splice(c, "x") + "\n" + START + "\nx\n" + END,   # two blocks
    lambda c: splice(c, "x") + " trailing",                      # block not at the end
    lambda c: splice(c, "x " + END + " y")])                     # marker smuggled in the block
def test_check_rejects_malformed_blocks(new):
    """Catches: counting markers loosely or allowing text after END."""
    assert check(new("Triage."), "Triage.") is not None

def test_check_needs_the_current_prompt():
    """Catches: passing when the job could not be read."""
    assert check(splice("Triage.", "x"), None) is not None
    assert check(splice("Triage.", "x"), "Triage.") is None
```

In `test_limits.py`: `ctx()` gains `job_prompt=OWNER` by default (`OWNER = "do the thing"`) and `jp()` defaults its prompt to `splice(OWNER, "be brief")`. Add:

```python
def test_job_prompt_must_keep_owner_text(tmp_path):
    """Catches: P2 not wired for job_prompt or for a composed tool's job_prompt."""
    assert any("owner's text" in r for r in check_fix(jp(prompt="rewritten"), ctx(tmp_path)))
    assert any("owner's text" in r for r in check_fix(composed(job_prompt="rewritten"), ctx(tmp_path)))
    assert not any("owner's text" in r for r in check_fix(composed(job_prompt=None), ctx(tmp_path)))
```

In `test_cycle.py`, change `JOB_PROMPT_REPLY` to `{"kind": "job_prompt", "target": TRIAGE, "payload": {"block": "Skip mail already starred.", "add_toolsets": []}}` and add:

```python
def test_cycle_splices_the_drafted_block(env):
    """Catches: the block reaching the ledger unspliced, or the owner text altered."""
    # draft JOB_PROMPT_REPLY as test_cycle_drafts_and_shadows_when_job_is_free does
    assert Ledger.load(env.root).fix(rec.id).payload["prompt"] == splice("Triage the inbox.", "Skip mail already starred.")
```

- [ ] **Step 2: Run and confirm they fail** — `uv run pytest -q tests/test_prompt_block.py tests/test_limits.py tests/test_cycle.py`.

- [ ] **Step 3: Implement.** `check` reasons, exact text: `"current job prompt unknown"`; `"job prompt must contain exactly one tuner notes block"` (START or END count ≠ 1, or END before START); `"tuner notes block must end the prompt"`; `"job prompt edit changes the owner's text outside the tuner notes block"` (`strip_block(new) != strip_block(current)`). `_check_job_prompt(fix, ctx)` and `_check_composed` (when `job_prompt` is a str) append `check(...)`. `_draft_one` applies `with_prompt(fix, job)` right after a successful parse. `_limits` passes `job_prompt=job.get("prompt") if isinstance(job, dict) else None`. `INSTRUCTIONS` item 2 becomes: `2. "job_prompt": add notes to a job's prompt. target is the job id from the request. payload: {"block": str, "add_toolsets": [str]}. block is text placed at the end of the job's prompt under a "tuner notes" heading; the owner's prompt is kept unchanged, and an existing tuner notes block is replaced by yours. Write only the block. add_toolsets may only be ["<TOOLSET>"] or [].` In item 1, `job_prompt` becomes: `job_prompt is optional block text (as in "job_prompt" below) telling the agent to use the tool, or null.` Then update every existing test whose job_prompt payload is not a splice of its job's prompt (grep `"prompt":` in `tests/test_cycle.py` and `jp(` in `tests/test_limits.py`) by building it with `splice`; do not weaken any assertion.

- [ ] **Step 4: Run the whole suite** → all pass; the count rises by the new tests only.

- [ ] **Step 5: Commit** — `feat(tuner): job prompt edits keep the owner's text (P2)`.

---

### Task 6: the input snapshot for later checks

The bridge op and `_Cycle._registered_tools` are built in Task 1b, and `_harm` already fills `RunEvidence.tools`, `fix_kind` and `fix_text` (Task 2). This task passes the same snapshot to `check_fix`.

**Files:**
- Modify: `tuner/hermes_tuner/limits.py` (`LimitContext` fields), `tuner/hermes_tuner/cycle_draft.py` (`_limits`)
- Test: `tuner/tests/test_cycle.py`

**Interfaces:**
- Consumes: Task 1b `_registered_tools`, `bridge.FakeBridge(tools=...)`.
- Produces: `LimitContext.live_tools: frozenset[str] = frozenset()`, `registered_tools: frozenset[str] | None = None`, `job_policy: str | None = None`. `_limits` passes `live_tools`, `registered_tools` (None when Task 1b's call failed this cycle, so a future check can tell "unknown" from "none"; track the failure in `_registered_tools` with a flag rather than treating an empty set as failure) and `job_policy=policy.EDITABLE_JOBS.get(fix.target)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_limits_receive_the_snapshot(env, monkeypatch):
    """Catches: the snapshot not passed to check_fix."""
    seen = []
    monkeypatch.setattr(cycle_draft, "check_fix", lambda fix, ctx: seen.append(ctx) or [])
    # draft JOB_PROMPT_REPLY with FakeBridge(jobs=jobs(), tools={"native": ["terminal"], "mcp": [GET], "mcp_complete": True}, ...)
    (c,) = seen
    assert (c.registered_tools, c.job_policy) == (frozenset({"terminal", GET}), "triage")

def test_unreadable_tool_list_gives_none(env, monkeypatch):
    """Catches: a bridge failure aborting drafting, or reported as an empty set."""
    # same, FakeBridge(fail={"registered_tools"}); c.registered_tools is None and the fix is still shadowed
```

`TRIAGE` in test_cycle.py is `"3bc4e11d29b1"`; confirm `policy.EDITABLE_JOBS[TRIAGE] == "triage"` before relying on it, else assert against `policy.EDITABLE_JOBS.get(TRIAGE)` and say so in the docstring. Patching `cycle_draft.check_fix` is a module attribute read at call time (`from .limits import ... check_fix` binds the name in `cycle_draft`), not a default argument.

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement** the interfaces above.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit** — `feat(tuner): give checks a snapshot of live and registered tools`.

---

### Task 7: ship and watch the first rollback

**Files:** none new. Runbook note in `runbooks/tuner.md` (harm checks, notices, the `HARM_CHECKS` switch, P2 markers).

- [ ] **Step 1: Full gates** — `cd tuner && uv run pytest -q > /tmp/loop1.log 2>&1; tail -3 /tmp/loop1.log` (record the count against the 923 at 7f55593); `git diff --stat origin/master -- broker box executor tuner/plugin .github tuner/hermes_tuner/fingerprint.py` → empty.
- [ ] **Step 2: Mutation check** each new test file: for every `Catches:` line, make that mutation, confirm the test fails, restore (repo rule).
- [ ] **Step 3: PR** — push the branch, open the PR with the spec and this plan; wait for `gh pr checks <n> --watch` to report all passes; merge only on the owner's word.
- [ ] **Step 4: Confirm deploy** — `ssh hermes-vm 'cd ~/hermes-harness && git log --oneline -1'` shows the merge within 15 minutes (harness-pull). No gateway restart: the plugin is unchanged.
- [ ] **Step 4b: Replay the signals on live data** (read-only, before merging): copy the branch's `harm.py` to the droplet and, with the tuner venv, run `harm.signals` with `fix_text=""` over every briefing and calendar run of the last 28 days (baseline = that job's runs in the 28 days before each). Print only run ids and signal names. Expected: `self_inflicted_errors` and `server_tripped` fire only on `cron_3bc4e11d29b1_20261104_080057`, and nothing fires anywhere else. Then the same run with `fix_text` = the prompt in `versions/20261103-23272612/after.json`: `unknown_tool` also fires. Print no result text: state.db is email-derived. Any other run firing is a finding to fix before merge.
- [ ] **Step 5: Dry run** — `ssh hermes-vm 'cd ~/hermes-harness/tuner && .venv/bin/python -m hermes_tuner run --dry-run'`; expected: `planned` contains `rollback 20261103-23272612` (if the fix is still in trial), no errors.
- [ ] **Step 6: After the next 03:30 cycle** — ledger record `20261103-23272612` is `rolled_back` with reason `harm: unknown_tool (early)`; `sha256` of the live briefing prompt equals that of `versions/20261103-23272612/before.json`'s prompt; `outbox/rollback-20261103-23272612.sent` exists. Report each as MEASURED with the command output.
