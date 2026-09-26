# Self-healing fleet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hermes and an automatic detector file fleet tasks as triage issues, the owner approves each one from Telegram, and fleet (or the `run_task` executor) runs them, including against the `fleet` repository itself.

**Architecture:** Four new box verbs (`file_task`, `list_triage`, `approve_task`, `close_task`) share `box/tasklib.py` and are the only code that touches the fleet ledger. The broker's approvals listener gains an announcer loop and three reply verbs (`task`/`solo`/`drop`) backed by `broker/hermes_broker/tasks.py`. A deterministic `box/doctor.py` run hourly files fleet faults. The `deploy` verb gains a `fleet` target. Two PRs to `FibonAdithya/fleet` make areas come from the target repo and give fleet its own policy layer.

**Tech Stack:** Python 3.11+ (broker, uv + pytest), Python 3.12 stdlib (box verbs, pytest in CI), `gh` CLI on the box, systemd user units, fleet (Python 3.11, uv, `make check`).

**Spec:** `docs/ai/specs/2026-09-26-fleet-self-healing-design.md`. Read it before any task.

## Global Constraints

- Box code is stdlib only. CI runs `cd box && python -m pytest -q` on Python 3.12 with only pytest installed. The laptop's system `python3` is 3.10 with no pytest (and no `tomllib`), so every box test command below uses `uv run --no-project --python 3.12 --with pytest`, which is the same interpreter and dependency set as CI (MEASURED 2026-09-26: 78 passed at `f66ad58`).
- Every file in `box/verbs/` is executable and starts with `#!/usr/bin/env python3`; CI `py_compile`s it.
- Box verbs read one JSON object on stdin (`boxlib.read_json_stdin`) and print one JSON object; a refusal is `sys.exit(boxlib.refuse(msg))` (exit 2, `{"error": msg}`).
- Broker tests: `cd broker && uv run --extra dev pytest -q`. Box tests: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest -q`. fleet: `make check` in `~/TIG/fleet`.
- Hermes (MCP tools) must never be able to reach `approve_task`, `close_task`, or any reply verb. Only the approvals listener calls them.
- Approval messages are honoured only from the owner's numeric id, not forwarded, exact match — the existing `parse_command` rules.
- `approve NNNN` stays grant approval. Task codes and grant codes are never live in both stores at once.
- Announced body is trimmed to 1,500 characters; codes last 7 days; announcer interval 300 s; doctor files at most 3 new issues per run; evidence at most 60 lines.
- `issue_hash = sha256(title + "\n" + body)`, hex.
- `<sig>` = first 8 hex chars of `sha256(f"{kind}|{key}|{fleet_sha}")`.
- fleet's `fleet.toml`: `slots = 3`, `per_task_usd = 2.0`, `fleet_usd = 10.0`, integration branch `integration`.
- fleet default branch is `main`; hermes-harness default branch is `master`.
- Git: stage explicit paths only; never `git add -A` / `.`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Nothing is deployed by this plan's code tasks. Task 9 is the deployment and is owner-attended.

## Review Focus

1. **Ops patterns are guesses.** The doctor's login/disk/herdr regexes were written from memory, not captured output. A real expired-login message that doesn't match turns an ops problem into an agent task that burns attempts. Task 7 Step 1 captures real messages and the tests use them.
2. **Oversized or odd issue text.** A 50 KB body, or one full of Markdown, must not break the Telegram send (4,096-char limit) or the hash. Task 5 pins the announcement at ≤ 4,000 characters and hashes the untrimmed text.
3. **Partial `list_triage` failure.** One repo's `gh` call failing must not prune that repo's outstanding codes, which would re-announce every one of its issues on the next tick. Task 5 tests prune scoping.
4. **Doctor on a night with no log or no run log.** A night killed before writing anything must produce an S1/S2 finding, not a crash that marks nothing `doctored` and retries forever. Task 7 tests both.
5. **Listener restart.** Codes announced before a restart must still work after it; `tasks.json` persists. Task 4 tests a fresh `TaskStore` instance.

---

## Part A — `FibonAdithya/fleet`

Work in `~/TIG/fleet` on branch `feat/areas-from-policy` (Task 1, off `origin/main`) and `feat/self-policy` (Task 2), each its own PR. Task 2 needs Task 1's `area_names` and non-fixture areas, so until Task 1's PR is merged, `feat/self-policy` is cut from `feat/areas-from-policy` and its PR to `main` says it is stacked on Task 1's PR; once Task 1 merges, rebase it onto `origin/main`. Neither PR is merged by an agent. fleet's `AGENTS.md` says a label-vocabulary change needs a human; each PR body states that the owner approved it in `hermes-harness/docs/ai/specs/2026-09-26-fleet-self-healing-design.md` §4.

### Task 1: Areas come from the target repo's `ownership.md`

**Files:**
- Modify: `src/fleet/labels.py` (lines 6, 24-28, 59-80)
- Modify: `src/fleet/policy.py` (`parse_ownership`, new `area_names`)
- Modify: `src/fleet/model.py` (`Policy`, new property)
- Modify: `src/fleet/ledger.py` (`_one_of`, `read_one`, `read`)
- Modify: `src/fleet/reader.py` (`read_ledger`)
- Modify: `src/fleet/cli.py` (line 101 in the plan helper; `discover` at 238-249)
- Modify: `src/fleet/daemon.py:168`
- Modify: `src/fleet/gh.py:229-248` (`FakeGh.__init__`)
- Test: `tests/test_policy.py`, `tests/test_ledger.py`, `tests/test_gh.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `labels.AREA_RE`; `labels.static_vocabulary(max_attempts, max_fix_passes, max_rebases, areas=AREAS)`; `policy.area_names(cfg: FleetConfig) -> frozenset[str]`; `Policy.area_names -> frozenset[str]`; `ledger.read_one(row, packet=None, verdict=None, areas=AREAS)`; `ledger.read(rows, packets=None, verdicts=None, areas=AREAS)`; `reader.read_ledger(gh, areas=AREAS)`; `FakeGh(..., areas=AREAS)`.
- Task 3 runs `uv run --project ~/TIG/fleet python -c "import json; from fleet.config import load_config; from fleet.policy import area_names; print(json.dumps(sorted(area_names(load_config()))))"` with the target repo as cwd. That one-liner must work after this task.

- [ ] **Step 1: Baseline**

Run: `cd ~/TIG/fleet && git fetch -q origin && git switch -c feat/areas-from-policy origin/main && make check 2>&1 | tail -3`
Expected: green. Record the pytest pass count; it is the baseline for Step 8.

- [ ] **Step 2: Write the failing tests**

In `tests/test_policy.py`, replace `test_an_unknown_area_in_the_ownership_map_raises` (the AREAS membership check it guards is being removed on purpose) with:

