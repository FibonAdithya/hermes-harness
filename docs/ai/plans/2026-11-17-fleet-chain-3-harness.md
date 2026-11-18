# fleet:human pings, box chaind, and rollout Implementation Plan (3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The approvals bot announces every `fleet:human` issue once and takes `answer`/`drop`/`take` replies; the box runs chaind and deploys chain through the same gate as fleet; then the whole design is rolled out and accepted on `fleet-fixture`.

**Architecture:** Same shape as the triage flow: read-only and write box verbs over the forced `dispatch` command (`box/verbs/*`, logic in `box/tasklib.py`), and an announcer in the approvals listener with its own code store (`TaskStore` class, separate file). The escalation text is extracted by fleet's own `fleet.escalation.latest`, called on the box through `uv run --project ~/TIG/fleet` as `tasklib.fleet_areas` already does. chain deploys like fleet: `request_deploy(sha, "chain")`, `deploy chain <prefix>`, a `deploy_chain` tool, and a `chain` target in the box `deploy` verb.

**Tech Stack:** Python 3.12, pytest (box suite runs under the executor image's pytest 7.2.1 too), httpx, systemd user units, shellcheck.

**Spec:** `docs/ai/specs/2026-11-17-fleet-chain-coordination-design.md` (as merged with these plans). Sections 4 and 6, Constraints and Acceptance are this plan.

**Repo:** hermes-harness, branch `feat/fleet-chain-coordination` from `master`.

**Depends on:** plan 2 Task 3 (`fleet.escalation.latest`) is called at runtime only; the tests here stub it. Tasks 1-5 can be built before plans 1 and 2 merge. Task 6 (rollout) needs all three merged.

## Global Constraints

- Required checks on master are `broker` and `box`; new tests go in `broker/tests` and `box/tests`.
- Box test helpers must not be named `setup` (issue #49, pytest 7.2.1).
- No Telegram token or bot code on the box. The droplet calls the box; the box never calls out.
- Answers file: `~/.local/state/fleet/answers/<owner>__<name>/<number>.json`, a JSON list of `{"text": str, "at": float}`, written atomically, mode 600.
- An owner answer is 1 to 2000 characters after stripping; longer is refused in chat, nothing written.
- `fleet:human` issues that also carry `fleet:solo` are not announced (they are `run_task` work, `tasklib.MODES["solo"]`).
- Every new reply verb keeps the existing rules: owner id, private chat, not forwarded (`approvals.parse_command`).
- Codes: four digits, unique across grants, tasks, proposals and the new human store (`_avoid_codes`).

## Review Focus

1. An issue body or escalation text containing the reply separator or a fake `answer 1234 ...` line must not pass as reply instructions. (Task 3, `test_human_announcement_separator_appears_once`.)
2. A `drop <code>` reply must reach the store that issued the code; the same four digits cannot be live in two stores. (Task 3, `test_drop_routes_to_human_store` and `test_codes_unique_across_stores`.)
3. Answering an issue whose escalation changed after it was announced (a newer escalation, or it left `fleet:human`) must be refused, not answer the wrong question. (Task 1, `test_answer_refuses_changed_escalation`.)
4. `list_human` when fleet's escalation helper fails (fleet checkout broken, uv missing) must still list the issue with an "escalation text unavailable" note, not drop it silently. (Task 1, `test_human_lists_issue_when_escalation_unreadable`.)
5. Deploying chain while a chain run is mid-stage must not kill the stage (chaind's unit has `KillMode=process`). (Task 5, `test_box_chaind_unit_kill_mode_process`.)

---

### Task 1: tasklib: list, answer and take `fleet:human` issues

**Files:**
- Modify: `box/tasklib.py:23` (CLASSES adds `build`, `bug`), add the functions below
- Modify: `box/tests/fakegh.py` (comments, assignees, `api user`, add/remove assignee)
- Test: `box/tests/test_tasklib.py`

**Interfaces:**
- Produces:
  - `tasklib.ANSWERS_DIR = Path(".local/state/fleet/answers")` (relative to home).
  - `tasklib.fleet_escalations(home: Path, comments: dict[int, list[str]]) -> dict[int, dict | None]` — one `uv run --no-sync --project ~/TIG/fleet python -P -c` call, comments passed on stdin as JSON, calling `fleet.escalation.latest` for each issue; raises `TaskError` on failure.
  - `tasklib.human(gh: Gh, repo_slug: str, home: Path, escalations=fleet_escalations) -> list[dict]` — open `fleet:human` issues without `fleet:solo` and not assigned to the gh login; each row `{number, title, labels, escalation: {index, reason, text} | None, note: str | None, hash}`, where `hash = issue_hash(title, f"{index}\n{text}")` (index -1 and text "" when unreadable).
  - `tasklib.answer(gh, repo_slug, number, want_hash, text, home, now, escalations=fleet_escalations) -> dict` — refuses (`TaskError`) a closed issue, one not in `fleet:human`, a `fleet:solo` one, a hash mismatch, or text outside 1..2000 characters; otherwise appends to the answers file, comments `Owner's answer (from Telegram):\n\n<text>`, ensures and adds `fleet:answered-N` and `fleet:ready`, then removes `fleet:human` (add before remove, as `approve` does). Returns `{number, answers: N}`.
  - `tasklib.take(gh, repo_slug, number, want_hash, escalations=fleet_escalations) -> dict` — same freshness checks, then assigns the gh login.

- [ ] **Step 1: Write the failing tests**: `test_human_lists_escalated_issue`; `test_human_skips_solo`; `test_human_skips_taken` (assigned to the login); `test_human_lists_issue_when_escalation_unreadable` (stub raises → row with `note`, `escalation None`); `test_answer_writes_file_comment_and_labels` (labels after: `fleet:ready`, `fleet:answered-1`, no `fleet:human`; file holds one entry; FakeGh call order has the add before the remove); `test_second_answer_is_answered_2`; `test_answer_refuses_changed_escalation`; `test_answer_refuses_solo`; `test_answer_text_bounds` (0 and 2001 characters refused, nothing written); `test_take_assigns_login`; `test_classes_match_fleet` (`CLASSES` contains `build` and `bug`).
- [ ] **Step 2: Run** `cd box && uv run --with pytest python -m pytest tests/test_tasklib.py -q` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the box suite — expect PASS.
- [ ] **Step 5: Commit** `git add box/tasklib.py box/tests/fakegh.py box/tests/test_tasklib.py && git commit -m "box: list, answer and take fleet:human issues"`

### Task 2: Box verbs `list_human`, `answer_human`, `take_human`

**Files:**
- Create: `box/verbs/list_human` (ungated, read-only, shape like `box/verbs/list_triage`: `{"issues": [...], "repos_ok": [...], "errors": [...]}`, each issue with `repo` and `slug` added), `box/verbs/answer_human` (`{repo, number, hash, text}`), `box/verbs/take_human` (`{repo, number, hash}`); all executable
- Test: `box/tests/test_task_verbs.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: the three verbs, called by the approvals listener only (Task 3). Errors use `boxlib.refuse`.

- [ ] **Step 1: Write the failing tests** (the existing verb-test harness in `test_task_verbs.py`): `test_list_human_verb_shape`; `test_list_human_reports_repo_errors` (a target whose slug is bad → in `errors`, the others listed); `test_answer_human_verb`; `test_answer_human_refuses_bad_number`; `test_take_human_verb`; and the verb files are executable (`test_dispatch.py` style).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the box suite and `shellcheck -S warning box/*.sh` — expect PASS.
- [ ] **Step 5: Commit** `git add box/verbs/list_human box/verbs/answer_human box/verbs/take_human box/tests/test_task_verbs.py && git commit -m "box: list_human, answer_human and take_human verbs"`

### Task 3: Approvals listener: announce `fleet:human`, take `answer`/`drop`/`take`

**Files:**
- Modify: `broker/hermes_broker/approvals.py` — regexes beside `_TASK` (:33), `parse_command` (:46), `_dispatch` (:415), `_avoid_codes` (:284), `run_listener` (:465)
- Create: `broker/hermes_broker/human.py` (`HumanAnnouncer`, `format_human`, `handle_human`)
- Modify: `broker/hermes_broker/listen.py` (construct the store and pass it)
- Test: `broker/tests/test_human.py` (new), `broker/tests/test_approvals.py`

**Interfaces:**
- Consumes: Task 2's verbs; `TaskStore` (`broker/hermes_broker/tasks.py`) reused as the human store at `state_dir / "human_codes.json"`.
- Produces:
  - `parse_command` returns `("answer", f"{code}\n{text}")` for `^answer\s+(\d{4})\s+(.+)$` (DOTALL; text stripped) and `("take", code)` for `^take\s+(\d{4})$`. `drop` is unchanged in parsing.
  - `_dispatch` order for `drop`: proposals, then the human store when the code is live there, then tasks.
  - `format_human(issue: dict, code: str) -> str`: head `fleet:human  <repo>#<n>  <title>`, the escalation text (cut to `BODY_CHARS`, the separator removed from it), then `_TAIL_SEPARATOR` exactly once and the tail `code <c>\nanswer <c> <your text> | drop <c> | take <c>`.
  - `HumanAnnouncer(humans: TaskStore, grants: GrantStore, tasks: TaskStore | None, proposals: ProposalStore | None, call: Call, send: Callable[[str], bool])` with `tick(now)`, run every `ANNOUNCE_EVERY` from `run_listener`; one message per (issue, hash); prunes codes for issues that left the listing (only for repos in `repos_ok`); gives a code back when the send fails; reports the box unreachable once, like `Announcer.tick`.
  - `handle_human(humans: TaskStore, verb: str, arg: str, now: float, call: Call) -> str`: `answer` → `answer_human`; `drop` → `close_task`; `take` → `take_human`; the code is spent whatever happens; the reply names the issue and what the verb did, or its error. An answer over 2000 characters is refused before any call.

- [ ] **Step 1: Write the failing tests**: `test_parse_answer_multiline`; `test_parse_take`; `test_parse_answer_needs_text` (`answer 1234` alone → None); `test_forwarded_answer_ignored`; `test_human_announced_once`; `test_new_escalation_new_message`; `test_human_announcement_separator_appears_once` (escalation text containing `—— reply below ——` and `answer 9999 x`); `test_answer_reply_calls_answer_human` (payload has repo, number, hash, text); `test_answer_too_long_refused_without_call`; `test_drop_routes_to_human_store`; `test_take_reply_calls_take_human`; `test_codes_unique_across_stores` (a code live in tasks is never issued by the human store); `test_failed_send_gives_code_back`; `test_listener_survives_human_tick_error`.
- [ ] **Step 2: Run** `cd broker && uv run pytest tests/test_human.py tests/test_approvals.py -q` — expect FAIL.
- [ ] **Step 3: Implement.** Update the module docstring of approvals.py (:1-7) to list the new replies.
- [ ] **Step 4: Run** the broker suite — expect PASS.
- [ ] **Step 5: Commit** `git add broker/hermes_broker/approvals.py broker/hermes_broker/human.py broker/hermes_broker/listen.py broker/tests && git commit -m "approvals: announce fleet:human issues; answer, drop and take replies"`

### Task 4: chain as a deploy target

**Files:**
- Modify: `broker/hermes_broker/grants.py` (`DEPLOY_TARGETS` adds `"chain"`), `broker/hermes_broker/server.py:197-252` (`request_deploy` accepts `chain`; reply text `deploy chain <prefix>`; new tool `deploy_chain`), `broker/hermes_broker/approvals.py:32` (`_DEPLOY` accepts `harness|fleet|chain`)
- Modify: `box/verbs/deploy` (`TARGETS` adds `"chain"`; `deploy_chain(home, sha)`), `box/verbs/deployed` (adds `"chain": stamp("chain-deployed.sha")`)
- Test: `broker/tests/test_deploy.py`, `broker/tests/test_grants.py`, `box/tests/test_deploy.py`, `box/tests/test_deployed.py`

**Interfaces:**
- Produces: `deploy_chain(home, sha)` in `~/chain`: `git fetch origin`; refuse unless `sha == origin/main`; check out `sha`; `make setup` (chain's Makefile creates `.venv` with uv); run `make check` and, on failure, check out the previous stamp and refuse; write `~/.local/share/chain-deployed.sha`; `systemctl --user restart chaind`. Prints `{"deployed": sha, "target": "chain"}`.
- `deploy_chain()` MCP tool: takes the grant with `target="chain"`, calls the box `deploy` verb with `{"sha": sha, "target": "chain"}`, timeout 1800.

- [ ] **Step 1: Write the failing tests**: `test_request_deploy_chain` (reply text says `deploy chain <12 hex>`); `test_bare_deploy_never_approves_chain`; `test_deploy_chain_needs_grant`; box: `test_deploy_chain_refuses_non_main_sha`, `test_deploy_chain_keeps_previous_on_failed_check`, `test_deploy_chain_writes_stamp_and_restarts` (the fake runner records `systemctl --user restart chaind`), `test_deployed_reports_chain`.
- [ ] **Step 2: Run** both suites — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both suites — expect PASS.
- [ ] **Step 5: Commit** `git add broker box && git commit -m "deploy: chain as a gated deploy target"`

### Task 5: chaind on the box

**Files:**
- Create: `box/units/chaind.service`: `WorkingDirectory=%h/chain`, `ExecStart=%h/chain/.venv/bin/chaind`, `KillMode=process`, `Restart=on-failure`, `Environment=PATH=%h/.local/bin:%h/.npm-global/bin:/usr/local/bin:/usr/bin:/bin` (the dispatch PATH, `box/dispatch`), no `SSH_AUTH_SOCK`
- Create: `box/chain-config.example.toml` (no `[intake]`; stage budgets that sum to at most the fleet-fixture `[agents.chain] per_task_usd` of 40.0 — ESTIMATE (unverified); a comment saying so)
- Modify: `box/install.sh` (enable `chaind` only when `~/chain/.venv/bin/chaind` exists; print a line saying how to set it up otherwise), `box/provision.sh` (clone `FibonAdithya/chain` to `~/chain` if absent, `make setup`, copy the example config to `~/.config/chain/config.toml` if absent)
- Test: `box/tests/test_units.py`

**Interfaces:**
- Produces: a box where `systemctl --user is-active chaind` is `active` and `~/chain/.venv/bin/chain status --json` works for the box user.

- [ ] **Step 1: Write the failing tests**: `test_box_chaind_unit_kill_mode_process`; `test_box_chaind_unit_path_matches_dispatch`; `test_chain_example_config_has_no_intake` (parse with tomllib); `test_chain_example_budgets_fit_ceiling` (sum of stage `budget_usd` ≤ 40.0).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the box suite and shellcheck — expect PASS.
- [ ] **Step 5: Commit** `git add box/units/chaind.service box/chain-config.example.toml box/install.sh box/provision.sh box/tests/test_units.py && git commit -m "box: run chaind for fleet's chain route"`

### Task 6: Runbook, PR, and rollout with acceptance (owner-attended)

**Files:**
- Modify: `runbooks/dispatch.md` (sections "fleet:human pings" and "The chain route on the box"), `README.md` (one line each)

- [ ] **Step 1: Write the runbook sections**: the reply verbs, what each does to the issue, where answers live, how to deploy chain, and the rollout below.
- [ ] **Step 2: PR.** Push, open the PR with the claim table, run `review-pr`, `gh pr checks <n> --watch` with output pasted. The owner merges. Merge order across repos: chain (plan 1), fleet (plan 2), this one.
- [ ] **Step 3: Measure on the box before enabling** (record each as MEASURED with the command):
  - `loginctl show-user <box user> -p Linger` → `Linger=yes`, or the owner enables it.
  - one `claude -p` in auto mode with an `--allowedTools` list, headless, as the box user, exits 0.
  - which Claude account the box login uses compared with the laptop's; record whether they share a usage limit.
  - the box user can push a branch to `FibonAdithya/fleet-fixture` (`git push --dry-run`).
- [ ] **Step 4: Box fleet checkout.** The owner switches `~/TIG/fleet` on the box to the public history (`git fetch && git reset --hard origin/main` against `FibonAdithya/fleet`).
- [ ] **Step 5: Deploy through the gate**, in order: harness (`deploy <prefix>`), then provision chain on the box (Task 5's provision step) and `deploy chain <prefix>`, then `deploy fleet <prefix>`. Check `deployed` reports all three stamps.
- [ ] **Step 6: fleet-fixture policy.** A PR on `FibonAdithya/fleet-fixture` adding: `build`/`bug` routes and the `chain` Role row in `docs/agent/task-classes.md`, `.agents/roles/chain.md`, `chain` in the handoff schema's role enum, `[agents.chain] command = "~/chain/.venv/bin/chain"` and `per_task_usd = 40.0` (ESTIMATE (unverified)), `[chain] default_area`. The owner merges it into `main` and `integration`.
- [ ] **Step 7: Acceptance**, spec items 1-7, on fleet-fixture, each with its issue number and the observed labels. Record MEASURED: chain spend per task, owner messages per merged change, minutes from escalation to ping.
- [ ] **Step 8: Hand back.** Anything still running (a fleet night, a chain run) gets one `remind-owner` ping at its expected finish; open decisions go to `hermes-wiki todo add`. Update the wiki pages `fleet.md` and `chain.md`.
