# Hermes Tuner — model Hermes's config coercion per key (amendment)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Let the tuner set string values such as `"on"`/`"off"` on string-typed keys
(`tools.tool_search.enabled`, `tools.tool_search.listing`, `compression.codex_app_server_auto`),
which the M2 round-trip check now refuses, while still refusing values Hermes would silently
change. Owner request 2026-10-19.

**Ground truth (pinned Hermes, `hermes_cli/config.py:9277-9286`):** `set_config_value(key, value: str)`
coerces `true/yes/on`→True, `false/no/off`→False, `isdigit`→int, one-dot digits→float — ONLY when
`_default_value_for_key(key)` (config.py:9020-9031, leaf of `DEFAULT_CONFIG`, None for unknown or
non-leaf keys) is NOT a `str`. String-default keys store the string unchanged.

**Branch:** `docs/hermes-tuner-design` at d657ade. Read-only Hermes source:
`/tmp/claude-1000/-home-fibonadithya-hermes-harness/dedb1af4-e258-4666-8f7a-15c7e5722e35/scratchpad/hermes-agent-src`.

### Task 1: Key-aware coercion model and read-back verification

**Files:** `tuner/hermes_tuner/bridge.py`, `bridge_entry.py`, `ledger.py`, `tests/test_ledger.py`.

**Changes:**
1. New read-only bridge op `config_default_is_str(key) -> bool` (Protocol, HermesBridge, FakeBridge
   with a settable set of string-default keys; NOT mutating). bridge_entry implements it with
   `hermes_cli.config._default_value_for_key(key)` imported inside the op; returns
   `isinstance(default, str)`. If the import fails → non-zero exit (BridgeError) → the config fix is
   refused (fail closed), never guessed.
2. `ledger._hermes_coerce(value_str, default_is_str)` mirrors config.py:9277-9286 exactly, applying
   the coercion only when `default_is_str` is False. `_round_trips(value, default_is_str)` uses it.
   Both the NEW value (M2) and the OLD value (rollback restorability) are checked with the key's
   `default_is_str`, fetched once per apply/rollback.
3. Read-back verification: after `config_set` in apply, re-read with `config_get`; if the stored
   value does not STRICTLY equal the intended value (`type(a) is type(b) and a == b`; do NOT use the
   existing `_norm`/`_same_config` helpers, which normalise and could hide "off"→False), restore
   the old value, re-read, and record `bridge_error` with reason
   `config <key> did not store as written` (if the restore read-back also mismatches, the reason
   says so and names the value left in place). Same verification after the rollback write
   (mismatch → keep applied state, reason `rollback failed: ...`, retryable as today). This also
   catches Hermes's managed-mode silent no-op (deferred T6 minor). `after.json` records the value
   READ BACK (which equals the intended value when verification passes), so later owner-edit
   detection compares against what Hermes actually stored.
4. Audit note: the strict read-back is the safety net; the coercion model (2) is what lets valid
   string values through. A test must show each layer catching its own failure.

**Tests (each names the mutation it catches):**
- `test_string_default_key_accepts_on_off`: `config_default_is_str` True for
  `tools.tool_search.enabled`, old "auto", new "off" → applied, live value is the string "off";
  rollback restores "auto". *Catches:* coercion applied regardless of default type (the M2 bug).
- `test_non_string_default_key_still_refuses_coercible_string`: default not str, new value "on"
  (string) → refused, nothing written. *Catches:* dropping the coercion model entirely.
- `test_non_string_default_key_refuses_unrestorable_old`: default not str, old value -1 → refused.
  *Catches:* old-value check removed.
- `test_default_lookup_failure_refuses`: FakeBridge raises BridgeError on `config_default_is_str`
  → bridge_error, no `config_set` call. *Catches:* guessing a type when Hermes can't be asked.
- `test_readback_mismatch_restores_and_fails`: FakeBridge stores a different value than written
  (simulated coercion/no-op) → old value restored, state bridge_error, reason names the key.
  *Catches:* trusting the write without reading back. Include the case written "off" read back
  False — a normalising comparison would wrongly accept it.
- `test_rollback_readback_mismatch_keeps_trial`: rollback write doesn't stick → state stays trial,
  reason "rollback failed". *Catches:* reporting rolled_back on a failed restore.
- FakeBridge: `config_default_is_str` not in MUTATING; protocol/op-table test updated; FakeBridge's
  `config_set` must model Hermes coercion using its string-default set so tests are realistic.

- [ ] RED → implement → GREEN (`cd tuner && uv run pytest -q -W error`) → mutation-check →
  commit `fix(tuner): model Hermes's per-key config coercion; verify config writes by read-back`.