```python
def test_a_malformed_area_name_in_the_ownership_map_raises():
    """Areas now come from this table, so the only check left is that the name
    can be a label. Catches dropping it: "Not An Area" would become the label
    `area:Not An Area`, which gh accepts and no task ever carries."""
    with pytest.raises(PolicyError, match="invalid area name"):
        parse_ownership("| Glob | Area |\n|---|---|\n| `src/x/**` | Not An Area |\n")


def test_areas_outside_the_fixture_vocabulary_are_accepted():
    """The fleet repository's own areas (allocator, ledger, ...) are not the
    fixture's. Catches keeping the old membership check."""
    pairs = parse_ownership(
        "| Glob | Area |\n|---|---|\n| `src/fleet/decide.py` | allocator |\n| `README.md` | docs |\n"
    )
    assert {area for _, area in pairs} == {"allocator", "docs"}


def test_area_names_reads_only_the_ownership_map(cfg):
    """`area_names` must work from ownership.md alone: the box's file_task verb
    calls it in a target repo, and the fixture cfg has no task-classes or roles.
    Catches implementing it as `load(cfg).area_names`."""
    cfg.ownership.write_text("| Glob | Area |\n|---|---|\n| `a/**` | alpha |\n| `b/**` | beta |\n| `c/**` | alpha |\n")
    assert area_names(cfg) == frozenset({"alpha", "beta"})
```

Add `area_names` to the `from fleet.policy import ...` line at the top of `tests/test_policy.py`, and `cfg` comes from `tests/conftest.py`.

In `tests/test_ledger.py`, after `test_an_unknown_area_label_is_rejected_loudly`, add:

```python
def test_an_area_is_checked_against_the_areas_passed_in():
    """Catches read_one ignoring its `areas` argument and still using the
    fixture tuple: `allocator` is valid for the fleet repo and unknown to the
    fixture."""
    assert ledger.read_one(row(labels=["area:allocator"]), areas=frozenset({"allocator"})).area == "allocator"
    with pytest.raises(ledger.LedgerError, match="unknown area"):
        ledger.read_one(row(labels=["area:eval"]), areas=frozenset({"allocator"}))
```

In `tests/test_gh.py`, after the test at line 343 that checks `static_vocabulary(max_attempts=3)`, add:

```python
def test_the_vocabulary_carries_the_areas_it_is_given():
    """Catches static_vocabulary hardcoding the fixture areas: FakeGh would then
    refuse `area:allocator` while real gh, seeded from the fleet repo's policy,
    accepts it -- a fake more permissive in one direction and stricter in the other."""
    vocab = static_vocabulary(areas=("allocator",))
    assert "area:allocator" in vocab and "area:eval" not in vocab
    gh = FakeGh(areas=("allocator",))
    assert "area:allocator" in gh.vocabulary
```

In `tests/test_cli.py`, add:

```python
def test_discover_refuses_an_area_the_target_repo_does_not_have(env, cfg):
    """The --area choice used to be the fixture tuple; now it is the target's
    ownership map, checked before anything is written. Catches validating
    against labels.AREAS, or not at all."""
    cfg.ownership.write_text("| Glob | Area |\n|---|---|\n| `src/**` | allocator |\n")
    env.setenv("FLEET_AGENT", "fleet-3")
    result = CliRunner().invoke(main, ["discover", "--from", "1", "--title", "t", "--area", "eval", "--evidence", "x:1"])
    assert result.exit_code == 2
    assert "not an area" in result.output and "allocator" in result.output
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd ~/TIG/fleet && uv run pytest tests/test_policy.py tests/test_ledger.py tests/test_gh.py tests/test_cli.py -q 2>&1 | tail -15`
Expected: the five new tests fail (ImportError for `area_names`, TypeError for the `areas` kwargs, unknown-area PolicyError for `allocator`, click Choice error text for the CLI test).

- [ ] **Step 4: Implement**

`src/fleet/labels.py` — keep `AREAS` as the no-policy default and add the name check:

```python
import re

# The fixture's areas: the default for code that has no policy to read. A target
# repository's real areas are the Area column of its ownership.md
# (policy.area_names); nothing validates against this tuple when a policy exists.
AREAS = ("eval", "train", "models", "data", "configs", "docs")
# What an area name must look like to be a label gh and humans can both type.
AREA_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
```

```python
def area_label(name: str) -> str:
    if not AREA_RE.fullmatch(name):
        raise ValueError(f"invalid area: {name}")
    return f"area:{name}"
```

In `static_vocabulary`, add a final parameter `areas: Collection[str] = AREAS` (import `Collection` from `collections.abc`) and change `*(f"area:{a}" for a in AREAS),` to `*(f"area:{a}" for a in areas),`.

`src/fleet/policy.py` — in `parse_ownership`, replace the membership check:

```python
        if not labels_module.AREA_RE.fullmatch(area):
            raise PolicyError(f"invalid area name {area!r} in ownership map")
```

and add after `parse_ownership`:

```python
def area_names(cfg: FleetConfig) -> frozenset[str]:
    """The target repository's areas: the Area column of its ownership map.

    Reads ownership.md alone, so a caller that only needs the vocabulary (the
    CLI's --area check, the box's file_task verb) does not need task-classes or
    role prompts to exist."""
    return frozenset(area for _, area in parse_ownership(cfg.ownership.read_text()))
```

`src/fleet/model.py` — inside `class Policy`, after `agent_for`:

```python
    @property
    def area_names(self) -> frozenset[str]:
        return frozenset(area for _, area in self.areas)
```

`src/fleet/ledger.py` — change `_one_of`'s `allowed` annotation to `Collection[str]` (import from `collections.abc`); `read_one` gains `areas: Collection[str] = labels_module.AREAS` as its last parameter and passes it: `area=_one_of(names, "area:", areas, "area"),`. `read` gains `areas: Collection[str] = labels_module.AREAS` and calls `read_one(row, packets.get(row["number"]), verdicts.get(row["number"]), areas)`.

`src/fleet/reader.py` — `def read_ledger(gh: GhClient, areas: Collection[str] = labels_module.AREAS) -> list[Task]:`; pass `areas` to `ledger_module.read_one(row, areas=areas)` and `ledger_module.read(rows, packets, areas=areas)`. Import `labels as labels_module` and `Collection`.

`src/fleet/daemon.py:168` — `ledger = read_ledger(self.gh, self.policy.area_names)`.

`src/fleet/cli.py:101` — load the policy once and use it for both:

```python
    policy = policy_module.load(cfg)
    tasks = read_ledger(gh, policy.area_names)
    intents, _ = decide(FleetState(), tasks, policy, [], now)
```

`src/fleet/cli.py` `discover` — change the option to `@click.option("--area", required=True, help="An area from the target repository's ownership map.")` and, after `cfg, gh, _ = _context(role_required=False)`:

```python
    known = policy_module.area_names(cfg)
    if area not in known:
        raise click.BadParameter(
            f"{area!r} is not an area in {cfg.ownership}; choose from {', '.join(sorted(known))}",
            param_hint="--area",
        )
```

`src/fleet/gh.py` `FakeGh.__init__` — add `areas: Collection[str] = labels_module.AREAS,` after `known_logins`, and pass it: `labels_module.static_vocabulary(max_attempts, max_fix_passes, max_rebases, areas)`.

- [ ] **Step 5: Run the new tests**

Run: `cd ~/TIG/fleet && uv run pytest tests/test_policy.py tests/test_ledger.py tests/test_gh.py tests/test_cli.py -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 6: Mutation-check**

For each, apply, run the named test, confirm FAIL, revert:
- `ledger.read_one`: pass `labels_module.AREAS` instead of `areas` → `test_an_area_is_checked_against_the_areas_passed_in` fails.
- `policy.area_names`: return `load(cfg).area_names` → `test_area_names_reads_only_the_ownership_map` fails.
- `static_vocabulary`: iterate `AREAS` instead of `areas` → `test_the_vocabulary_carries_the_areas_it_is_given` fails.
- `cli.discover`: delete the `known` check → `test_discover_refuses_an_area_the_target_repo_does_not_have` fails (exit code 1 from `do_discover`'s `get_issue`, not 2).
- `parse_ownership`: delete the `AREA_RE` check → `test_a_malformed_area_name_in_the_ownership_map_raises` fails.

- [ ] **Step 7: The vendored fixture still yields its six areas**

Run: `cd ~/TIG/fleet && uv run python -c "from fleet.policy import parse_ownership; from pathlib import Path; print(sorted({a for _, a in parse_ownership(Path('fixtures/policy/fleet-fixture/docs/agent/ownership.md').read_text())}))"`
Expected: `['configs', 'data', 'docs', 'eval', 'models', 'train']`

- [ ] **Step 8: Full gate**

Run: `cd ~/TIG/fleet && make check 2>&1 | tail -5`
Expected: green; pass count = baseline + 5 (test_policy: one test replaced by three; one new test each in test_ledger, test_gh, test_cli).

- [ ] **Step 9: Commit, push, PR**

```bash
cd ~/TIG/fleet
git add src/fleet/labels.py src/fleet/policy.py src/fleet/model.py src/fleet/ledger.py src/fleet/reader.py src/fleet/cli.py src/fleet/daemon.py src/fleet/gh.py tests/test_policy.py tests/test_ledger.py tests/test_gh.py tests/test_cli.py
git status --short
git commit -m "feat(policy): areas come from the target repo's ownership map

The area vocabulary was the fixture's six names, hardcoded, so fleet could not
target a repository with different areas -- including itself. Areas are now the
Area column of the target's ownership.md; labels.AREAS stays as the default for
code with no policy. A label-vocabulary change needs a human per AGENTS.md: the
owner approved this one in hermes-harness docs/ai/specs/2026-09-26-fleet-self-healing-design.md §4.1.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/areas-from-policy
gh pr create --repo FibonAdithya/fleet --base main --title "feat(policy): areas come from the target repo's ownership map" --body "See commit message. Owner approval of the vocabulary change: hermes-harness spec 2026-09-26 §4.1.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Then wait for `gh pr checks <n> --watch` to report, and stop for the owner to merge. Task 2 starts from the merged `main`.

### Task 2: fleet's own policy layer

**Files (all in `~/TIG/fleet`):**
- Create: `fleet.toml`
- Create: `docs/agent/ownership.md`
- Create: `docs/agent/task-classes.md`
- Create: `.agents/roles/*.md`, `.agents/schemas/*.json` (copied)
- Modify: `.gitignore` (add `.fleet/`)
- Test: `tests/test_self_policy.py`

**Interfaces:**
- Consumes: Task 1's `policy.area_names`, `policy.load`.
- Produces: `~/TIG/fleet` is a fleet target; `FibonAdithya/fleet` has the full label vocabulary for areas `allocator, ledger, cost, executor, daemon, cli, docs`. Task 7's doctor files there with areas `daemon`, `cost`, `executor`.

- [ ] **Step 1: Baseline, and check the parent-walk concern**

Run: `cd ~/TIG/fleet && git fetch -q && git switch -c feat/self-policy feat/areas-from-policy && make check 2>&1 | tail -3` (use `origin/main` as the start point instead if Task 1's PR is already merged)
Expected: green; record the count.

- [ ] **Step 2: Write the failing test**

`tests/test_self_policy.py`:

```python
"""This repository is also a fleet target. Its policy layer must load, and its
areas must cover every source file, or a task in an uncovered file is excluded
from nothing."""

from __future__ import annotations

from pathlib import Path

from fleet.config import load_config
from fleet.policy import load

ROOT = Path(__file__).resolve().parents[1]


def test_the_repository_policy_loads():
    policy = load(load_config(ROOT))
    assert policy.area_names == frozenset({"allocator", "ledger", "cost", "executor", "daemon", "cli", "docs"})
    assert policy.slots == 3
    assert policy.fleet_usd == 10.0


def test_every_source_file_has_an_area_or_is_non_dispatchable():
    """Catches adding a module to src/fleet without mapping it. The
    non-dispatchable files are listed here on purpose: a new one has to be
    added deliberately, in two places."""
    non_dispatchable = {"labels.py", "packet.py", "commands.py", "schemas.py", "__init__.py"}
    policy = load(load_config(ROOT))
    globs = [glob for glob, _ in policy.areas]
    for path in sorted((ROOT / "src" / "fleet").glob("*.py")):
        if path.name in non_dispatchable:
            continue
        rel = path.relative_to(ROOT).as_posix()
        assert any(Path(rel).match(g) for g in globs), f"{rel} has no area"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd ~/TIG/fleet && uv run pytest tests/test_self_policy.py -q 2>&1 | tail -5`
Expected: FAIL, `ConfigNotFound` or missing `fleet.toml`.

- [ ] **Step 4: Create the policy layer**

`fleet.toml` — the vendored fixture's, with this repository's values:

```toml
[repo]
slug = "FibonAdithya/fleet"
integration_branch = "integration"

[policy]
ownership = "docs/agent/ownership.md"
task_classes = "docs/agent/task-classes.md"
roles = ".agents/roles"
schemas = ".agents/schemas"

[budget]
per_task_usd = 2.0
fleet_usd = 10.0

[fleet]
slots = 3
stall_minutes = 10
max_attempts = 2
max_fix_passes = 1

[agents.claude]
model_flag = "--model"
args = ["--permission-mode", "acceptEdits"]

# First-party API rates, USD per million tokens, read from
# platform.claude.com/docs/en/about-claude/pricing on 2026-09-24.
# cache_write is the 5-minute rate. Agents bill the subscription, not these
# rates; fleet still needs them to enforce fleet_usd.
[prices."claude-sonnet-5"]
input = 2.0
output = 10.0
cache_read = 0.2
cache_write = 2.5
```

The model and prices are the live `fleet-fixture`'s (its PR 15, the configuration proven on the box), not the vendored copy's `claude-opus-5`: fleet-on-fleet runs on the box with the same CLIs, and a role-table model with no `[prices]` row is a `PolicyError` (`policy.py:220`).

`docs/agent/ownership.md`:

```markdown
# Ownership map

One live task per area. Tests live in the area of the code they cover.

| Glob | Area |
|---|---|
| `src/fleet/decide.py` | allocator |
| `src/fleet/expand.py` | allocator |
| `src/fleet/simulate.py` | allocator |
| `tests/test_decide*.py` | allocator |
| `tests/test_expand.py` | allocator |
| `tests/test_simulate.py` | allocator |
| `tests/test_scenarios.py` | allocator |
| `src/fleet/ledger.py` | ledger |
| `src/fleet/reader.py` | ledger |
| `src/fleet/gh.py` | ledger |
| `tests/test_ledger.py` | ledger |
| `tests/test_reader.py` | ledger |
| `tests/test_gh.py` | ledger |
| `src/fleet/cost.py` | cost |
| `tests/test_cost.py` | cost |
| `tests/test_codex_cost.py` | cost |
| `src/fleet/executor.py` | executor |
| `src/fleet/herdr.py` | executor |
| `src/fleet/herdr_executor.py` | executor |
| `src/fleet/git.py` | executor |
| `tests/test_executor.py` | executor |
| `tests/test_herdr*.py` | executor |
| `tests/test_git.py` | executor |
| `src/fleet/daemon.py` | daemon |
| `src/fleet/runlog.py` | daemon |
| `src/fleet/statesocket.py` | daemon |
| `src/fleet/status.py` | daemon |
| `tests/test_daemon.py` | daemon |
| `tests/test_runlog.py` | daemon |
| `tests/test_statesocket.py` | daemon |
| `tests/test_status.py` | daemon |
| `src/fleet/cli.py` | cli |
| `src/fleet/config.py` | cli |
| `src/fleet/policy.py` | cli |
| `tests/test_cli.py` | cli |
| `tests/test_config.py` | cli |
| `tests/test_policy.py` | cli |
| `tests/test_plan.py` | cli |
| `README.md` | docs |
| `docs/runbook.md` | docs |

## Non-dispatchable paths

No task touching these is ever approved with `task`. They are AGENTS.md's
"What requires a human" list as paths. fleet does not enforce this table; the
owner does, when reading the announcement.

| Path | Why |
|---|---|
| `src/fleet/labels.py` | the label vocabulary is repository state on GitHub |
| `src/fleet/packet.py` | packet schema ids; real issues carry the current shape |
| `src/fleet/commands.py` | `ROLE_MAY_HAND_TO` and `VERDICT_ROLES`; widening either is a privilege grant |
| `src/fleet/schemas.py` | packet validation |
| `Makefile` | the gate |
| `.github/**` | CI |
| `fixtures/**` | captured real output; re-capture is deliberate |
| `pyproject.toml`, `uv.lock` | the agentify pin, `requires-python`, rfc3339-validator |
| `AGENTS.md`, `docs/agent/**`, `.agents/**`, `fleet.toml` | this layer governs the fleet; editing it widens its own authority |
```

`docs/agent/task-classes.md`: copy `fixtures/policy/fleet-fixture/docs/agent/task-classes.md`, change every `claude-opus-5` in its Role | Agent | Model table to `claude-sonnet-5` (matching `git -C ~/TIG/fleet-fixture show origin/main:docs/agent/task-classes.md`), and replace its "Where work may run" section with:

```markdown
## Where work may run

fleet's gate is `make check`: ruff and pytest, CPU only, under a minute. Every
class may run unattended on `tig-server`.
```

Roles and schemas: `mkdir -p .agents && cp -r fixtures/policy/fleet-fixture/.agents/roles fixtures/policy/fleet-fixture/.agents/schemas .agents/` — both from the vendored copy (AGENTS.md source of truth #2; it has all six roles and both schemas). Then compare against the live fixture: `git -C ~/TIG/fleet-fixture fetch -q && for f in $(cd fixtures/policy/fleet-fixture && find .agents -type f); do git -C ~/TIG/fleet-fixture show origin/main:$f | diff -q - fixtures/policy/fleet-fixture/$f >/dev/null || echo "DIFFERS: $f"; done`. Schemas were identical on 2026-09-26 (MEASURED: `diff` of both schema files, rc 0). Any `DIFFERS` line is reported in the task report, not resolved.

`.gitignore`: append `.fleet/`.

- [ ] **Step 5: Run the test**

Run: `cd ~/TIG/fleet && uv run pytest tests/test_self_policy.py -q`
Expected: 2 passed. If `test_every_source_file_has_an_area_or_is_non_dispatchable` names a file, add its row to `ownership.md`.

- [ ] **Step 6: Mutation-check**

Delete the `src/fleet/cost.py` row from `ownership.md` → the coverage test fails naming it. Restore. Change `slots = 3` to `4` → `test_the_repository_policy_loads` fails. Restore.

- [ ] **Step 7: Full gate, and the parent-walk check**

Run: `cd ~/TIG/fleet && make check 2>&1 | tail -5`
Expected: green; count = Step 1 baseline + 2. If any existing test now fails, a test is finding the new root `fleet.toml` through `config.py:145`'s parent walk. Stop and report the test name; do not delete it.

- [ ] **Step 8: `fleet plan` loads it**

Run: `cd ~/TIG/fleet && uv run fleet plan`
Expected: `no action` or a list of intents; not a `PolicyError`.

- [ ] **Step 9: Commit, push, PR**

```bash
cd ~/TIG/fleet
git add fleet.toml docs/agent/ownership.md docs/agent/task-classes.md .agents .gitignore tests/test_self_policy.py
git status --short
git commit -m "feat: fleet is a fleet target

Its own policy layer: seven areas, AGENTS.md's human-only list as
non-dispatchable paths, three slots and a \$10 night. Spec: hermes-harness
docs/ai/specs/2026-09-26-fleet-self-healing-design.md §4.2.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/self-policy
gh pr create --repo FibonAdithya/fleet --base main --title "feat: fleet is a fleet target" --body "Spec §4.2. After merge, seed labels (plan Task 2 Step 10).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 10: Seed the labels on `FibonAdithya/fleet` (after the owner merges)**

This writes to GitHub; confirm with the owner first.

```bash
cd ~/TIG/fleet && git switch main && git pull -q
uv run python -c "
from fleet.config import load_config
from fleet.gh import RealGh
from fleet.labels import static_vocabulary
from fleet.policy import area_names
cfg = load_config()
gh = RealGh(cfg.repo_slug)
for name in sorted(static_vocabulary(cfg.max_attempts, cfg.max_fix_passes, cfg.max_rebases, area_names(cfg))):
    gh.ensure_label(name)
print('ok')"
gh label list --repo FibonAdithya/fleet --limit 100 | grep -c 'area:'
```

Expected: `ok`, then `7`. Also create the integration branch if absent: `git ls-remote --exit-code origin integration || (git push origin main:integration)`.

---

## Part B — `hermes-harness`

Work on branch `feat/fleet-self-healing` off `origin/master` in a worktree: `git worktree add .worktrees/fleet-self-healing-impl -b feat/fleet-self-healing origin/master`. Commit the spec and this plan onto it first (cherry-pick from `docs/fleet-self-healing`).

### Task 3: Ledger verbs on the box

**Files:**
- Create: `box/tasklib.py`
- Create: `box/verbs/file_task`, `box/verbs/list_triage`, `box/verbs/approve_task`, `box/verbs/close_task`
- Test: `box/tests/fakegh.py` (the fake, no pytest import, so the verb tests' `gh` shim can load it), `box/tests/test_tasklib.py`, `box/tests/test_task_verbs.py`

**Interfaces:**
- Consumes: `boxlib._name`, `boxlib.SLUG_RE`, `boxlib.refuse`, `boxlib.read_json_stdin`; Task 1's `fleet.policy.area_names`.
- Produces (verbs, JSON in/out):
  - `file_task {repo, title, body, area, cls}` → `{repo, slug, number, url}`
  - `list_triage {}` → `{issues: [{repo, slug, number, title, body, labels, hash}], repos_ok: [repo], errors: [str]}`
  - `approve_task {repo, number, hash, mode: "fleet"|"solo"}` → `{repo, slug, number, title, body}`
  - `close_task {repo, number}` → `{repo, number, closed: true}`
- Produces (Python, used by Task 7): `tasklib.Gh`, `tasklib.real_gh`, `tasklib.TaskError`, `tasklib.issue_hash(title, body) -> str`, `tasklib.repo_dir(home, name) -> Path`, `tasklib.slug(repo_dir) -> str`, `tasklib.ensure_label(gh, slug, name)`, `tasklib.file_issue(gh, slug, title, body, area, cls, areas, source, extra_labels=()) -> dict`, `tasklib.open_issues_with_label(gh, slug, label) -> list[dict]`, `tasklib.comment(gh, slug, number, body)`.

- [ ] **Step 1: Write the fake and the failing library tests**

`box/tests/fakegh.py` — imports nothing from pytest or tasklib, because Step 5's `gh` shim runs it as a plain script:

```python
"""A stand-in for the gh CLI, shared by the tasklib, verb and doctor tests."""
import json

SLUG = "FibonAdithya/fleet-fixture"


class FakeGh:
    """Stands in for the gh CLI. Labels must exist before they are applied, as with real gh."""

    def __init__(self):
        self.labels = {"fleet:triage", "fleet:ready", "fleet:auto-ok", "fleet:human", "area:docs", "class:patch"}
        self.issues = {}
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append((tuple(args), stdin))
        cmd = args[:2]
        if cmd == ["label", "create"]:
            if args[2] in self.labels:
                return 1, "", f"label with name \"{args[2]}\" already exists; use `--force` to update its color and description"
            self.labels.add(args[2])
            return 0, "", ""
        if cmd == ["issue", "create"]:
            names = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
            missing = [n for n in names if n not in self.labels]
            if missing:
                return 1, "", f"could not add label: '{missing[0]}' not found"
            n = len(self.issues) + 1
            title = args[args.index("--title") + 1]
            self.issues[n] = {"number": n, "title": title, "body": stdin, "state": "OPEN",
                              "labels": [{"name": x} for x in names], "comments": []}
            return 0, f"https://github.com/{SLUG}/issues/{n}\n", ""
        if cmd == ["issue", "view"]:
            i = self.issues.get(int(args[2]))
            return (0, json.dumps(i), "") if i else (1, "", "not found")
        if cmd == ["issue", "edit"]:
            i = self.issues[int(args[2])]
            if "--add-label" in args:
                for n in args[args.index("--add-label") + 1].split(","):
                    if n not in self.labels:
                        return 1, "", f"'{n}' not found"
                    if n not in [x["name"] for x in i["labels"]]:
                        i["labels"].append({"name": n})
            if "--remove-label" in args:
                gone = set(args[args.index("--remove-label") + 1].split(","))
                i["labels"] = [x for x in i["labels"] if x["name"] not in gone]
            return 0, "", ""
        if cmd == ["issue", "close"]:
            self.issues[int(args[2])]["state"] = "CLOSED"
            return 0, "", ""
        if cmd == ["issue", "list"]:
            label = args[args.index("--label") + 1]
            rows = [i for i in self.issues.values() if i["state"] == "OPEN" and label in [x["name"] for x in i["labels"]]]
            return 0, json.dumps(rows), ""
        if cmd == ["issue", "comment"]:
            self.issues[int(args[2])]["comments"].append(args[args.index("--body") + 1])
            return 0, "", ""
        raise AssertionError(f"unexpected gh call {args}")
```

`box/tests/test_tasklib.py`:

```python
"""tasklib: the only code on the box that writes the fleet ledger."""
import pytest

import tasklib
from fakegh import SLUG, FakeGh
from tasklib import TaskError


def names(gh, n):
    return {x["name"] for x in gh.issues[n]["labels"]}


def test_issue_hash_is_sha256_of_title_newline_body():
    import hashlib
    assert tasklib.issue_hash("t", "b") == hashlib.sha256(b"t\nb").hexdigest()
    assert tasklib.issue_hash("t", "b") != tasklib.issue_hash("t", "b ")


def test_file_issue_lands_in_triage_and_never_ready():
    gh = FakeGh()
    out = tasklib.file_issue(gh, SLUG, " Fix the stall check ", "body", "docs", "patch", frozenset({"docs"}), "source:hermes")
    assert out == {"number": 1, "url": f"https://github.com/{SLUG}/issues/1"}
    assert names(gh, 1) == {"fleet:triage", "area:docs", "class:patch", "source:hermes"}
    assert gh.issues[1]["title"] == "Fix the stall check"


def test_file_issue_refuses_an_area_the_repo_does_not_have():
    gh = FakeGh()
    with pytest.raises(TaskError, match="area 'eval' is not in"):
        tasklib.file_issue(gh, SLUG, "t", "b", "eval", "patch", frozenset({"docs"}), "source:hermes")
    assert gh.issues == {}


@pytest.mark.parametrize("cls", ["bug", "", "PATCH"])
def test_file_issue_refuses_an_unknown_class(cls):
    with pytest.raises(TaskError, match="class"):
        tasklib.file_issue(FakeGh(), SLUG, "t", "b", "docs", cls, frozenset({"docs"}), "source:hermes")


@pytest.mark.parametrize("title,body", [("", "b"), ("   ", "b"), ("x" * 201, "b"), ("t", "x" * 20001)])
def test_file_issue_refuses_empty_or_oversized_text(title, body):
    with pytest.raises(TaskError):
        tasklib.file_issue(FakeGh(), SLUG, title, body, "docs", "patch", frozenset({"docs"}), "source:hermes")


def test_file_issue_refuses_a_source_that_is_not_a_producer():
    """Catches passing the caller's label through: Hermes must not be able to file as the doctor."""
    with pytest.raises(TaskError, match="source"):
        tasklib.file_issue(FakeGh(), SLUG, "t", "b", "docs", "patch", frozenset({"docs"}), "fleet:ready")


def _filed(gh):
    tasklib.file_issue(gh, SLUG, "t", "b", "docs", "patch", frozenset({"docs"}), "source:hermes")
    return tasklib.issue_hash("t", "b")


def test_approve_for_fleet_adds_ready_and_auto_ok_then_removes_triage():
    gh = FakeGh()
    h = _filed(gh)
    out = tasklib.approve(gh, SLUG, 1, h, "fleet")
    assert out == {"number": 1, "title": "t", "body": "b"}
    assert names(gh, 1) == {"fleet:ready", "fleet:auto-ok", "area:docs", "class:patch", "source:hermes"}
    edits = [c[0] for c in gh.calls if c[0][:2] == ("issue", "edit")]
    assert "--add-label" in edits[0] and "--remove-label" in edits[1]  # add before remove


def test_approve_for_solo_moves_to_human_and_marks_solo():
    gh = FakeGh()
    h = _filed(gh)
    tasklib.approve(gh, SLUG, 1, h, "solo")
    assert names(gh, 1) == {"fleet:human", "fleet:solo", "area:docs", "class:patch", "source:hermes"}


def test_approve_refuses_when_the_text_changed_since_announcement():
    gh = FakeGh()
    h = _filed(gh)
    gh.issues[1]["body"] = "b, plus something the owner never read"
    with pytest.raises(TaskError, match="changed since it was announced"):
        tasklib.approve(gh, SLUG, 1, h, "fleet")
    assert "fleet:ready" not in names(gh, 1)


def test_approve_refuses_a_closed_or_already_moved_issue():
    gh = FakeGh()
    h = _filed(gh)
    gh.issues[1]["state"] = "CLOSED"
    with pytest.raises(TaskError, match="closed"):
        tasklib.approve(gh, SLUG, 1, h, "fleet")
    gh.issues[1]["state"] = "OPEN"
    gh.issues[1]["labels"] = [{"name": "fleet:ready"}]
    with pytest.raises(TaskError, match="no longer in triage"):
        tasklib.approve(gh, SLUG, 1, h, "fleet")


def test_approve_refuses_an_unknown_mode():
    gh = FakeGh()
    h = _filed(gh)
    with pytest.raises(TaskError, match="mode"):
        tasklib.approve(gh, SLUG, 1, h, "ready")


def test_close_marks_dropped_and_closes():
    gh = FakeGh()
    _filed(gh)
    tasklib.close(gh, SLUG, 1)
    assert gh.issues[1]["state"] == "CLOSED" and "fleet:dropped" in names(gh, 1)


def test_triage_lists_open_triage_issues_with_their_hash():
    gh = FakeGh()
    _filed(gh)
    rows = tasklib.triage(gh, SLUG)
    assert rows == [{"number": 1, "title": "t", "body": "b",
                     "labels": ["fleet:triage", "area:docs", "class:patch", "source:hermes"],
                     "hash": tasklib.issue_hash("t", "b")}]


def test_repo_dir_needs_a_fleet_toml(tmp_path):
    (tmp_path / "TIG" / "plain").mkdir(parents=True)
    with pytest.raises(TaskError, match="no fleet.toml"):
        tasklib.repo_dir(tmp_path, "plain")
    with pytest.raises(ValueError):
        tasklib.repo_dir(tmp_path, "../etc")


def test_slug_reads_fleet_toml(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "fleet.toml").write_text('[repo]\nslug = "FibonAdithya/fleet"\n')
    assert tasklib.slug(d) == "FibonAdithya/fleet"
    (d / "fleet.toml").write_text('[repo]\nslug = "not a slug"\n')
    with pytest.raises(TaskError, match="slug"):
        tasklib.slug(d)


def test_fleet_repos_are_discovered_not_listed(tmp_path):
    for name, has in (("a", True), ("b", False), ("c", True)):
        (tmp_path / "TIG" / name).mkdir(parents=True)
        if has:
            (tmp_path / "TIG" / name / "fleet.toml").write_text("")
    assert [p.name for p in tasklib.fleet_repos(tmp_path)] == ["a", "c"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest tests/test_tasklib.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'tasklib'`.

- [ ] **Step 3: Implement `box/tasklib.py`**

```python
"""The fleet ledger, as the box's verbs see it.

The only code on the box that writes fleet issues: file_task, approve_task,
close_task and the doctor all come through here. Every call takes the gh runner
as an argument so tests stand in for GitHub; production passes real_gh, which
uses the box's gh login.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Callable

import boxlib

Gh = Callable[[list[str], "str | None"], "tuple[int, str, str]"]

# fleet/src/fleet/labels.py::CLASSES. Extend by PR when fleet does.
CLASSES = ("patch", "spec", "investigation", "integration")
TRIAGE, READY, AUTO_OK, HUMAN = "fleet:triage", "fleet:ready", "fleet:auto-ok", "fleet:human"
SOLO, DROPPED = "fleet:solo", "fleet:dropped"
SOURCES = ("source:hermes", "source:doctor")
MODES = {"fleet": (READY, AUTO_OK), "solo": (HUMAN, SOLO)}
MAX_TITLE, MAX_BODY = 200, 20000


class TaskError(ValueError):
    pass


def real_gh(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    return p.returncode, p.stdout, p.stderr


def issue_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()


def fleet_repos(home: Path) -> list[Path]:
    root = Path(home) / "TIG"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "fleet.toml").is_file())


def repo_dir(home: Path, name: str) -> Path:
    d = Path(home) / "TIG" / boxlib._name(name)
    if not (d / "fleet.toml").is_file():
        raise TaskError(f"{name} has no fleet.toml")
    return d


def slug(repo: Path) -> str:
    try:
        s = tomllib.loads((Path(repo) / "fleet.toml").read_text())["repo"]["slug"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"{Path(repo).name}/fleet.toml has no [repo] slug: {exc}") from exc
    if not isinstance(s, str) or not boxlib.SLUG_RE.fullmatch(s):
        raise TaskError(f"{Path(repo).name}/fleet.toml slug is not owner/name: {s!r}")
    return s


def fleet_areas(repo: Path, home: Path) -> frozenset[str]:
    """The target's areas, read by fleet's own parser (fleet.policy.area_names)."""
    code = ("import json; from fleet.config import load_config; from fleet.policy import area_names; "
            "print(json.dumps(sorted(area_names(load_config()))))")
    p = subprocess.run(["uv", "run", "--project", str(Path(home) / "TIG" / "fleet"), "python", "-c", code],
                       cwd=repo, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise TaskError(f"could not read {Path(repo).name}'s areas: {p.stderr.strip()[-300:]}")
    return frozenset(json.loads(p.stdout.strip().splitlines()[-1]))


def _ok(gh: Gh, args: list[str], stdin: str | None = None) -> str:
    rc, out, err = gh(args, stdin)
    if rc != 0:
        raise TaskError(f"gh {' '.join(args[:2])} failed: {err.strip()[:300]}")
    return out


def ensure_label(gh: Gh, repo_slug: str, name: str) -> None:
    rc, _, err = gh(["label", "create", name, "--repo", repo_slug, "--color", "ededed"], None)
    if rc != 0 and "already exists" not in err:
        raise TaskError(f"gh label create {name} failed: {err.strip()[:300]}")


def file_issue(gh: Gh, repo_slug: str, title: str, body: str, area: str, cls: str,
               areas: frozenset[str], source: str, extra_labels: tuple[str, ...] = ()) -> dict:
    title = (title or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise TaskError(f"title must be 1..{MAX_TITLE} characters")
    if not isinstance(body, str) or len(body) > MAX_BODY:
        raise TaskError(f"body must be at most {MAX_BODY} characters")
    if area not in areas:
        raise TaskError(f"area {area!r} is not in {repo_slug}'s ownership.md (have: {', '.join(sorted(areas))})")
    if cls not in CLASSES:
        raise TaskError(f"class must be one of {', '.join(CLASSES)}: {cls!r}")
    if source not in SOURCES:
        raise TaskError(f"source must be one of {', '.join(SOURCES)}: {source!r}")
    for name in (source, *extra_labels):
        ensure_label(gh, repo_slug, name)
    labels = (TRIAGE, f"area:{area}", f"class:{cls}", source, *extra_labels)
    args = ["issue", "create", "--repo", repo_slug, "--title", title, "--body-file", "-"]
    for name in labels:
        args += ["--label", name]
    url = _ok(gh, args, body).strip().splitlines()[-1]
    return {"number": int(url.rsplit("/", 1)[1]), "url": url}


def _view(gh: Gh, repo_slug: str, number: int) -> dict:
    return json.loads(_ok(gh, ["issue", "view", str(int(number)), "--repo", repo_slug,
                               "--json", "state,title,body,labels"]))


def approve(gh: Gh, repo_slug: str, number: int, want_hash: str, mode: str) -> dict:
    """The owner's `task` or `solo` reply. Refuses unless the issue is still the one they read."""
    if mode not in MODES:
        raise TaskError(f"mode must be fleet or solo: {mode!r}")
    row = _view(gh, repo_slug, number)
    names = {x["name"] for x in row.get("labels", [])}
    if str(row.get("state", "")).upper() != "OPEN":
        raise TaskError(f"#{number} is closed")
    if TRIAGE not in names:
        raise TaskError(f"#{number} is no longer in triage")
    if issue_hash(row["title"], row["body"]) != want_hash:
        raise TaskError(f"#{number} changed since it was announced; wait for the new announcement")
    add = MODES[mode]
    for name in add:
        ensure_label(gh, repo_slug, name)
    # Add before remove, as fleet's own CLI does: a failure between the two leaves
    # the issue in two states, which fleet's ledger refuses loudly, rather than in
    # none, which it reads as triage and forgets.
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--add-label", ",".join(add)])
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--remove-label", TRIAGE])
    return {"number": int(number), "title": row["title"], "body": row["body"]}


def close(gh: Gh, repo_slug: str, number: int) -> None:
    ensure_label(gh, repo_slug, DROPPED)
    _ok(gh, ["issue", "edit", str(int(number)), "--repo", repo_slug, "--add-label", DROPPED])
    _ok(gh, ["issue", "close", str(int(number)), "--repo", repo_slug])


def open_issues_with_label(gh: Gh, repo_slug: str, label: str) -> list[dict]:
    return json.loads(_ok(gh, ["issue", "list", "--repo", repo_slug, "--state", "open", "--label", label,
                               "--json", "number,title,body,labels", "--limit", "200"]) or "[]")


def triage(gh: Gh, repo_slug: str) -> list[dict]:
    return [{"number": r["number"], "title": r["title"], "body": r["body"],
             "labels": [x["name"] for x in r.get("labels", [])], "hash": issue_hash(r["title"], r["body"])}
            for r in open_issues_with_label(gh, repo_slug, TRIAGE)]


def comment(gh: Gh, repo_slug: str, number: int, body: str) -> None:
    _ok(gh, ["issue", "comment", str(int(number)), "--repo", repo_slug, "--body", body])
```

- [ ] **Step 4: Run the library tests**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest tests/test_tasklib.py -q`
Expected: all pass.

- [ ] **Step 5: Write the failing verb tests**

`box/tests/test_task_verbs.py` drives each verb as a subprocess with a `gh` shim on PATH, as `test_deploy.py` does with `git`. The shim is a Python script that loads a JSON state file, applies `FakeGh` semantics, and writes it back.

```python
"""The four ledger verbs, end to end through a gh shim."""
import json
import os
import subprocess
import sys
from pathlib import Path

BOX = Path(__file__).resolve().parents[1]
SHIM = r'''#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, os.environ["TESTS_DIR"])
from fakegh import FakeGh
state = os.environ["GH_STATE"]
gh = FakeGh()
if os.path.exists(state):
    s = json.load(open(state)); gh.labels = set(s["labels"]); gh.issues = {int(k): v for k, v in s["issues"].items()}
stdin = sys.stdin.read() if "--body-file" in sys.argv else None
rc, out, err = gh(sys.argv[1:], stdin)
json.dump({"labels": sorted(gh.labels), "issues": gh.issues}, open(state, "w"))
sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)
'''
UV_SHIM = '#!/bin/sh\necho \'["daemon", "docs"]\'\n'


def home_with_repo(tmp_path, name="fleet-fixture", slug="FibonAdithya/fleet-fixture"):
    home = tmp_path / "home"
    d = home / "TIG" / name
    d.mkdir(parents=True)
    (d / "fleet.toml").write_text(f'[repo]\nslug = "{slug}"\n')
    (home / "TIG" / "notfleet").mkdir()
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "gh").write_text(SHIM)
    (shims / "uv").write_text(UV_SHIM)
    for s in ("gh", "uv"):
        (shims / s).chmod(0o755)
    return home, shims


def run(verb, home, shims, args):
    env = {"HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}",
           "GH_STATE": str(home / "gh.json"), "TESTS_DIR": str(BOX / "tests")}
    p = subprocess.run([str(BOX / "verbs" / verb)], input=json.dumps(args).encode(), env=env,
                       capture_output=True, check=False)
    return p.returncode, json.loads(p.stdout)


def test_file_then_list_then_approve(tmp_path):
    home, shims = home_with_repo(tmp_path)
    rc, out = run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    assert rc == 0 and out["number"] == 1 and out["slug"] == "FibonAdithya/fleet-fixture"
    rc, out = run("list_triage", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and out["errors"] == []
    [issue] = out["issues"]
    assert (issue["repo"], issue["slug"], issue["number"]) == ("fleet-fixture", "FibonAdithya/fleet-fixture", 1)
    rc, out = run("approve_task", home, shims, {"repo": "fleet-fixture", "number": 1, "hash": issue["hash"], "mode": "fleet"})
    assert rc == 0 and out["title"] == "t"
    assert run("list_triage", home, shims, {})[1]["issues"] == []


def test_file_task_refuses_a_repo_without_fleet_toml_and_a_bad_area(tmp_path):
    home, shims = home_with_repo(tmp_path)
    rc, out = run("file_task", home, shims, {"repo": "notfleet", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    assert rc == 2 and "no fleet.toml" in out["error"]
    rc, out = run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "eval", "cls": "patch"})
    assert rc == 2 and "area 'eval'" in out["error"]


def test_file_task_always_files_as_hermes(tmp_path):
    """Catches a verb that forwards a caller-supplied source label."""
    home, shims = home_with_repo(tmp_path)
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch",
                                   "source": "source:doctor"})
    issue = run("list_triage", home, shims, {})[1]["issues"][0]
    assert "source:hermes" in issue["labels"] and "source:doctor" not in issue["labels"]


def test_approve_task_refuses_a_stale_hash(tmp_path):
    home, shims = home_with_repo(tmp_path)
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    rc, out = run("approve_task", home, shims, {"repo": "fleet-fixture", "number": 1, "hash": "0" * 64, "mode": "fleet"})
    assert rc == 2 and "changed since it was announced" in out["error"]


def test_close_task(tmp_path):
    home, shims = home_with_repo(tmp_path)
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    rc, out = run("close_task", home, shims, {"repo": "fleet-fixture", "number": 1})
    assert (rc, out) == (0, {"repo": "fleet-fixture", "number": 1, "closed": True})


def test_list_triage_reports_a_failing_repo_without_dropping_the_others(tmp_path):
    home, shims = home_with_repo(tmp_path)
    bad = home / "TIG" / "broken"
    bad.mkdir()
    (bad / "fleet.toml").write_text("[repo]\n")  # no slug
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    rc, out = run("list_triage", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and len(out["issues"]) == 1
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("broken:")
```

- [ ] **Step 6: Run to verify failure**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest tests/test_task_verbs.py -q 2>&1 | tail -3`
Expected: FAIL, the verb files do not exist.

- [ ] **Step 7: Implement the four verbs**

`box/verbs/file_task`:

```python
#!/usr/bin/env python3
"""File a fleet task in triage for the owner to approve. Ungated: triage is inert until they reply."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402
import tasklib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
home = Path(os.environ.get("HOME", "/home/adi"))
try:
    repo = tasklib.repo_dir(home, a.get("repo", ""))
    slug = tasklib.slug(repo)
    out = tasklib.file_issue(tasklib.real_gh, slug, a.get("title", ""), a.get("body", ""), a.get("area", ""),
                             a.get("cls", ""), tasklib.fleet_areas(repo, home), "source:hermes")
except (ValueError, TypeError) as exc:
    sys.exit(boxlib.refuse(str(exc)))
print(json.dumps({"repo": repo.name, "slug": slug, **out}))
```

`box/verbs/list_triage`:

```python
#!/usr/bin/env python3
"""Open fleet:triage issues in every repo under ~/TIG that has a fleet.toml. Ungated, read-only."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402
import tasklib  # noqa: E402

boxlib.read_json_stdin(sys.stdin.buffer)
home = Path(os.environ.get("HOME", "/home/adi"))
issues, ok, errors = [], [], []
for repo in tasklib.fleet_repos(home):
    try:
        slug = tasklib.slug(repo)
        rows = tasklib.triage(tasklib.real_gh, slug)
    except (ValueError, TypeError) as exc:
        errors.append(f"{repo.name}: {exc}")
        continue
    ok.append(repo.name)
    issues += [{"repo": repo.name, "slug": slug, **r} for r in rows]
print(json.dumps({"issues": issues, "repos_ok": ok, "errors": errors}))
```

`box/verbs/approve_task`:

```python
#!/usr/bin/env python3
"""The owner's `task`/`solo` reply. Called only by the broker's approvals listener, never by an MCP tool."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402
import tasklib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
home = Path(os.environ.get("HOME", "/home/adi"))
try:
    repo = tasklib.repo_dir(home, a.get("repo", ""))
    slug = tasklib.slug(repo)
    out = tasklib.approve(tasklib.real_gh, slug, int(a.get("number", 0)), str(a.get("hash", "")), a.get("mode", ""))
except (ValueError, TypeError) as exc:
    sys.exit(boxlib.refuse(str(exc)))
print(json.dumps({"repo": repo.name, "slug": slug, **out}))
```

`box/verbs/close_task`:

```python
#!/usr/bin/env python3
"""The owner's `drop` reply: close the issue, labelled fleet:dropped. Called only by the approvals listener."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402
import tasklib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
home = Path(os.environ.get("HOME", "/home/adi"))
try:
    repo = tasklib.repo_dir(home, a.get("repo", ""))
    number = int(a.get("number", 0))
    tasklib.close(tasklib.real_gh, tasklib.slug(repo), number)
except (ValueError, TypeError) as exc:
    sys.exit(boxlib.refuse(str(exc)))
print(json.dumps({"repo": repo.name, "number": number, "closed": True}))
```

`chmod +x box/verbs/file_task box/verbs/list_triage box/verbs/approve_task box/verbs/close_task`

- [ ] **Step 8: Run all box tests**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 9: Mutation-check**

- `tasklib.approve`: delete the hash comparison → `test_approve_refuses_when_the_text_changed_since_announcement` and `test_approve_task_refuses_a_stale_hash` fail.
- `tasklib.approve`: swap the add and remove edits → `test_approve_for_fleet_adds_ready_and_auto_ok_then_removes_triage` fails.
- `file_task` verb: pass `a.get("source", "source:hermes")` → `test_file_task_always_files_as_hermes` fails.
- `list_triage` verb: move `ok.append` before the `try` → `test_list_triage_reports_a_failing_repo_without_dropping_the_others` fails.

- [ ] **Step 10: Commit**

```bash
git add box/tasklib.py box/verbs/file_task box/verbs/list_triage box/verbs/approve_task box/verbs/close_task box/tests/fakegh.py box/tests/test_tasklib.py box/tests/test_task_verbs.py
git status --short
git commit -m "feat(box): ledger verbs for fleet tasks -- file, list triage, approve, close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: Task codes on the broker

**Files:**
- Create: `broker/hermes_broker/tasks.py`
- Modify: `broker/hermes_broker/grants.py` (`create_request` gains `avoid`; new `pending_codes`)
- Modify: `broker/hermes_broker/server.py` (`request_access` passes `avoid`)
- Test: `broker/tests/test_tasks.py`, `broker/tests/test_grants.py`

**Interfaces:**
- Produces: `TaskStore(path)` with `announce(repo: str, slug: str, number: int, body_hash: str, now: float, avoid: set[str]) -> str | None`, `take(code: str, now: float) -> dict | None` (keys `repo, slug, number, hash`), `prune(listed_repos: set[str], present: set[tuple[str, int]], now: float) -> None`, `live_codes(now: float) -> set[str]`; `CODE_TTL_SECONDS = 7 * 24 * 3600`. `GrantStore.pending_codes(now) -> set[str]`; `GrantStore.create_request(box, minutes, reason, now, avoid=frozenset())`.

- [ ] **Step 1: Write the failing tests**

`broker/tests/test_tasks.py`:

```python
"""Task codes: one per announced issue, bound to the text the owner was shown."""
import os
import stat

import pytest
from hermes_broker.tasks import CODE_TTL_SECONDS, TaskStore

H1, H2 = "a" * 64, "b" * 64


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "tasks.json")


def test_first_announcement_gets_a_four_digit_code(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert code is not None and len(code) == 4 and code.isdigit()


def test_the_same_issue_and_text_is_not_announced_twice(store):
    store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1300.0, avoid=set()) is None


def test_changed_text_gets_a_new_code_and_kills_the_old(store):
    old = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    new = store.announce("fleet", "FibonAdithya/fleet", 42, H2, now=1300.0, avoid=set())
    assert new is not None and new != old
    assert store.take(old, now=1301.0) is None
    assert store.take(new, now=1301.0)["hash"] == H2


def test_take_is_single_use(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.take(code, now=1001.0) == {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "hash": H1}
    assert store.take(code, now=1002.0) is None


def test_codes_expire_after_seven_days(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.take(code, now=1000.0 + CODE_TTL_SECONDS) is None


def test_expired_issue_is_announced_again(store):
    store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0 + CODE_TTL_SECONDS, avoid=set()) is not None


def test_avoided_codes_are_never_issued(store, monkeypatch):
    import hermes_broker.tasks as tasks
    draws = iter([1234, 1234, 5678])
    monkeypatch.setattr(tasks.secrets, "randbelow", lambda n: next(draws))
    assert store.announce("fleet", "FibonAdithya/fleet", 1, H1, now=1.0, avoid={"1234"}) == "5678"


def test_prune_drops_issues_no_longer_in_triage_only_for_repos_listed(store):
    """Catches pruning every repo when one repo's listing failed: its issues
    would all be re-announced on the next tick."""
    a = store.announce("fleet", "FibonAdithya/fleet", 1, H1, now=1.0, avoid=set())
    b = store.announce("fleet-fixture", "FibonAdithya/fleet-fixture", 2, H1, now=1.0, avoid=set())
    store.prune(listed_repos={"fleet"}, present=set(), now=2.0)
    assert store.take(a, now=3.0) is None
    assert store.take(b, now=3.0) is not None


def test_codes_survive_a_listener_restart(tmp_path):
    code = TaskStore(tmp_path / "tasks.json").announce("fleet", "FibonAdithya/fleet", 7, H1, now=1.0, avoid=set())
    assert TaskStore(tmp_path / "tasks.json").take(code, now=2.0)["number"] == 7


def test_file_is_owner_only(store):
    store.announce("fleet", "FibonAdithya/fleet", 7, H1, now=1.0, avoid=set())
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_live_codes(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 7, H1, now=1.0, avoid=set())
    assert store.live_codes(now=2.0) == {code}
    assert store.live_codes(now=2.0 + CODE_TTL_SECONDS) == set()
```

Append to `broker/tests/test_grants.py`:

```python
def test_request_codes_avoid_live_task_codes(store, monkeypatch):
    import hermes_broker.grants as grants
    draws = iter([4321, 1111])
    monkeypatch.setattr(grants.secrets, "randbelow", lambda n: next(draws))
    assert store.create_request("tig-server", 30, "r", now=1.0, avoid={"4321"}) == "1111"


def test_pending_codes_lists_unexpired_requests(store):
    code = store.create_request("tig-server", 30, "r", now=1.0)
    assert store.pending_codes(now=2.0) == {code}
    assert store.pending_codes(now=1.0 + 121) == set()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd broker && uv run --extra dev pytest tests/test_tasks.py tests/test_grants.py -q 2>&1 | tail -3`
Expected: FAIL (no module `hermes_broker.tasks`; `avoid` unexpected keyword).

- [ ] **Step 3: Implement**

`broker/hermes_broker/tasks.py`:

```python
"""Codes for announced fleet tasks.

One code per triage issue, bound to the hash of the title and body the owner was
shown. Codes are separate from grant codes (grants.py) but drawn from the same
four-digit space, so each store avoids the other's live codes.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

CODE_TTL_SECONDS = 7 * 24 * 3600


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"codes": {}}
        data.setdefault("codes", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    @staticmethod
    def _live(data: dict[str, Any], now: float) -> dict[str, dict]:
        return {c: e for c, e in data["codes"].items() if e["expires_at"] > now}

    def live_codes(self, now: float) -> set[str]:
        return set(self._live(self._read(), now))

    def announce(self, repo: str, slug: str, number: int, body_hash: str, now: float, avoid: set[str]) -> str | None:
        """A new code for this issue, or None if it is already announced with this text."""
        data = self._read()
        codes = self._live(data, now)
        mine = [c for c, e in codes.items() if e["repo"] == repo and e["number"] == int(number)]
        if any(codes[c]["hash"] == body_hash for c in mine):
            return None
        for c in mine:
            codes.pop(c)
        while True:
            code = f"{secrets.randbelow(10000):04d}"
            if code not in codes and code not in avoid:
                break
        codes[code] = {"repo": repo, "slug": slug, "number": int(number), "hash": body_hash,
                       "expires_at": now + CODE_TTL_SECONDS}
        data["codes"] = codes
        self._write(data)
        return code

    def take(self, code: str, now: float) -> dict | None:
        data = self._read()
        entry = data["codes"].pop(code, None)
        self._write(data)
        if entry is None or entry["expires_at"] <= now:
            return None
        return {k: entry[k] for k in ("repo", "slug", "number", "hash")}

    def prune(self, listed_repos: set[str], present: set[tuple[str, int]], now: float) -> None:
        """Forget codes for issues that left triage. Only repos whose listing succeeded are judged."""
        data = self._read()
        data["codes"] = {c: e for c, e in self._live(data, now).items()
                         if e["repo"] not in listed_repos or (e["repo"], e["number"]) in present}
        self._write(data)
```

`broker/hermes_broker/grants.py` — change `create_request`'s signature to `def create_request(self, box: str, minutes: int, reason: str, now: float, avoid: set[str] | frozenset[str] = frozenset()) -> str:` and replace the single draw with:

```python
        while True:
            code = f"{secrets.randbelow(10000):04d}"
            if code not in data["pending"] and code not in avoid:
                break
```

Add after `pending_reason`:

```python
    def pending_codes(self, now: float) -> set[str]:
        return {c for c, r in self._read()["pending"].items() if r["expires_at"] > now}
```

`broker/hermes_broker/server.py` — add `from .tasks import TaskStore` and in `request_access`:

```python
    now = time.time()
    avoid = TaskStore(_config().state_dir / "tasks.json").live_codes(now)
    code = _store().create_request(box, minutes, reason, now=now, avoid=avoid)
```

- [ ] **Step 4: Run the broker suite**

Run: `cd broker && uv run --extra dev pytest -q 2>&1 | tail -3`
Expected: all pass. If `tests/test_gating.py`'s `request_access` test fails because `_config()` is now called, monkeypatch `server._config` there the way `test_deploy.py`'s fixture patches `_store`: `monkeypatch.setattr(server, "_config", lambda: SimpleNamespace(state_dir=tmp_path))`.

- [ ] **Step 5: Mutation-check**

- `TaskStore.prune`: drop the `e["repo"] not in listed_repos or` clause → `test_prune_drops_issues_no_longer_in_triage_only_for_repos_listed` fails.
- `TaskStore.announce`: remove the `code not in avoid` condition → `test_avoided_codes_are_never_issued` fails (returns "1234").
- `TaskStore.announce`: skip the `for c in mine: codes.pop(c)` → `test_changed_text_gets_a_new_code_and_kills_the_old` fails.
- `GrantStore.create_request`: ignore `avoid` → `test_request_codes_avoid_live_task_codes` fails.

- [ ] **Step 6: Commit**

```bash
git add broker/hermes_broker/tasks.py broker/hermes_broker/grants.py broker/hermes_broker/server.py broker/tests/test_tasks.py broker/tests/test_grants.py
git status --short
git commit -m "feat(broker): task codes bound to the text the owner read

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Add `broker/tests/test_gating.py` to the `git add` if Step 4 changed it.)

### Task 5: Reply verbs and the announcer

**Files:**
- Modify: `broker/hermes_broker/approvals.py` (parser, `handle_task`, `Announcer`, `run_listener`)
- Modify: `broker/hermes_broker/listen.py`
- Test: `broker/tests/test_approvals.py`, `broker/tests/test_announcer.py`

**Interfaces:**
- Consumes: Task 3's verbs (`list_triage`, `approve_task`, `close_task`, and the existing `run_task`); Task 4's `TaskStore`, `GrantStore.pending_codes`.
- Produces: `parse_command` returns `("task"|"solo"|"drop", "NNNN")`; `handle_task(tasks, verb, code, now, call, repos) -> str`; `Announcer(tasks, grants, call, send)` with `.tick(now)`; `format_announcement(issue: dict, code: str) -> str`; `ANNOUNCE_EVERY = 300`, `BODY_CHARS = 1500`, `SOLO_MINUTES = 60`. `call` is `Callable[[str, dict, int], dict]` — `call(verb, args, timeout)` against tig-server.

- [ ] **Step 1: Write the failing parser tests**

Append to `broker/tests/test_approvals.py`:

```python
import pytest


@pytest.mark.parametrize("verb", ["task", "solo", "drop"])
def test_task_replies_are_parsed(verb):
    assert parse_command(msg(f" {verb.upper()} 7310 "), OWNER) == (verb, "7310")


@pytest.mark.parametrize("text", ["task", "task 731", "task 73100", "solo abcd", "drop 7310 now", "tasks 7310"])
def test_malformed_task_replies_ignored(text):
    assert parse_command(msg(text), OWNER) is None


def test_task_reply_forwarded_or_from_someone_else_ignored():
    assert parse_command(msg("task 7310", forward_origin={"type": "user"}), OWNER) is None
    update = msg("task 7310")
    update["message"]["from"]["id"] = 999
    assert parse_command(update, OWNER) is None
```

- [ ] **Step 2: Write the failing handler and announcer tests**

`broker/tests/test_announcer.py`:

```python
"""The announcer posts each triage issue once; the owner's reply acts on exactly what was posted."""
import pytest
from hermes_broker.approvals import Announcer, format_announcement, handle_task
from hermes_broker.grants import GrantStore
from hermes_broker.tasks import TaskStore

H = "c" * 64
ISSUE = {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "title": "Stall check misses long tool calls",
         "body": "evidence", "labels": ["fleet:triage", "source:doctor"], "hash": H}


class Box:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, verb, args, timeout=60):
        self.calls.append((verb, args))
        r = self.replies.get(verb, {})
        return r(args) if callable(r) else dict(r)


@pytest.fixture
def stores(tmp_path):
    return TaskStore(tmp_path / "tasks.json"), GrantStore(tmp_path / "grants.json")


def test_each_issue_is_announced_once(stores):
    tasks, grants = stores
    sent = []
    box = Box({"list_triage": {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}})
    a = Announcer(tasks, grants, box, sent.append)
    a.tick(now=1000.0)
    a.tick(now=1300.0)
    assert len(sent) == 1 and "fleet#42" in sent[0] and "task " in sent[0]


def test_edited_issue_is_reannounced_with_a_new_code(stores):
    tasks, grants = stores
    sent = []
    listing = {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}
    box = Box({"list_triage": lambda a: listing})
    ann = Announcer(tasks, grants, box, sent.append)
    ann.tick(now=1.0)
    listing["issues"] = [{**ISSUE, "body": "edited", "hash": "d" * 64}]
    ann.tick(now=2.0)
    assert len(sent) == 2 and sent[0].split("code ")[1][:4] != sent[1].split("code ")[1][:4]


def test_box_down_is_reported_once_then_recovery_once(stores):
    tasks, grants = stores
    sent = []
    state = {"r": {"error": "tig-server unreachable: timed out"}}
    ann = Announcer(tasks, grants, Box({"list_triage": lambda a: state["r"]}), sent.append)
    ann.tick(now=1.0)
    ann.tick(now=2.0)
    state["r"] = {"issues": [], "repos_ok": ["fleet"], "errors": []}
    ann.tick(now=3.0)
    assert len(sent) == 2 and "cannot list triage" in sent[0] and "reachable again" in sent[1]


def test_ops_issue_offers_drop_only():
    text = format_announcement({**ISSUE, "labels": ["fleet:triage", "source:doctor", "ops"]}, "7310")
    assert "drop 7310" in text and "task 7310" not in text and "solo 7310" not in text
    assert "needs you on the box" in text


def test_announcement_is_bounded_whatever_the_body():
    text = format_announcement({**ISSUE, "title": "t" * 200, "body": "x" * 50000}, "7310")
    assert len(text) <= 4000
    assert text.count("x") == 1500


def test_task_reply_approves_for_fleet(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "title": "t", "body": "b"}})
    out = handle_task(tasks, "task", code, now=2.0, call=box, repos=())
    assert box.calls == [("approve_task", {"repo": "fleet", "number": 42, "hash": H, "mode": "fleet"})]
    assert "ready for fleet" in out


def test_solo_reply_relabels_then_starts_run_task_with_the_verified_text(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42,
                                "title": "TITLE-FROM-BOX", "body": "BODY-FROM-BOX"},
               "run_task": {"id": "task-20260926-1200-ab12"}})
    out = handle_task(tasks, "solo", code, now=2.0, call=box, repos=("FibonAdithya/fleet",))
    assert [c[0] for c in box.calls] == ["approve_task", "run_task"]
    assert box.calls[0][1]["mode"] == "solo"
    run = box.calls[1][1]
    assert run["repo"] == "FibonAdithya/fleet" and run["minutes"] == 60
    assert "#42" in run["prompt"] and "TITLE-FROM-BOX" in run["prompt"] and "BODY-FROM-BOX" in run["prompt"]
    assert "task-20260926-1200-ab12" in out


def test_solo_refuses_a_repo_run_task_may_not_touch(stores):
    """Checked before relabelling, so a refused solo leaves the issue in triage."""
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({})
    out = handle_task(tasks, "solo", code, now=2.0, call=box, repos=("FibonAdithya/other",))
    assert box.calls == [] and "not allowlisted" in out


def test_box_refusal_is_reported(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"approve_task": {"error": "#42 changed since it was announced; wait for the new announcement"}})
    assert "changed since it was announced" in handle_task(tasks, "task", code, now=2.0, call=box, repos=())


def test_drop_reply_closes(stores):
    tasks, _ = stores
    code = tasks.announce("fleet", "FibonAdithya/fleet", 42, H, now=1.0, avoid=set())
    box = Box({"close_task": {"repo": "fleet", "number": 42, "closed": True}})
    assert handle_task(tasks, "drop", code, now=2.0, call=box, repos=()) == "Dropped fleet#42."
    assert box.calls == [("close_task", {"repo": "fleet", "number": 42})]


def test_unknown_code(stores):
    tasks, _ = stores
    box = Box({})
    assert handle_task(tasks, "task", "0000", now=2.0, call=box, repos=()) == "No announced task with that code."
    assert box.calls == []


def test_announced_codes_avoid_pending_grant_codes(stores, monkeypatch):
    tasks, grants = stores
    import hermes_broker.grants as g
    import hermes_broker.tasks as t
    monkeypatch.setattr(g.secrets, "randbelow", lambda n: 1234)
    grants.create_request("tig-server", 30, "r", now=1.0)
    draws = iter([1234, 5678])
    monkeypatch.setattr(t.secrets, "randbelow", lambda n: next(draws))
    sent = []
    Announcer(tasks, grants, Box({"list_triage": {"issues": [ISSUE], "repos_ok": ["fleet"], "errors": []}}),
              sent.append).tick(now=2.0)
    assert "code 5678" in sent[0]
```

- [ ] **Step 3: Run to verify failure**

Run: `cd broker && uv run --extra dev pytest tests/test_approvals.py tests/test_announcer.py -q 2>&1 | tail -3`
Expected: FAIL (ImportError for `Announcer`; task replies parse to None).

- [ ] **Step 4: Implement in `approvals.py`**

Update the module docstring's list of accepted commands to include `task|solo|drop <4 digits>`. Add the regex beside the others:

```python
_TASK = re.compile(r"^(task|solo|drop)\s+(\d{4})$", re.IGNORECASE)
```

In `parse_command`, before the `_REVOKE` check:

```python
    task = _TASK.match(text)
    if task:
        return (task.group(1).lower(), task.group(2))
```

Add after `handle`:

```python
TASK_VERBS = ("task", "solo", "drop")
ANNOUNCE_EVERY = 300
BODY_CHARS = 1500
SOLO_MINUTES = 60
Call = Callable[..., dict]


def format_announcement(issue: dict, code: str) -> str:
    source = next((x.split(":", 1)[1] for x in issue.get("labels", []) if x.startswith("source:")), "manual")
    head = f"{issue['repo']}#{issue['number']} [{source}] {issue['title'][:200]}"
    body = issue.get("body", "")
    shown = body[:BODY_CHARS] + ("\n[… trimmed]" if len(body) > BODY_CHARS else "")
    if "ops" in issue.get("labels", []):
        tail = f"code {code} — needs you on the box, not an agent.\ndrop {code}"
    else:
        tail = f"code {code}\ntask {code} | solo {code} | drop {code}"
    return f"{head}\n\n{shown}\n\n{tail}"


def _solo_prompt(slug: str, number: int, title: str, body: str) -> str:
    return (f"Fix issue #{number} in {slug}. Open a pull request whose description says `Closes #{number}`.\n\n"
            f"Title: {title}\n\n{body}")


def handle_task(tasks: TaskStore, verb: str, code: str, now: float, call: Call, repos: tuple[str, ...]) -> str:
    """Apply the owner's task/solo/drop reply. The code is spent whatever happens;
    if the issue is still in triage, the next announcer tick offers a new one."""
    entry = tasks.take(code, now)
    if entry is None:
        return "No announced task with that code."
    ref = f"{entry['repo']}#{entry['number']}"
    if verb == "drop":
        r = call("close_task", {"repo": entry["repo"], "number": entry["number"]}, 60)
        return f"{ref}: {r['error']}" if "error" in r else f"Dropped {ref}."
    if verb == "solo" and entry["slug"] not in repos:
        return f"{ref}: {entry['slug']} is not allowlisted for run_task; add it to broker.json repos."
    mode = "fleet" if verb == "task" else "solo"
    r = call("approve_task", {"repo": entry["repo"], "number": entry["number"], "hash": entry["hash"], "mode": mode}, 60)
    if "error" in r:
        return f"{ref}: {r['error']}"
    if mode == "fleet":
        return f"{ref} is ready for fleet. It runs on the next fleet night, or run_fleet('{entry['repo']}')."
    t = call("run_task", {"repo": entry["slug"], "prompt": _solo_prompt(entry["slug"], entry["number"], r["title"], r["body"]),
                          "minutes": SOLO_MINUTES}, 60)
    if "error" in t:
        return f"{ref} moved to fleet:human, but run_task failed: {t['error']}"
    return f"{ref}: run_task {t['id']} started; it opens a PR that closes #{entry['number']}."


class Announcer:
    """Posts each fleet:triage issue once, with a code. Lives in the listener
    because getUpdates is single-consumer and this bot is the owner's only channel."""

    def __init__(self, tasks: TaskStore, grants: GrantStore, call: Call, send: Callable[[str], None]) -> None:
        self.tasks, self.grants, self.call, self.send = tasks, grants, call, send
        self.box_down = False

    def tick(self, now: float) -> None:
        r = self.call("list_triage", {}, 120)
        if "error" in r:
            if not self.box_down:
                self.send(f"Task announcer: cannot list triage on tig-server: {r['error']}")
                self.box_down = True
            return
        if self.box_down:
            self.send("Task announcer: tig-server reachable again.")
            self.box_down = False
        for err in r.get("errors", []):
            logger.warning("list_triage: %s", err)
        issues = r.get("issues", [])
        self.tasks.prune(set(r.get("repos_ok", [])), {(i["repo"], i["number"]) for i in issues}, now)
        for issue in issues:
            code = self.tasks.announce(issue["repo"], issue["slug"], issue["number"], issue["hash"], now,
                                       avoid=self.grants.pending_codes(now))
            if code:
                self.send(format_announcement(issue, code))
```

Add imports: `from typing import Callable` and `from .tasks import TaskStore`.

Change `run_listener`:

```python
def run_listener(
    store: GrantStore,
    token: str,
    owner_id: int,
    poll_seconds: int = 30,
    tasks: TaskStore | None = None,
    call: Call | None = None,
    repos: tuple[str, ...] = (),
) -> None:
    offset = 0
    last_announce = 0.0
    with httpx.Client() as client:
        announcer = None
        if tasks is not None and call is not None:
            announcer = Announcer(tasks, store, call, lambda text: _send(client, token, owner_id, text))
        while True:
            # ... existing getUpdates block unchanged ...
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                parsed = parse_command(update, owner_id)
                if parsed is None:
                    continue
                verb, arg = parsed
                if verb in TASK_VERBS:
                    if tasks is None or call is None:
                        continue
                    reply = handle_task(tasks, verb, arg, time.time(), call, repos)
                else:
                    reply = handle(store, verb, arg, time.time())
                _send(client, token, owner_id, reply)
            now = time.time()
            if announcer is not None and now - last_announce >= ANNOUNCE_EVERY:
                last_announce = now
                try:
                    announcer.tick(now)
                except Exception:
                    # The announcer must never take grant approval down with it.
                    logger.exception("announcer tick failed")
```

Keep the existing `getUpdates` try/except block exactly as it is; only the loop body after it changes.

`broker/hermes_broker/listen.py`:

```python
from . import box
from .tasks import TaskStore
...
    cfg = load_config(Path(os.environ["HERMES_BROKER_CONFIG"]))
    target = cfg.ssh_targets["tig-server"]
    run_listener(
        GrantStore(cfg.state_dir / "grants.json"),
        cfg.approvals_bot_token,
        cfg.owner_telegram_id,
        tasks=TaskStore(cfg.state_dir / "tasks.json"),
        call=lambda verb, args, timeout=60: box.call(target, verb, args, timeout=timeout),
        repos=cfg.repos,
    )
```

- [ ] **Step 5: Run the broker suite**

Run: `cd broker && uv run --extra dev pytest -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 6: Mutation-check**

- `handle_task`: move the allowlist check after `approve_task` → `test_solo_refuses_a_repo_run_task_may_not_touch` fails (calls not empty).
- `handle_task` solo: build the prompt from the stored entry instead of `r["title"]`/`r["body"]` — not detectable by tests since the stored entry has no text; confirm by reading that `TaskStore` stores no title/body, so the only text available is the box-verified `r`. No mutation needed; note it in the commit.
- `Announcer.tick`: remove the `if not self.box_down` guard → `test_box_down_is_reported_once_then_recovery_once` fails.
- `format_announcement`: drop `[:BODY_CHARS]` → `test_announcement_is_bounded_whatever_the_body` fails.
- `Announcer.tick`: pass `avoid=set()` → `test_announced_codes_avoid_pending_grant_codes` fails.

- [ ] **Step 7: Commit**

```bash
git add broker/hermes_broker/approvals.py broker/hermes_broker/listen.py broker/tests/test_approvals.py broker/tests/test_announcer.py
git status --short
git commit -m "feat(broker): announce fleet triage; owner replies task, solo, or drop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: `file_task` MCP tool

**Files:**
- Modify: `broker/hermes_broker/server.py`
- Test: `broker/tests/test_file_task.py`

**Interfaces:**
- Consumes: Task 3's `file_task` verb.
- Produces: MCP tool `file_task(repo: str, title: str, body: str, area: str, cls: str = "patch") -> str`, ungated.

- [ ] **Step 1: Write the failing test**

`broker/tests/test_file_task.py`:

```python
"""file_task is ungated and can only produce a triage issue."""
import pytest


@pytest.fixture
def server(monkeypatch):
    from hermes_broker import server as s
    calls = []
    monkeypatch.setattr(s, "_target", lambda box: "tig-server")
    monkeypatch.setattr(s.box, "call", lambda t, v, a, timeout=60: calls.append((v, a)) or
                        {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 9,
                         "url": "https://github.com/FibonAdithya/fleet/issues/9"})
    return s, calls


def test_file_task_needs_no_grant_and_passes_only_its_arguments(server):
    s, calls = server
    out = s.file_task("fleet", "t", "b", "daemon", "investigation")
    assert calls == [("file_task", {"repo": "fleet", "title": "t", "body": "b", "area": "daemon", "cls": "investigation"})]
    assert "fleet#9" in out and "owner" in out


def test_the_tool_cannot_reach_the_owner_only_verbs():
    """Catches an MCP tool being added for approve_task or close_task."""
    import inspect
    from hermes_broker import server as s
    src = inspect.getsource(s)
    assert '"approve_task"' not in src and '"close_task"' not in src
```

- [ ] **Step 2: Run to verify failure**

Run: `cd broker && uv run --extra dev pytest tests/test_file_task.py -q 2>&1 | tail -3`
Expected: FAIL, no attribute `file_task`.

- [ ] **Step 3: Implement** — in `server.py`, after `add_repo`:

```python
@mcp.tool()
def file_task(repo: str, title: str, body: str, area: str, cls: str = "patch") -> str:
    """File a task for fleet in repo's backlog. No grant needed: it lands in triage and
    nothing runs until the owner replies to its announcement in the approvals chat.

    area must be one of the repo's areas (its docs/agent/ownership.md); cls is one of
    patch, spec, investigation, integration. Write the body as the brief an agent
    will work from: what is wrong, where (file:line), and how to tell it is fixed.
    """
    r = box.call(_target("tig-server"), "file_task",
                 {"repo": repo, "title": title, "body": body, "area": area, "cls": cls}, timeout=120)
    if "error" in r:
        return r["error"]
    return (f"Filed {r['repo']}#{r['number']} in triage: {r['url']}\n"
            f"The owner is sent it within 5 minutes and replies task, solo or drop.")
```

- [ ] **Step 4: Run, mutation-check, commit**

Run: `cd broker && uv run --extra dev pytest -q 2>&1 | tail -3` → all pass.
Mutation: add `require_grant(_store(), "tig-server", now=time.time())` at the top of `file_task` → `test_file_task_needs_no_grant_and_passes_only_its_arguments` fails with `Locked` (the fixture stubs no store). Revert.

```bash
git add broker/hermes_broker/server.py broker/tests/test_file_task.py
git commit -m "feat(broker): file_task tool -- Hermes files fleet tasks into triage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7: fleet-doctor

**Files:**
- Create: `box/doctor.py`
- Create: `box/verbs/doctor`
- Create: `box/units/fleet-doctor.service`, `box/units/fleet-doctor.timer`
- Create: `box/tests/fixtures/ops/*.txt` (captured messages)
- Modify: `box/boxlib.py` (`read_status` adds `doctor` per night)
- Modify: `box/verbs/status` (adds `doctor_unit`)
- Modify: `broker/hermes_broker/server.py` (`night_status` renders both)
- Test: `box/tests/test_doctor.py`, `broker/tests/test_gating.py` (night_status rendering)

**Interfaces:**
- Consumes: Task 3's `tasklib.file_issue`, `ensure_label`, `open_issues_with_label`, `comment`, `slug`; `boxlib.read_status`, `nights_root`.
- Produces: `doctor.Finding(kind, key, area, suggested, ops, evidence)`; `doctor.examine(night: dict, night_dir: Path, repo_dir: Path) -> list[Finding]`; `doctor.signature(kind, key, fleet_sha) -> str`; `doctor.run(home: Path, gh) -> dict`; `~/nights/<id>/doctor.json` = `{"filed": [int], "commented": [int], "skipped": int}`; `~/nights/<id>/doctored` marker.

- [ ] **Step 1: Capture real ops messages**

The patterns must match what the tools actually print. On the laptop, capture each into `box/tests/fixtures/ops/` (one file per case, raw stderr):

```bash
mkdir -p box/tests/fixtures/ops
env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN HOME=$(mktemp -d) claude -p hi > box/tests/fixtures/ops/claude-not-logged-in.txt 2>&1
env -u OPENAI_API_KEY -u CODEX_API_KEY HOME=$(mktemp -d) codex exec hi > box/tests/fixtures/ops/codex-not-logged-in.txt 2>&1 </dev/null
GH_CONFIG_DIR=$(mktemp -d) GH_TOKEN= gh api user > box/tests/fixtures/ops/gh-not-logged-in.txt 2>&1
dd if=/dev/zero of=/dev/full bs=1 count=1 2> box/tests/fixtures/ops/disk-full.txt
herdr --remote nonexistent-host-for-capture status > box/tests/fixtures/ops/herdr-unreachable.txt 2>&1
wc -c box/tests/fixtures/ops/*.txt
```

The `env -u` strips any API key from the environment: with one set, `claude -p` or `codex exec` would make a real call and capture a reply instead of the not-logged-in message. Read every captured file before committing it and remove anything that is not the tool's own error text (a local path or username is fine; a token is not).

Expected: five non-empty files. If any command is absent on the laptop, run it on the box over `ssh adi@tig-server` instead and copy the output by hand (files are tiny). If a message cannot be captured anywhere, leave that file out and remove its pattern from `OPS_PATTERNS` below: an uncaptured pattern is a guess, and the spec's Review Focus #1 is exactly this. Record in the commit message which were captured where.

Write each `OPS_PATTERNS` regex below to match its captured file; adjust the literal text to what was captured. The first matching pattern wins, and `gh`'s message may itself say "not logged in", so make each regex specific to its own tool's wording. The parametrized test asserts `f.key == path.stem`, so a pattern that claims another tool's message fails it.

- [ ] **Step 2: Write the failing tests**

`box/tests/test_doctor.py`:

```python
"""The doctor turns a broken fleet night into one triage issue on FibonAdithya/fleet."""
import json
from pathlib import Path

import pytest

import doctor
from fakegh import FakeGh

OPS = Path(__file__).parent / "fixtures" / "ops"
NIGHT = "fleet-20260926-2300-ab12"


def make_night(home, status="failed", log="", runlog=None, repo="fleet-fixture", night=NIGHT):
    d = home / "nights" / night
    d.mkdir(parents=True)
    if status is not None:
        (d / "status").write_text(status + "\n")
    (d / "meta.json").write_text(json.dumps({"kind": "fleet", "repo": repo, "started_at": 1.0}))
    if log is not None:
        (d / "log").write_text(log)
    r = home / "TIG" / repo
    r.mkdir(parents=True, exist_ok=True)
    (r / "fleet.toml").write_text(f'[repo]\nslug = "FibonAdithya/{repo}"\n')
    if runlog is not None:
        (r / ".fleet").mkdir(exist_ok=True)
        (r / ".fleet" / "run.jsonl").write_text("".join(json.dumps(e) + "\n" for e in runlog))
    return d


def fleet_repo(home, sha="1" * 40):
    r = home / "TIG" / "fleet"
    r.mkdir(parents=True, exist_ok=True)
    (r / "fleet.toml").write_text('[repo]\nslug = "FibonAdithya/fleet"\n')
    stamp = home / ".local" / "share" / "fleet-deployed.sha"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(sha)


def gh_for_fleet():
    gh = FakeGh()
    gh.labels |= {"area:daemon", "area:cost", "area:executor", "class:investigation"}
    return gh


def entry(run_id, kind, **fields):
    return {"run_id": run_id, "intent": {"kind": kind, **fields}, "outcome": "applied", "cost_usd": 0.0}


CRASH = ('fleet run: run_id=r1 (new run) spent=$0.00 socket=/x\nTraceback (most recent call last):\n'
         '  File "/home/adi/TIG/fleet/src/fleet/daemon.py", line 168, in tick\n'
         'fleet.ledger.LedgerError: unknown area: \'allocator\'\n')


def test_a_failed_night_that_ticked_is_s1(tmp_path):
    d = make_night(tmp_path, log=CRASH, runlog=[entry("r1", "Dispatch", task=1, role="implementer", base="x")])
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.suggested, f.area, f.ops) == ("S1", "solo", "daemon", False)
    assert "LedgerError" in f.key and "daemon.py" in f.key and "168" not in f.key


def test_a_night_that_never_ticked_is_s2(tmp_path):
    d = make_night(tmp_path, log=CRASH, runlog=[])
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.suggested) == ("S2", "solo")


def test_a_night_with_no_log_and_no_runlog_is_s2_not_a_crash(tmp_path):
    d = make_night(tmp_path, status=None, log=None, runlog=None)
    [f] = doctor.examine({"id": NIGHT, "status": "unknown"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert f.kind == "S2" and "no-log" in f.key


def test_an_idle_night_killed_at_its_hour_limit_is_healthy(tmp_path):
    """`fleet run` loops until RuntimeMaxSec's SIGTERM, so every fleet night ends
    `killed`, and an idle backlog writes no run-log entries (the run log records
    intents, not ticks). Catches treating either as a fault: the doctor would file
    on every normal night."""
    d = make_night(tmp_path, status="killed", log="fleet run: run_id=r1 (new run) spent=$0.00 socket=/x\n", runlog=[])
    assert doctor.examine({"id": NIGHT, "status": "killed"}, d, tmp_path / "TIG" / "fleet-fixture") == []


def test_a_night_that_died_before_starting_is_s2(tmp_path):
    """A broken fleet.toml fails in load_config, before `fleet run:` is printed.
    Catches keying S2 only on an empty run log: with no run id there is no run log to read."""
    log = ('Traceback (most recent call last):\n'
           '  File "/home/adi/TIG/fleet/src/fleet/config.py", line 160, in load_config\n'
           'fleet.config.ConfigError: slots must be an integer\n')
    d = make_night(tmp_path, status="failed", log=log, runlog=None)
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.key) == ("S2", "no-start|ConfigError|config.py:load_config")


def test_a_drain_is_s3_for_the_owner(tmp_path):
    log = [entry("r1", "Drain", reason="UnpricedModel: claude-opus-9 has no [prices] row")]
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n", runlog=log)
    [f] = doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.suggested, f.area) == ("S3", "task", "cost")
    assert "invariant 5" in "\n".join(f.evidence)


def test_three_releases_for_one_reason_is_s4_two_is_not(tmp_path):
    rel = [entry("r1", "Release", task=n, reason="no_agent", attempt=1) for n in (1, 2, 3)]
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n", runlog=rel)
    [f] = doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert (f.kind, f.key, f.area) == ("S4", "Release:no_agent", "executor")
    d2 = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n",
                    runlog=rel[:2], night="fleet-20260927-2300-cd34", repo="other")
    assert doctor.examine({"id": "fleet-20260927-2300-cd34", "status": "done"}, d2, tmp_path / "TIG" / "other") == []


def test_other_runs_in_the_same_runlog_are_ignored(tmp_path):
    rel = [entry("old", "Release", task=n, reason="stalled", attempt=1) for n in (1, 2, 3)]
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n",
                   runlog=rel + [entry("r1", "Dispatch", task=1, role="implementer", base="x")])
    assert doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture") == []


def test_a_clean_night_has_no_findings(tmp_path):
    d = make_night(tmp_path, status="done", log="fleet run: run_id=r1 (new run)\n",
                   runlog=[entry("r1", "Dispatch", task=1, role="implementer", base="x")])
    assert doctor.examine({"id": NIGHT, "status": "done"}, d, tmp_path / "TIG" / "fleet-fixture") == []


def test_every_ops_pattern_has_a_captured_message_and_every_capture_a_pattern():
    """Catches a pattern written from memory (no capture), and an empty fixtures
    directory, which would make the parametrized test below collect nothing and pass."""
    assert sorted(p.stem for p in OPS.glob("*.txt")) == sorted(name for name, _ in doctor.OPS_PATTERNS)
    assert doctor.OPS_PATTERNS


@pytest.mark.parametrize("path", sorted(OPS.glob("*.txt")), ids=lambda p: p.stem)
def test_captured_ops_messages_are_ops(tmp_path, path):
    d = make_night(tmp_path, log="fleet run: run_id=r1 (new run)\n" + path.read_text())
    [f] = doctor.examine({"id": NIGHT, "status": "failed"}, d, tmp_path / "TIG" / "fleet-fixture")
    assert f.ops and f.kind == "ops" and f.key == path.stem


def test_signature_depends_on_the_deployed_sha():
    assert doctor.signature("S1", "k", "1" * 40) != doctor.signature("S1", "k", "2" * 40)
    assert len(doctor.signature("S1", "k", "1" * 40)) == 8


def test_run_files_once_marks_the_night_and_then_comments_on_a_repeat(tmp_path):
    fleet_repo(tmp_path)
    make_night(tmp_path, log=CRASH, runlog=[])
    gh = gh_for_fleet()
    out = doctor.run(tmp_path, gh)
    assert out["filed"] == [1] and len(gh.issues) == 1
    issue = gh.issues[1]
    names = {x["name"] for x in issue["labels"]}
    assert {"fleet:triage", "source:doctor", "class:investigation", "area:daemon"} <= names
    assert any(n.startswith("doctor:") for n in names)
    assert (tmp_path / "nights" / NIGHT / "doctored").exists()
    assert json.loads((tmp_path / "nights" / NIGHT / "doctor.json").read_text())["filed"] == [1]
    assert doctor.run(tmp_path, gh)["filed"] == []           # marker honoured
    make_night(tmp_path, log=CRASH, runlog=[], night="fleet-20260927-2300-cd34")
    out = doctor.run(tmp_path, gh)
    assert out["filed"] == [] and out["commented"] == [1] and len(gh.issues[1]["comments"]) == 1


def test_a_new_deployed_sha_files_a_new_issue(tmp_path):
    fleet_repo(tmp_path, sha="1" * 40)
    make_night(tmp_path, log=CRASH, runlog=[])
    gh = gh_for_fleet()
    doctor.run(tmp_path, gh)
    fleet_repo(tmp_path, sha="2" * 40)
    make_night(tmp_path, log=CRASH, runlog=[], night="fleet-20260927-2300-cd34")
    assert doctor.run(tmp_path, gh)["filed"] == [2]


def test_at_most_three_new_issues_per_run(tmp_path):
    fleet_repo(tmp_path)
    for n in range(5):
        make_night(tmp_path, log=CRASH.replace("LedgerError", f"Error{n}"), runlog=[],
                   night=f"fleet-2026092{n}-2300-ab1{n}")
    gh = gh_for_fleet()
    out = doctor.run(tmp_path, gh)
    assert len(out["filed"]) == 3 and out["skipped"] == 2
    assert all((tmp_path / "nights" / f"fleet-2026092{n}-2300-ab1{n}" / "doctored").exists() for n in range(5))


def test_running_and_non_fleet_nights_are_left_alone(tmp_path, monkeypatch):
    fleet_repo(tmp_path)
    make_night(tmp_path, status="running", log=CRASH, runlog=[])
    make_night(tmp_path, status="failed", log=CRASH, runlog=[], night="talos-20260926-2300-ab12")
    gh = gh_for_fleet()
    import boxlib
    monkeypatch.setattr(boxlib, "unit_active", lambda night_id: True)  # the running night's unit is alive
    assert doctor.run(tmp_path, gh)["filed"] == []
    assert not (tmp_path / "nights" / NIGHT / "doctored").exists()
```

- [ ] **Step 3: Run to verify failure**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest tests/test_doctor.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'doctor'`.

- [ ] **Step 4: Implement `box/doctor.py`**

```python
"""fleet-doctor: turn a broken fleet night into one triage issue on FibonAdithya/fleet.

Deterministic on purpose: nothing here asks a model whether or what to file, so
log content can shape only the quoted evidence. Faults in fleet, not in the task
it was working on: a hard task reaching NEEDS_HUMAN is fleet working.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import boxlib
import tasklib

MAX_NEW_ISSUES = 3
EVIDENCE_LINES = 60
S4_THRESHOLD = 3
LOG_TAIL_BYTES = 256 * 1024
TERMINAL = ("done", "failed", "killed", "skipped", "paused", "stale", "unknown", "unreadable")
# Not `killed`: `fleet run` loops until RuntimeMaxSec's SIGTERM, so that is how
# every fleet night ends. A daemon that crashes exits non-zero first and reads `failed`.
BROKEN = ("failed", "stale", "unknown", "unreadable")

# One per captured file in tests/fixtures/ops/, named by its stem. Write each
# regex from the captured text (plan Task 7 Step 1), never from memory.
OPS_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("claude-not-logged-in", re.compile(r"(?i)not logged in|/login")),
    ("codex-not-logged-in", re.compile(r"(?i)codex.*(log ?in|auth)")),
    ("gh-not-logged-in", re.compile(r"(?i)gh auth login")),
    ("disk-full", re.compile(r"(?i)no space left on device")),
    ("herdr-unreachable", re.compile(r"(?i)herdr.*(connect|unreachable|refused)")),
)
RUN_ID_RE = re.compile(r"fleet run: run_id=(\S+)")
EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Refused|Exit)\w*)\b")
FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')
DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class Finding:
    kind: str
    key: str
    area: str
    suggested: str
    ops: bool = False
    evidence: tuple[str, ...] = field(default=())


def signature(kind: str, key: str, fleet_sha: str) -> str:
    return hashlib.sha256(f"{kind}|{key}|{fleet_sha}".encode()).hexdigest()[:8]


def _tail(path: Path) -> list[str] | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None


def _crash_key(lines: list[str]) -> str:
    exc, frame = "no-exception", "no-frame"
    for i in range(len(lines) - 1, -1, -1):
        m = EXC_RE.match(lines[i])
        if m:
            exc = m.group(1).rsplit(".", 1)[-1]
            for j in range(i - 1, -1, -1):
                f = FRAME_RE.match(lines[j])
                if f:
                    frame = f"{Path(f.group(1)).name}:{f.group(2)}"
                    break
            break
    return f"{exc}|{frame}"


def _runlog(repo_dir: Path, run_id: str | None) -> list[dict]:
    if run_id is None:
        return []
    out = []
    for line in (_tail(Path(repo_dir) / ".fleet" / "run.jsonl") or []):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("run_id") == run_id:
            out.append(e)
    return out


def examine(night: dict, night_dir: Path, repo_dir: Path) -> list[Finding]:
    lines = _tail(Path(night_dir) / "log")
    evidence = tuple((lines or [])[-EVIDENCE_LINES:])
    text = "\n".join(lines or [])
    for name, pat in OPS_PATTERNS:
        if pat.search(text):
            return [Finding("ops", name, "daemon", "drop", True, evidence)]
    ids = RUN_ID_RE.findall(text)
    entries = _runlog(repo_dir, ids[-1] if ids else None)
    if lines is None:
        return [Finding("S2", "no-log", "daemon", "solo", False, ())]
    if night["status"] in BROKEN:
        # The run log records intents, not ticks, so an empty one on a healthy
        # night only means an idle backlog. It is evidence only once the night broke.
        if not ids:
            return [Finding("S2", f"no-start|{_crash_key(lines)}", "daemon", "solo", False, evidence)]
        if not entries:
            return [Finding("S2", f"no-tick|{_crash_key(lines)}", "daemon", "solo", False, evidence)]
        return [Finding("S1", f"{night['status']}|{_crash_key(lines)}", "daemon", "solo", False, evidence)]
    found = []
    drains = sorted({DIGITS.sub("N", e["intent"].get("reason", "")) for e in entries if e["intent"].get("kind") == "Drain"})
    for reason in drains:
        note = ("fleet AGENTS.md invariant 5: an unpriced transcript is never fixed by adding a [prices] row. "
                "This finding is for the owner.")
        found.append(Finding("S3", reason, "cost", "task", False, (note, *evidence)))
    counts = collections.Counter((e["intent"]["kind"], e["intent"].get("reason", "")) for e in entries
                                 if e["intent"].get("kind") in ("Release", "Escalate"))
    for (kind, reason), n in sorted(counts.items()):
        if n >= S4_THRESHOLD:
            found.append(Finding("S4", f"{kind}:{reason}", "executor", "task", False,
                                 (f"{n} x {kind}({reason}) in run {ids[-1]}", *evidence)))
    return found


def _body(night_id: str, fleet_sha: str, f: Finding) -> str:
    fence = "\n".join(f.evidence).replace("```", "'''")
    return (f"fleet-doctor found `{f.kind}` in night `{night_id}` on fleet `{fleet_sha[:12]}`.\n\n"
            f"Key: `{f.key}`\nSuggested reply: `{f.suggested}`\n\n```\n{fence}\n```\n")


def run(home: Path, gh: tasklib.Gh) -> dict:
    home = Path(home)
    root = boxlib.nights_root({"HOME": str(home)})
    fleet_dir = home / "TIG" / "fleet"
    fleet_slug = tasklib.slug(fleet_dir)
    stamp = home / ".local" / "share" / "fleet-deployed.sha"
    fleet_sha = stamp.read_text().strip() if stamp.is_file() else "unknown"
    areas = frozenset({"daemon", "cost", "executor"})
    total = {"filed": [], "commented": [], "skipped": 0}
    for night in boxlib.read_status(root):
        d = root / night["id"]
        if not night["id"].startswith("fleet-") or night["status"] not in TERMINAL or (d / "doctored").exists():
            continue
        try:
            repo = json.loads(boxlib.read_small(d / "meta.json", 65536) or "{}").get("repo", "")
            repo_dir = home / "TIG" / boxlib._name(repo)
        except (ValueError, json.JSONDecodeError):
            repo_dir = home / "TIG" / "nonexistent"
        mine = {"filed": [], "commented": [], "skipped": 0}
        for f in examine(night, d, repo_dir):
            sig = signature(f.kind, f.key, fleet_sha)
            label = f"doctor:{sig}"
            tasklib.ensure_label(gh, fleet_slug, label)
            existing = tasklib.open_issues_with_label(gh, fleet_slug, label)
            if existing:
                tasklib.comment(gh, fleet_slug, existing[0]["number"], f"Seen again in night `{night['id']}`.")
                mine["commented"].append(existing[0]["number"])
                continue
            if len(total["filed"]) >= MAX_NEW_ISSUES:
                mine["skipped"] += 1
                continue
            extra = (label, "ops") if f.ops else (label,)
            out = tasklib.file_issue(gh, fleet_slug, f"[doctor] {f.kind} {f.key}"[:200], _body(night["id"], fleet_sha, f),
                                     f.area, "investigation", areas, "source:doctor", extra)
            mine["filed"].append(out["number"])
            total["filed"].append(out["number"])
        (d / "doctor.json").write_text(json.dumps(mine))
        (d / "doctored").write_text("")
        total["commented"] += mine["commented"]
        total["skipped"] += mine["skipped"]
    return total
```

Note on `areas`: the doctor files only to fleet's `daemon`, `cost` and `executor` areas, which Task 2 creates. `file_issue` checks against this set instead of calling `fleet_areas` so the doctor does not need `uv` to classify its own findings.

Replace the placeholder `OPS_PATTERNS` regexes with ones written against Step 1's captured files, and delete any pattern whose file was not captured.

`box/verbs/doctor`:

```python
#!/usr/bin/env python3
"""Examine finished fleet nights and file faults on FibonAdithya/fleet. Run hourly by fleet-doctor.timer."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402
import doctor  # noqa: E402
import tasklib  # noqa: E402

boxlib.read_json_stdin(sys.stdin.buffer)
try:
    out = doctor.run(Path(os.environ.get("HOME", "/home/adi")), tasklib.real_gh)
except (ValueError, TypeError, OSError) as exc:
    sys.exit(boxlib.refuse(f"doctor failed: {exc}"))
print(json.dumps(out))
```

`box/units/fleet-doctor.service`:

```ini
[Unit]
Description=fleet-doctor: file faults from finished fleet nights

[Service]
Type=oneshot
Environment=PATH=%h/.npm-global/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh -c 'echo {} | %h/TIG/hermes-harness/box/verbs/doctor'
```

`box/units/fleet-doctor.timer`:

```ini
[Unit]
Description=fleet-doctor hourly

[Timer]
OnCalendar=hourly
Persistent=false

[Install]
WantedBy=timers.target
```

`install.sh` already copies every unit and never enables timers; the doctor timer is enabled in Task 9.

- [ ] **Step 5: Surface doctor results in `night_status`**

In `boxlib.read_status`, before `rows.append`, read the summary and add it to the row:

```python
        doc_raw = read_small(d / "doctor.json")
        try:
            doctor_summary = json.loads(doc_raw) if doc_raw else None
        except json.JSONDecodeError:
            doctor_summary = None
```

and add `"doctor": doctor_summary,` to the row dict.

In `box/verbs/status`, add the unit's state:

```python
import subprocess  # noqa: E402
failed = subprocess.run(["systemctl", "--user", "is-failed", "fleet-doctor.service"],
                        capture_output=True, text=True, check=False).stdout.strip() == "failed"
print(json.dumps({"nights": boxlib.read_status(boxlib.nights_root(os.environ)),
                  "doctor_unit": "failed" if failed else "ok"}))
```

In `broker/hermes_broker/server.py` `night_status`, render them:

```python
    lines = []
    for n in rows:
        line = f"{n['id']}  {n['status']:8} started {n['started']}  exit={n['exit_code']}"
        doc = n.get("doctor")
        if doc and (doc.get("filed") or doc.get("commented") or doc.get("skipped")):
            line += (f"  doctor: filed {doc.get('filed', [])} commented {doc.get('commented', [])}"
                     f" skipped {doc.get('skipped', 0)}")
        lines.append(line)
    if r.get("doctor_unit") == "failed":
        lines.append("fleet-doctor: last run FAILED; check `journalctl --user -u fleet-doctor` on the box")
    return "\n".join(lines)
```

(keeping the existing `if not rows: return "no nights recorded"` guard, extended to also report a failed doctor unit.)

Add to `box/tests/test_doctor.py`:

```python
def test_status_rows_carry_the_doctor_summary(tmp_path):
    import boxlib
    d = make_night(tmp_path, status="done", log="")
    (d / "doctor.json").write_text(json.dumps({"filed": [4], "commented": [], "skipped": 1}))
    [row] = boxlib.read_status(tmp_path / "nights")
    assert row["doctor"] == {"filed": [4], "commented": [], "skipped": 1}
```

Add to `broker/tests/test_gating.py`, following its existing `night_status` test's fixture:

```python
def test_night_status_shows_doctor_results_and_a_failed_doctor(monkeypatch):
    from hermes_broker import server
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: {
        "nights": [{"id": "fleet-20260926-2300-ab12", "status": "failed", "started": "2026-09-26T23:00:00Z",
                    "exit_code": 1, "doctor": {"filed": [4], "commented": [], "skipped": 0}}],
        "doctor_unit": "failed"})
    out = server.night_status()
    assert "doctor: filed [4]" in out and "fleet-doctor: last run FAILED" in out
```

- [ ] **Step 6: Run both suites**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest -q 2>&1 | tail -3 && cd ../broker && uv run --extra dev pytest -q 2>&1 | tail -3`
Expected: all pass. The existing `test_status_verb_lists_nights` in `box/tests/test_nights.py` may assert the exact row dict or the exact top-level keys; update it to include `"doctor": None` and `"doctor_unit"`, and say so in the commit.

- [ ] **Step 7: Mutation-check**

- `examine`: change `S4_THRESHOLD` to 2 → `test_three_releases_for_one_reason_is_s4_two_is_not` fails.
- `_runlog`: drop the `run_id` filter → `test_other_runs_in_the_same_runlog_are_ignored` fails.
- `run`: skip writing `doctored` → `test_run_files_once_marks_the_night_and_then_comments_on_a_repeat` fails.
- `run`: write `doctored` only when something was filed → `test_at_most_three_new_issues_per_run` fails.
- `signature`: drop `fleet_sha` from the hash → `test_a_new_deployed_sha_files_a_new_issue` and `test_signature_depends_on_the_deployed_sha` fail.
- `_crash_key`: keep the line number → `test_a_failed_night_that_ticked_is_s1` fails.
- `examine`: check `entries` before `lines is None` → `test_a_night_with_no_log_and_no_runlog_is_s2_not_a_crash` fails on the key.
- `BROKEN`: add `killed` back → `test_an_idle_night_killed_at_its_hour_limit_is_healthy` fails.
- `examine`: move the `not entries` S2 check above the `BROKEN` check → `test_an_idle_night_killed_at_its_hour_limit_is_healthy` fails.
- `examine`: delete the `not ids` branch → `test_a_night_that_died_before_starting_is_s2` fails on the key (`no-tick|…`).
- Delete one file from `tests/fixtures/ops/` → `test_every_ops_pattern_has_a_captured_message_and_every_capture_a_pattern` fails.

- [ ] **Step 8: Commit**

```bash
git add box/doctor.py box/verbs/doctor box/units/fleet-doctor.service box/units/fleet-doctor.timer box/tests/fixtures/ops box/tests/test_doctor.py box/boxlib.py box/verbs/status box/tests/test_nights.py broker/hermes_broker/server.py broker/tests/test_gating.py
chmod +x box/verbs/doctor
git status --short
git commit -m "feat(box): fleet-doctor files fleet faults from finished nights

Ops patterns captured from: <say which command, which host, per file>.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 8: Deploy target `fleet`

**Files:**
- Modify: `box/verbs/deploy`
- Modify: `broker/hermes_broker/grants.py` (`create_deploy_request`, `approve_deploy`, `take_deploy_grant`, new `deploy_target`)
- Modify: `broker/hermes_broker/approvals.py` (`handle` deploy reply)
- Modify: `broker/hermes_broker/server.py` (`request_deploy`, new `deploy_fleet`)
- Test: `box/tests/test_deploy_fleet.py`, `broker/tests/test_deploy.py`, `broker/tests/test_grants.py`

**Interfaces:**
- Produces: `GrantStore.create_deploy_request(sha, now, target="harness")`; `GrantStore.take_deploy_grant(now, target="harness") -> str | None` (does not consume a grant for another target); `GrantStore.deploy_target() -> str | None`; MCP `request_deploy(sha, target="harness")`, `deploy_fleet()`; box verb `deploy {sha, target}` with `target` ∈ {`harness`, `fleet`}, default `harness`, output `{"deployed": sha, "target": "fleet"}` for fleet (harness output unchanged).

- [ ] **Step 1: Write the failing broker tests**

Append to `broker/tests/test_grants.py`:

```python
SHA_F = "f" * 40


def test_a_fleet_deploy_grant_is_not_a_harness_grant_and_is_not_spent_by_asking(store):
    store.create_deploy_request(SHA_F, now=1.0, target="fleet")
    assert store.approve_deploy(SHA_F[:7], now=2.0) == SHA_F
    assert store.deploy_target() == "fleet"
    assert store.take_deploy_grant(now=3.0) is None            # harness asks: refused, not consumed
    assert store.take_deploy_grant(now=3.0, target="fleet") == SHA_F
    assert store.take_deploy_grant(now=3.0, target="fleet") is None


def test_unknown_deploy_target_rejected(store):
    with pytest.raises(ValueError):
        store.create_deploy_request(SHA_F, now=1.0, target="droplet")
```

Append to `broker/tests/test_deploy.py`:

```python
def test_deploy_fleet_uses_only_a_fleet_grant(fakes, store):
    server, calls, box_reply = fakes
    import time
    store.create_deploy_request(SHA, now=time.time(), target="fleet")
    store.approve_deploy(SHA[:7], now=time.time())
    with pytest.raises(Locked):
        server.deploy_harness()
    box_reply.clear()
    box_reply.update({"deployed": SHA, "target": "fleet"})
    out = server.deploy_fleet()
    assert calls == [("box", "tig-server", "deploy", {"sha": SHA, "target": "fleet"}, 1800)]
    assert out.startswith(f"tig-server: fleet deployed {SHA[:12]}")


def test_request_deploy_names_the_target(fakes, store):
    server, _, _ = fakes
    assert "fleet" in server.request_deploy(SHA, target="fleet")
    assert server.request_deploy(SHA, target="droplet").startswith("target must be")
```

Append to `broker/tests/test_approvals.py`:

```python
def test_fleet_deploy_reply_names_fleet(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "g.json")
    sha = "91a2e066c76837d9ac80243755ebc628993a0454"
    store.create_deploy_request(sha, now=1000.0, target="fleet")
    assert handle(store, "deploy", "91a2e06", now=1002.0) == f"Deploy of fleet {sha} to tig-server approved for 10 min."
```

- [ ] **Step 2: Write the failing box tests**

`box/tests/test_deploy_fleet.py`:

```python
"""deploy target fleet: exactly the approved main commit, never under a running night, never a red one."""
import json
import os
import subprocess
from pathlib import Path

VERB = Path(__file__).resolve().parents[1] / "verbs" / "deploy"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(up, check_rc=0, msg="c"):
    (up / "Makefile").write_text(f"check:\n\t@echo checked {msg}; exit {check_rc}\n")
    git(up, "add", "Makefile")
    git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    git(up, "push", "-q", "origin", "HEAD:main")
    return git(up, "rev-parse", "HEAD")


def setup(tmp_path):
    origin, up, home = tmp_path / "origin.git", tmp_path / "up", tmp_path / "home"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(up)], check=True, capture_output=True)
    git(up, "checkout", "-q", "-b", "main")
    first = commit(up, msg="first")
    box = home / "TIG" / "fleet"
    subprocess.run(["git", "clone", "-q", str(origin), str(box)], check=True)
    stamp = home / ".local" / "share" / "fleet-deployed.sha"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(first)
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "uv").write_text("#!/bin/sh\necho \"uv $*\" >> \"$HOME/uv.log\"\n")
    (shims / "systemctl").write_text("#!/bin/sh\necho \"${SYSTEMCTL_STATE:-inactive}\"\n")
    for s in ("uv", "systemctl"):
        (shims / s).chmod(0o755)
    return up, home, box, first, shims


def run(home, shims, args, **env):
    e = {"HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}", **env}
    p = subprocess.run([str(VERB)], input=json.dumps(args).encode(), env=e, capture_output=True, check=False)
    return p.returncode, json.loads(p.stdout)


def running_night(home, name="fleet-20260926-2300-ab12"):
    d = home / "nights" / name
    d.mkdir(parents=True)
    (d / "status").write_text("running\n")


def test_deploys_a_green_main_commit(tmp_path):
    up, home, box, _, shims = setup(tmp_path)
    want = commit(up, msg="second")
    rc, out = run(home, shims, {"sha": want, "target": "fleet"})
    assert (rc, out) == (0, {"deployed": want, "target": "fleet"})
    assert git(box, "rev-parse", "HEAD") == want
    assert (home / ".local/share/fleet-deployed.sha").read_text() == want
    assert "uv sync --locked --dev" in (home / "uv.log").read_text()


def test_refuses_while_any_fleet_night_runs(tmp_path):
    """Any repo's fleet night: the daemon for fleet-fixture runs from ~/TIG/fleet too."""
    up, home, box, first, shims = setup(tmp_path)
    want = commit(up, msg="second")
    running_night(home)
    rc, out = run(home, shims, {"sha": want, "target": "fleet"}, SYSTEMCTL_STATE="active")
    assert rc == 2 and "fleet-20260926-2300-ab12 is running" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first


def test_refuses_when_main_moved(tmp_path):
    up, home, box, first, shims = setup(tmp_path)
    approved = commit(up, msg="approved")
    commit(up, msg="later")
    rc, out = run(home, shims, {"sha": approved, "target": "fleet"})
    assert rc == 2 and "main is at" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first


def test_a_red_commit_is_rolled_back_to_the_deployed_one(tmp_path):
    up, home, box, first, shims = setup(tmp_path)
    red = commit(up, check_rc=1, msg="red")
    rc, out = run(home, shims, {"sha": red, "target": "fleet"})
    assert rc == 2 and f"make check failed at {red[:12]}" in out["error"] and "checked red" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first
    assert (home / ".local/share/fleet-deployed.sha").read_text() == first


def test_unknown_target_refused(tmp_path):
    _, home, _, first, shims = setup(tmp_path)
    rc, out = run(home, shims, {"sha": first, "target": "droplet"})
    assert rc == 2 and out["error"] == "deploy target must be harness or fleet"
```

- [ ] **Step 3: Run to verify failure**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest tests/test_deploy_fleet.py tests/test_deploy.py -q 2>&1 | tail -3; cd ../broker && uv run --extra dev pytest tests/test_deploy.py tests/test_grants.py tests/test_approvals.py -q 2>&1 | tail -3`
Expected: new tests fail; existing deploy tests pass.

- [ ] **Step 4: Implement the box verb**

Restructure `box/verbs/deploy`: keep the sha check, then branch on target. The harness path is the existing code unchanged, moved into `deploy_harness(home, sha)`. Add:

```python
TARGETS = ("harness", "fleet")
target = a.get("target", "harness")
if target not in TARGETS:
    sys.exit(boxlib.refuse("deploy target must be harness or fleet"))


def deploy_fleet(home: Path, sha: str) -> None:
    repo = home / "TIG" / "fleet"
    stamp = home / ".local" / "share" / "fleet-deployed.sha"

    def g(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)

    root = boxlib.nights_root({"HOME": str(home)})
    for row in boxlib.read_status(root):
        if row["id"].startswith("fleet-") and row["status"] == "running":
            sys.exit(boxlib.refuse(f"{row['id']} is running; deploy fleet after it ends"))
    r = g("fetch", "-q", "origin")
    if r.returncode != 0:
        sys.exit(boxlib.refuse(f"git fetch failed: {r.stderr.strip()[:300]}"))
    main = g("rev-parse", "origin/main").stdout.strip()
    if main != sha:
        sys.exit(boxlib.refuse(f"main is at {main[:12]}, not the approved {sha[:12]}; request a new deploy"))
    previous = stamp.read_text().strip() if stamp.is_file() else g("rev-parse", "HEAD").stdout.strip()

    def install(at: str) -> subprocess.CompletedProcess:
        for args in (("checkout", "-q", "main"), ("reset", "-q", "--hard", at)):
            r = g(*args)
            if r.returncode != 0:
                return r
        r = subprocess.run(["uv", "sync", "--locked", "--dev"], cwd=repo, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            return r
        return subprocess.run(["make", "check"], cwd=repo, capture_output=True, text=True, check=False)

    r = install(sha)
    if r.returncode != 0:
        why = (r.stdout + r.stderr).strip()[-300:]
        install(previous)
        sys.exit(boxlib.refuse(f"make check failed at {sha[:12]}; back on {previous[:12]}: {why}"))
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(sha)
    print(json.dumps({"deployed": sha, "target": "fleet"}))


if target == "fleet":
    deploy_fleet(home, sha)
else:
    deploy_harness(home, sha)
```

`install(previous)` re-runs `make check` on the rollback too; its result is not checked because the previous SHA already passed when it was deployed, and there is nothing further to fall back to. The refusal says which SHA is now checked out.

`boxlib.read_status` calls `systemctl --user is-active` for a `running` night; the test shim answers from `SYSTEMCTL_STATE`.

- [ ] **Step 5: Implement the broker side**

`grants.py`:

```python
DEPLOY_TARGETS = ("harness", "fleet")
```

```python
    def create_deploy_request(self, sha: str, now: float, target: str = "harness") -> None:
        if target not in DEPLOY_TARGETS:
            raise ValueError(f"unknown deploy target: {target}")
        data = self._read()
        data["pending_deploy"] = {"sha": sha, "target": target, "expires_at": now + REQUEST_TTL_SECONDS}
        self._write(data)
```

In `approve_deploy`, carry the target into the grant: `data["grants"][DEPLOY] = {"sha": req["sha"], "target": req.get("target", "harness"), "expires_at": ...}`.

```python
    def deploy_target(self) -> str | None:
        grant = self._read()["grants"].get(DEPLOY)
        return grant.get("target", "harness") if grant else None

    def take_deploy_grant(self, now: float, target: str = "harness") -> str | None:
        """The approved commit, if a deploy grant for this target is live. Consumes it only on a match."""
        data = self._read()
        grant = data["grants"].get(DEPLOY)
        if grant is None or grant.get("target", "harness") != target:
            return None
        data["grants"].pop(DEPLOY)
        self._write(data)
        return grant["sha"] if grant["expires_at"] > now else None
```

`approvals.handle`, deploy branch:

```python
        target = store.deploy_target()
        what = sha if target == "harness" else f"{target} {sha}"
        return f"Deploy of {what} to tig-server approved for {DEPLOY_GRANT_MINUTES} min."
```

`server.py` `request_deploy`:

```python
@mcp.tool()
def request_deploy(sha: str, target: str = "harness") -> str:
    """Ask the owner to approve deploying one commit to the box.

    target is "harness" (hermes-harness master) or "fleet" (fleet main). sha is the
    full 40-character commit id. The owner approves by typing that commit's prefix
    in the approvals chat; the approval covers that commit and target only, for one
    deploy_harness() or deploy_fleet() call within 10 minutes.
    """
    if target not in ("harness", "fleet"):
        return "target must be harness or fleet"
    sha = sha.strip().lower()
    if not _SHA_RE.fullmatch(sha):
        return "sha must be the full 40-character commit id"
    _store().create_deploy_request(sha, now=time.time(), target=target)
    what = "hermes-harness" if target == "harness" else "fleet"
    return (
        f"Requested a deploy of {what} {sha} to tig-server.\n"
        f"Ask the owner to read that commit and reply `deploy {sha[:12]}` "
        f"in the approvals chat within 2 minutes."
    )
```

Check `test_request_deploy_needs_a_full_sha` still passes: it asserts `startswith("sha must be")`, which holds. `test_request_deploy_tells_the_owner_what_to_type` asserts on the harness message; the harness text is unchanged.

New tool after `deploy_harness`:

```python
@mcp.tool()
def deploy_fleet() -> str:
    """Deploy the owner-approved fleet main commit to the box. Needs request_deploy(sha, "fleet") first.

    Refused while any fleet night runs. The box runs make check on the new commit
    and stays on the previous one if it fails.
    """
    sha = _store().take_deploy_grant(now=time.time(), target="fleet")
    if sha is None:
        raise Locked('LOCKED: no approved fleet deploy. Call request_deploy(sha, "fleet") first.')
    r = box.call(_target("tig-server"), "deploy", {"sha": sha, "target": "fleet"}, timeout=1800)
    return r.get("error") or f"tig-server: fleet deployed {sha[:12]}."
```

`deploy_harness` keeps calling `take_deploy_grant(now=...)` (target defaults to harness) and keeps sending `{"sha": sha}`.

- [ ] **Step 6: Run both suites**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest -q 2>&1 | tail -3 && cd ../broker && uv run --extra dev pytest -q 2>&1 | tail -3`
Expected: all pass, including every pre-existing deploy test unchanged.

- [ ] **Step 7: Mutation-check**

- `take_deploy_grant`: pop before the target comparison → `test_a_fleet_deploy_grant_is_not_a_harness_grant_and_is_not_spent_by_asking` fails.
- `deploy_fleet` (verb): delete the running-night loop → `test_refuses_while_any_fleet_night_runs` fails.
- `deploy_fleet` (verb): drop `install(previous)` → `test_a_red_commit_is_rolled_back_to_the_deployed_one` fails.
- `deploy_fleet` (verb): compare against `origin/master` → `test_deploys_a_green_main_commit` fails.

- [ ] **Step 8: Commit**

```bash
git add box/verbs/deploy box/tests/test_deploy_fleet.py broker/hermes_broker/grants.py broker/hermes_broker/approvals.py broker/hermes_broker/server.py broker/tests/test_deploy.py broker/tests/test_grants.py broker/tests/test_approvals.py
git status --short
git commit -m "feat: deploy target fleet -- one approved main commit, never under a night, never red

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 9: Runbook, PR, deployment, and the round trip

Owner-attended. Every step that writes to GitHub, the droplet, or the box is confirmed with the owner first.

**Files:**
- Modify: `runbooks/dispatch.md` (new section "fleet tasks and the doctor")
- Modify: `docs/ai/specs/2026-09-26-fleet-self-healing-design.md` (Status line → as-built notes)

- [ ] **Step 1: Runbook section**

Add to `runbooks/dispatch.md`:

```markdown
## fleet tasks and the doctor

- Hermes files with `file_task(repo, title, body, area, cls)`. The doctor files hourly on its own.
- Every triage issue is sent to the approvals chat within 5 minutes with a 4-digit code.
  - `task NNNN` — fleet runs it (next night, or `run_fleet(repo)`).
  - `solo NNNN` — `run_task` fixes it in a container and opens a PR. Use it when fleet itself is broken.
  - `drop NNNN` — close it.
  - `[doctor] ops ...` issues offer only `drop`: log in on the box, then drop.
- A refused `task`/`solo` means the issue changed after it was sent; a new code follows.
- Before `task` on a `fleet` issue, check it touches no path in fleet's
  `docs/agent/ownership.md` "Non-dispatchable" table. fleet does not enforce that table.
- fleet deploys: merge `integration` → `main`, then Hermes calls `request_deploy(sha, "fleet")`,
  you reply `deploy <prefix>`, Hermes calls `deploy_fleet()`. Refused while a fleet night runs.
- Rollback: revert on `main`, deploy that commit.
- Doctor state: `night_status()` shows per-night `doctor:` results and a failed doctor unit.
```

- [ ] **Step 2: Whole-branch review and PR**

Run: `cd box && uv run --no-project --python 3.12 --with pytest python -m pytest -q && cd ../broker && uv run --extra dev pytest -q`, then push `feat/fleet-self-healing` and open a PR to `master`. Probe the transport first per the owner's standing instructions (`timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=8 -T git@github.com`). Wait for CI with `gh pr checks <n> --watch`; the owner merges.

- [ ] **Step 3: Deploy the box** (after merge)

Hermes or the orchestrator calls `request_deploy(<merge sha>)`; the owner replies `deploy <prefix>`; `deploy_harness()`. Then on the box as `adi`: `systemctl --user list-unit-files | grep fleet-doctor` shows both units, disabled.

- [ ] **Step 4: Deploy fleet on the box for the first time**

`~/TIG/fleet` must be at merged `main` (Tasks 1 and 2). Seed the stamp by hand once, over SSH as `adi`: `cd ~/TIG/fleet && git fetch -q && git switch main && git reset -q --hard origin/main && uv sync --locked --dev && make check && git rev-parse HEAD > ~/.local/share/fleet-deployed.sha`. Every later fleet deploy goes through `deploy_fleet()`.

- [ ] **Step 5: Droplet: config and listener**

On the droplet, add `"FibonAdithya/fleet"` to `repos` in `broker.json`, pull the merged broker, and restart the broker and the approvals listener by hand (the droplet is deploy-by-hand, per PR #14). Confirm: `journalctl --user -u <listener unit> -n 20` shows no traceback, and within 5 minutes either announcements arrive or nothing is in triage.

- [ ] **Step 6: Live checks (spec §7)**

With the owner watching the approvals chat:
1. `file_task("fleet-fixture", "doc typo in README", "README line 3 says 'teh'.", "docs", "patch")` → issue has `fleet:triage` + `source:hermes`, no `fleet:ready`.
2. One announcement arrives; wait 10 minutes; no second.
3. Edit the issue body on GitHub; owner replies `task <old code>` → "changed since it was announced"; a new announcement arrives.
4. Owner replies `task <new code>` → labels are `fleet:ready` + `fleet:auto-ok`; `cd ~/TIG/fleet-fixture && fleet plan` on the box lists a dispatch for it.
5. Forward that announcement to the bot and reply from another account → no effect (covered by unit tests; spot-check one).
6. `file_task("fleet-fixture", "t", "b", "notanarea", "patch")` → refused.
7. Enable the doctor: `systemctl --user enable --now fleet-doctor.timer`.

- [ ] **Step 7: Round trip (spec §7.12)**

1. On a scratch branch of fleet, break `fleet.toml` (e.g. `slots = "three"`), push it, and on the box check it out by hand in `~/TIG/fleet` (not via deploy). This is a deliberate manual deviation; note it.
2. `run_fleet("fleet-fixture", 1)` under a grant → night fails.
3. Run `echo {} | ~/TIG/hermes-harness/box/verbs/doctor` on the box (or wait for the timer) → one `[doctor] S2 ...` issue on `FibonAdithya/fleet`, announced within 5 minutes with "Suggested reply: solo".
4. Restore `~/TIG/fleet` to the deployed SHA by hand (the fix under test is the process, not this typo). Owner replies `solo <code>` → `run_task` starts; its PR appears.
5. Owner reviews and merges or closes the PR, then `request_deploy(<main sha>, "fleet")`, `deploy <prefix>`, `deploy_fleet()`.
6. `run_fleet("fleet-fixture", 1)` → night `done`; next doctor run files nothing for it.

- [ ] **Step 8: Record as-built**

Update the spec's Status line to "built; deployed <date>", and add an "As built" section listing every deviation from this plan with its reason. Commit and PR as in Step 2.
