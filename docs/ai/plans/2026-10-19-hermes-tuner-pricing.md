# Hermes Tuner — real model pricing (amendment)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use
> checkbox syntax.

**Goal:** Replace the flat `USD_PER_MTOK = 0.42` estimate with the configured model's own price
list (per-million input / cache-read / cache-write / output), looked up through Hermes, and price
every run from its token breakdown.

**Why:** owner request 2026-10-19 ("pull the llm costs from the model we're using and the pricing
page"). For `deepseek-v4-pro` the list is input $0.435, cache read $0.003625, output $0.87 per
million (Hermes snapshot of https://api-docs.deepseek.com/quick_start/pricing, version
`deepseek-pricing-2026-07`, `agent/usage_pricing.py:491-500` in the pinned source). Cache reads are
120× cheaper than input and are most prompt tokens, so a flat rate on `prompt_tokens` misprices runs
badly and distorts the detector's ranking.

**Kept from the final-review ruling (F1):** one price list applied to ALL history (old and new runs
alike), so comparisons stay independent of billing changes. Hermes's per-session
`estimated_cost_usd` stays unused for decisions.

**Builds on:** branch `docs/hermes-tuner-design` at 58e0d3f. Spec
`docs/ai/specs/2026-10-19-hermes-tuner-design.md`; main plan `docs/ai/plans/2026-10-19-hermes-tuner.md`.
Pinned Hermes source (read-only):
`/tmp/claude-1000/-home-fibonadithya-hermes-harness/dedb1af4-e258-4666-8f7a-15c7e5722e35/scratchpad/hermes-agent-src`.

## Global constraints
- Hermes source is never modified; Hermes is reached only via `bridge_entry.py`.
- No HTML scraping of pricing pages: the price list comes from Hermes's `get_pricing_entry`
  (provider `/models` API, else Hermes's dated docs snapshot of the vendor's pricing page).
- No mail-derived data in the repo. Every dollar figure shown is labelled ESTIMATE and names the
  price source.
- Pricing unavailable ⇒ fail closed for cost-dependent decisions (see Task 2), never a silent $0.

---

### Task 1: Token breakdown and price list primitives

**Files:** `tuner/hermes_tuner/model.py`, `history.py`, `scrub.py`, `measure.py`, `policy.py`,
`bridge.py`, `bridge_entry.py`, tests (`tests/factory.py`, `test_history.py`, `test_scrub.py`,
`test_measure.py`, `test_ledger.py` for bridge protocol/FakeBridge).

**Interfaces:**
- `Session` gains `input_tokens: int`, `cache_read_tokens: int`, `cache_write_tokens: int`
  (fields added with defaults of 0 AFTER existing fields so existing constructors still work);
  `prompt_tokens` stays and must equal `input + cache_read + cache_write` when built by History
  (Hermes `CanonicalUsage.prompt_tokens`, usage_pricing.py:41-43). History sums each field over a
  compression chain (NULL → 0).
  Verified in the audit: `sessions.input_tokens` is FRESH input only (Hermes writes
  `canonical_usage.input_tokens`, agent/conversation_loop.py:3249). Today the tuner builds
  `prompt_tokens = input + cache_read` and omits cache writes; fix it to
  `input + cache_read + cache_write`.
- `scrub` dumps/loads the three new fields and REQUIRES them on load (KeyError → ValueError
  "fixture predates token breakdown"). No fixture has been committed yet, so no back-compat path:
  silently loading zeros would make every run look nearly free.
- `model.Pricing` (frozen dataclass): `model: str`, `input_per_m: float`, `output_per_m: float`,
  `cache_read_per_m: float | None`, `cache_write_per_m: float | None`, `source: str`,
  `source_url: str | None`, `version: str | None`, `fetched_at: float`. `Pricing.from_dict/to_dict`.
- `measure.priced_cost(session, pricing) -> float` =
  `(input × input_per_m + cache_read × (cache_read_per_m if not None else input_per_m)
  + cache_write × (cache_write_per_m if not None else input_per_m) + output × output_per_m) / 1e6`.
  `JobStats` keeps `median_priced_cost` (now from pricing) and `median_prompt_tokens`;
  `job_stats/chat_stats(sessions, ..., pricing)`; `weekly_spend` removed (unused per final review).
- `policy.USD_PER_MTOK` removed. `policy.PRICING_MAX_AGE_S = 7 * 86400`.
- `Bridge.pricing() -> dict` (Protocol, HermesBridge op `pricing`, FakeBridge with a settable
  price dict; not mutating). `bridge_entry` op `pricing`: read model/provider/base_url exactly as
  `oneshot` does; lookup order:
  1. `get_pricing_entry(model, provider=provider, base_url=base_url, api_key=key)` where `key` is
     the provider credential resolved the way Hermes's auxiliary client resolves it for the main
     provider (find the resolver in agent/auxiliary_client.py; best effort — if it can't be
     resolved use "" and let step 2 handle a failed or empty lookup);
  2. if that returns None, or an entry whose input AND output are both None/0 (includes Hermes's
     `included-route` entries), retry with the model's vendor: if `model` contains "/", vendor =
     the part before "/", bare = the part after; else vendor from the provider name only if it is
     a known docs-snapshot vendor; call `get_pricing_entry(bare, provider=vendor)`;
  3. still nothing usable → exit non-zero with JSON error `no pricing for <model>`.
  Output: the Pricing dict (Decimals → float) with `source`, `source_url`, `pricing_version`.
  `oneshot` additionally returns `input_tokens`, `cache_read_tokens`, `cache_write_tokens`,
  `output_tokens` separately (from `normalize_usage`), keeping `input_tokens` meaning Hermes's
  `input_tokens` (fresh input), not prompt total.

**Tests (each names the mutation it catches):**
- `test_priced_cost_uses_cache_rate`: 1,000,000 cache-read tokens at the v4-pro list → $0.003625,
  not $0.435. *Catches:* cache reads priced at the input rate.
- `test_priced_cost_falls_back_to_input_rate_when_cache_rate_missing`: cache_read_per_m None →
  priced at input rate. *Catches:* None treated as 0 (free cache reads).
- `test_priced_cost_output_rate`: 1M output → $0.87. *Catches:* output priced at input rate.
- `test_history_keeps_token_breakdown_and_chain_sums`: factory rows with distinct input/cache
  read/cache write per chain member → summed per field on the merged Session; prompt_tokens equals
  Hermes's definition. *Catches:* fields dropped or not summed across compression.
- `test_scrub_round_trips_breakdown`, and a dump missing the new fields raises ValueError.
  *Catches:* scrub dropping the fields; silently loading zeros.
- `test_bridge_entry_pricing_lookup_order` — unit-test the lookup function in bridge_entry with
  injected stand-ins for `get_pricing_entry` (no Hermes import): route entry used when usable;
  vendor retry when route returns None or an all-zero entry; error when both fail. *Catches:*
  accepting a $0 included-route entry; skipping the vendor fallback.
- FakeBridge `pricing` not in MUTATING; protocol/op-table test updated.

- [ ] Step 1: failing tests, RED captured. - [ ] Step 2: implement. - [ ] Step 3: GREEN + full
  suite + mutation-check. - [ ] Step 4: commit `feat(tuner): price runs from the model's list
  prices and token breakdown`.

### Task 2: Wire pricing through the cycle, CLI and reports

**Files:** `cycle.py`, `cycle_draft.py`, `budget.py`, `detect.py`, `draft.py`, `trial.py`,
`__main__.py`, `runbooks/tuner.md`, tests (`test_cycle.py`, `test_detect.py`, `test_draft.py`,
`test_trial.py`, `test_answer_key.py`, `test_budget` if present).

**Behaviour:**
- Each non-dry run calls `bridge.pricing()` once at the start. Success → store under
  `cycle_state["pricing"]` (Pricing.to_dict, `fetched_at = now`). BridgeError → use the stored
  pricing if `now − fetched_at ≤ PRICING_MAX_AGE_S` (report error "pricing lookup failed; using
  cached list from <date>"); otherwise NO pricing this run.
- No pricing ⇒ the run still does cleanup, shadow evaluation of quality, approved proposals,
  rollbacks requested by deadlines, restart and the weekly line (with "pricing unavailable" instead
  of dollar figures); it SKIPS drafting and SKIPS cost-based trial verdicts (records stay `trial`,
  "continue"), and reports error `no model pricing`. Dry run: calls `bridge.pricing()` (it is not
  mutating) and prints it in the report; failure is reported, nothing else changes.
- `detect(sessions, now, pricing, skill_names=None)`; candidates worth ≤ 0 dropped (unchanged).
- `draft._spend(reply, pricing)` prices the oneshot from its token breakdown.
- Budget spend, 28-day pause, savings, baseline_cost, `_global_verdict`, `trial_verdict` all use
  `priced_cost(·, pricing)` with the SAME Pricing object for both sides of every comparison.
  `trial_verdict`'s prompt-token fallback is kept only for the both-zero case.
- A pricing CHANGE between runs (new version or new model) must never compare a baseline priced
  under one list with runs priced under another. The final-review F7 fix stopped widening the
  history window for kept/applied records, so the baseline window may no longer be loaded at
  judgement or savings time. Therefore: when a fix goes live, store on the record
  `baseline_runs: list[[input, cache_read, cache_write, output]]` — the token breakdowns of that
  job's runs (≥1 tool call) in [applied_at − 28 d, applied_at), at most the 60 most recent (state
  the cap in the docstring). Global (config/skill) fixes store the same for the chat +
  editable-job runs they compare against. Every verdict and the weekly saving compute the baseline
  as the median `priced_cost` of `baseline_runs` under the CURRENT pricing. `baseline_cost` stays
  only as a display value written at go-live. Ledger load of older records without `baseline_runs`
  → those records are judged "continue" until the 21-day deadline (no tuner ledger exists on the
  droplet yet, so this path only guards hand-edited files).
- Weekly line: `Hermes tuner: saved ~$X.XX this week (ESTIMATE at <model> list prices,
  <pricing_version>), N applied, K kept, R rolled back; tuner spent $S.SS` (+ errors suffix as now).
- CLI: `detect --db PATH [--fixture PATH] [--now YYYY-MM-DD] [--pricing JSON_FILE]`; without
  `--pricing`, it calls `HermesBridge().pricing()`; `--db` no longer required with `--fixture`.
  `run` unchanged. `pricing` subcommand prints the resolved price list (for the droplet smoke).
- `test_answer_key.py` uses a fixed Pricing equal to the v4-pro snapshot values above.
- Runbook: where prices come from, how to see them (`python -m hermes_tuner pricing`), what
  happens when unavailable, and that figures are list-price ESTIMATES, not invoices.

**Tests (each names the mutation it catches):**
- `test_detector_ranks_by_list_price`: two candidates with equal result chars; candidate A's runs
  have 3× the total prompt tokens of B's but almost all are cache reads; B's are fresh input → B
  ranks above A. Under a flat per-token rate A would rank above B, so the test cannot pass by a
  tie or alphabetical order. *Catches:* flat-rate pricing.
- `test_no_pricing_skips_drafting_and_verdicts_but_runs_safety_steps`: FakeBridge.pricing raises,
  no cached list → zero oneshot calls, trial record stays `trial`, a due 21-day deadline rollback
  still happens, weekly line says "pricing unavailable". *Catches:* drafting on $0; freezing safety.
- `test_cached_pricing_used_within_max_age` and `..._not_after_max_age` (boundary at exactly
  PRICING_MAX_AGE_S → used; +1 s → not). *Catches:* off-by-one; stale lists forever.
- `test_pricing_change_rebases_baseline`: fix goes live under list A (baseline_runs stored); the
  trial runs use exactly the baseline's token counts; pricing then switches to list B with HALVED
  rates. Correct (B on both sides) → equal cost → rollback. Buggy (baseline priced under A, runs
  under B) → runs look 50% cheaper → keep. Assert rollback. *Catches:* comparing across price
  lists. (Doubled rates would NOT discriminate: the bug would also yield rollback.)
- `test_savings_use_stored_baseline_runs_without_history`: a kept fix whose baseline window is
  outside the loaded history → weekly saving still computed from `baseline_runs`. *Catches:*
  savings depending on the F7-trimmed window.
- `test_weekly_line_names_price_source`. *Catches:* unlabelled dollar figures.
- `test_draft_spend_priced_from_breakdown`. *Catches:* oneshot priced at a flat rate.
- `test_no_result_text_reaches_drafting_model` still passes (unchanged guarantee).

- [ ] Step 1: failing tests, RED. - [ ] Step 2: implement. - [ ] Step 3: GREEN + full suites
  (tuner `-W error`, broker) + shellcheck + mutation-check. - [ ] Step 4: commit
  `feat(tuner): cycle, detector and weekly line use the model's list prices`.

## Execution note
Tasks 1 and 2 go to ONE implementer in sequence (two commits): Task 1 changes the meaning of the
oneshot `input_tokens` field and `priced_cost`'s signature, and Task 2 updates every caller; a
reviewer sees both together. Between the commits the suite must still pass.

## Not in this plan
- Live-scraping pricing web pages. If Hermes's snapshot is stale, updating Hermes updates it.
- Fixing the parked M2 string-key refusal (separate small change).
