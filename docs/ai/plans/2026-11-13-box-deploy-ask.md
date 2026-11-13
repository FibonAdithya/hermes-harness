# Box Deploy Ask Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The approvals bot asks the owner, once per master commit, to deploy hermes-harness to tig-server when the box's stamp is behind `origin/master`; the owner's `deploy <prefix>` reply makes the listener run the box `deploy` verb itself.

**Architecture:** A new module `broker/hermes_broker/deploy_ask.py` holds a small state file (`<state_dir>/deploy_ask.json`), the message formatter, a `DeployRunner` (one deploy at a time, in a worker thread) and a `DeployAnnouncer` (one `tick(now)` per `ANNOUNCE_EVERY`, read-only git against the droplet checkout). `GrantStore` gains a TTL and an origin on the pending deploy, a busy check, and an approve-and-consume method that writes no grant. On the box, a new ungated `deployed` verb reads the stamps and `deploy` takes a `flock`. `droplet/harness-pull.sh` defers the approvals restart while a deploy runs.

**Tech Stack:** Python 3 (broker: uv, pytest, httpx; box: stdlib verbs, pytest), bash (`harness-pull.sh`, shellcheck in CI), git.

**Spec:** `docs/ai/specs/2026-11-13-box-deploy-ask-design.md`

## Global Constraints

- The gate stays: nothing is deployed until the owner types the commit prefix. No automatic deploy.
- Settle time: master's commit time must be at least 30 minutes (1800 s) before `now`.
- At most one ask per master commit; the asked sha survives a listener restart (state file, not memory).
- Auto-ask pending TTL is 24 hours (86400 s). `REQUEST_TTL_SECONDS` stays 120 and `DEPLOY_GRANT_MINUTES` stays 10.
- `origin` is `"agent"` (default, also for records with no `origin`) or `"auto"`.
- An auto-ask never displaces an unexpired non-auto `pending_deploy` or any live deploy grant (either target); an agent request may replace an auto-ask.
- Approving an auto request writes no `grants["deploy"]`.
- The box call is `call("deploy", {"sha": sha, "target": "harness"}, 900)`. Replies: `tig-server: deployed <sha12>` or the verb's error text. A second approval while one runs: `a deploy is already running`.
- The announcer's git commands are only `rev-parse`, `log`, `diff`, `merge-base`. It never fetches or writes the checkout.
- `harness_checkout` is a new `BrokerConfig` field, default `~/hermes-harness`.
- `deploy_running` record: `{"sha": ..., "started_at": ...}`; harness-pull treats one younger than 20 minutes (1200 s) as live, older as stale.
- Box `deploy` takes an exclusive non-blocking `flock` on `~/.local/share/deploy.lock` for both targets; refusal text `a deploy is already running`.
- Message: at most 15 commit lines, each cut to 100 characters, then `and N more`; reply line uses a 12-character prefix; plain text through the existing `send`.
- `deployed` failure: warning logged at most once per hour; nothing sent.
- Tests inject fakes for `call`, `send`, the clock and the git runner. Never monkeypatch module defaults.
- Stage explicit paths only; no `git add -A` / `git add .` / `git commit -a`.

## Review Focus

1. **Malformed `deployed` reply** (an old box returns `{}`, or `harness` is a non-hex string or a number): the announcer treats it as a failed read (rate-limited warning, no ask, no pending). A reply of `{"harness": null}` is the null-stamp case, not a failure. Test in Task 7.
2. **Box sha unknown to the droplet checkout** (the box is on a commit the droplet has never fetched, or on a force-pushed branch): `merge-base --is-ancestor` exits 128, not 1. Any nonzero exit means "not an ancestor"; the ask is sent with that line and no commit list, and the tick does not crash. Test in Task 7.
3. **Missing or corrupt `deploy_ask.json`**: `DeployAskState` reads it as empty (the announcer may ask once more, which is safe), and `harness-pull.sh` restarts as usual rather than deferring forever. Tests in Tasks 5 and 9.
4. **Wrong target or wrong prefix against an auto request** (`deploy fleet <prefix>`, or a mistyped prefix): refused with the existing "No pending … deploy" reply, the pending auto request stays, no box call. Tests in Tasks 3 and 6.
5. **The worker's box call fails or raises** (`{"error": "tig-server unreachable: …"}`, or an exception from `call`): the owner still gets a result message, the `deploy_running` record is cleared, and the runner is free, so the next approval is not stuck on "already running". Test in Task 6.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `box/verbs/deployed` | create | ungated verb: print both stamps |
| `box/verbs/deploy` | modify | `flock` around both targets |
| `box/tests/test_deployed.py` | create | verb tests |
| `box/tests/test_deploy.py` | modify | lock test |
| `broker/hermes_broker/grants.py` | modify | TTL/origin on pending deploy, `deploy_slot_busy`, `pending_deploy_origin`, `approve_auto_deploy` |
| `broker/tests/test_grants.py` | modify | store tests |
| `broker/hermes_broker/deploy_ask.py` | create | `DeployAskState`, `format_deploy_ask`, `DeployRunner`, `DeployAnnouncer` |
| `broker/tests/test_deploy_ask.py` | create | state, format, runner, announcer tests |
| `broker/hermes_broker/approvals.py` | modify | `handle`/`_dispatch` route auto approvals to the runner; `run_listener` builds and ticks the announcer |
| `broker/tests/test_approvals.py` | modify | handle-level origin tests |
| `broker/hermes_broker/config.py`, `broker/tests/test_config.py` | modify | `harness_checkout` |
| `broker/hermes_broker/listen.py`, `broker/tests/test_prod.py` | modify | pass `harness_checkout` |
| `droplet/harness-pull.sh` | modify | defer restart while a deploy runs; `uv` from PATH |
| `box/tests/test_harness_pull.py` | create | stubbed-PATH tests of the script |
| `runbooks/dispatch.md` | modify | "Deploys" section |

The spec places `DeployAnnouncer` in `approvals.py`; the spec leaves module layout to the builder, and `approvals.py` is already 511 lines, so it goes in `deploy_ask.py`. `deploy_ask.py` must not import `approvals.py` (approvals imports it). Define `Call = Callable[..., dict]` locally in `deploy_ask.py`.

Test commands (MEASURED baseline at 418850a, 2026-11-13: broker `262 passed`, box `212 passed`):

- Broker: `cd broker && uv run --extra dev pytest -q`
- Box: `cd box && uv run --with pytest python -m pytest -q`

---

### Task 1: Box verb `deployed`

**Files:**
- Create: `box/verbs/deployed` (mode 755; CI fails on a non-executable verb)
- Test: `box/tests/test_deployed.py`

**Interfaces:**
- Produces: box verb `deployed`, stdin ignored (consumed with `boxlib.read_json_stdin`, as `uptime` does), stdout one JSON object `{"harness": <40-hex str | null>, "fleet": <40-hex str | null>}`, exit 0. Reads `$HOME/.local/share/harness-deployed.sha` and `$HOME/.local/share/fleet-deployed.sha`, stripped of surrounding whitespace; null when missing or not `[0-9a-f]{40}`.
- Registration: `boxlib.dispatch` runs any executable file in `box/verbs/`, so the file is the registration. The broker has no verb allow-list (`box.call` only checks `VERB_RE`).

- [ ] **Step 1: Write the failing tests** (spec test 12), reusing the `_verb(name, payload, home)` helper shape from `box/tests/test_uptime.py`:

```python
A, B = "a" * 40, "b" * 40

def test_missing_stamps_give_nulls(tmp_path):
    p = _verb("deployed", {}, tmp_path)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == {"harness": None, "fleet": None}

def test_valid_stamps_are_returned(tmp_path):
    share = tmp_path / ".local/share"; share.mkdir(parents=True)
    (share / "harness-deployed.sha").write_text(A)          # harness-pull/deploy write no newline
    (share / "fleet-deployed.sha").write_text(B + "\n")     # a newline must not make it invalid
    assert json.loads(_verb("deployed", {}, tmp_path).stdout) == {"harness": A, "fleet": B}

def test_non_hex_stamp_gives_null(tmp_path):
    share = tmp_path / ".local/share"; share.mkdir(parents=True)
    (share / "harness-deployed.sha").write_text("195a042")   # short
    (share / "fleet-deployed.sha").write_text("Z" * 40)       # not hex
    assert json.loads(_verb("deployed", {}, tmp_path).stdout) == {"harness": None, "fleet": None}

def test_deployed_is_reachable_through_dispatch(tmp_path):
    # boxlib.dispatch(command, stdin, verbs_dir, env) with io.BytesIO(b"{}") and capsys
    # returns 0 and prints {"harness": None, "fleet": None}
```

Catches: a verb that returns the raw file text (short stamp passes through); a missing `.strip()` (newline stamp → null); a non-executable file (dispatch test returns "unknown verb").

- [ ] **Step 2:** Run `cd box && uv run --with pytest python -m pytest tests/test_deployed.py -q`. Expected: FAIL (verb file missing).
- [ ] **Step 3:** Write `box/verbs/deployed` following `box/verbs/uptime` (shebang `#!/usr/bin/env python3`, `sys.path` insert, `boxlib.read_json_stdin(sys.stdin.buffer)`), `chmod 755`.
- [ ] **Step 4:** Re-run Step 2's command. Expected: 4 passed.
- [ ] **Step 5: Commit**

```bash
git add box/verbs/deployed box/tests/test_deployed.py
git commit -m "box: ungated deployed verb reports both deploy stamps"
```

---

### Task 2: One deploy at a time on the box

**Files:**
- Modify: `box/verbs/deploy` (around the final `if target == "fleet"` dispatch)
- Test: `box/tests/test_deploy.py`

**Interfaces:**
- Produces: `deploy` refuses with exactly `{"error": "a deploy is already running"}` (exit 2, via `boxlib.refuse`) when it cannot take `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `$HOME/.local/share/deploy.lock`. The lock is taken after argument validation and before any git command, held until the process exits, for both targets. Create `~/.local/share` if missing.

- [ ] **Step 1: Write the failing test** (spec test 18) in `box/tests/test_deploy.py`, using the existing `make_repos`, `commit` and `run(home, args, path=...)` helpers:

```python
def test_refuses_while_another_deploy_holds_the_lock(tmp_path):
    up, home, box, first = make_repos(tmp_path)
    want = commit(up, msg="second")
    shim = tmp_path / "shim"; shim.mkdir()
    log = tmp_path / "git.log"
    real = shutil.which("git")
    (shim / "git").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{real}" "$@"\n')
    (shim / "git").chmod(0o755)
    lock = home / ".local/share/deploy.lock"; lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out = run(home, {"sha": want}, path=f"{shim}:{os.environ['PATH']}")
    assert (rc, out) == (2, {"error": "a deploy is already running"})
    assert not log.exists()                      # no git command ran
    assert git(box, "rev-parse", "HEAD") == first

def test_lock_is_released_after_a_deploy(tmp_path):
    up, home, _, _ = make_repos(tmp_path)
    a = commit(up, msg="a"); assert run(home, {"sha": a})[0] == 0
    b = commit(up, msg="b"); assert run(home, {"sha": b}) == (0, {"deployed": b})
```

Same lock test for `{"sha": want, "target": "fleet"}` is not needed: one lock call site covers both targets; assert that by placing the lock before the `if target == "fleet"` line.

Catches: no lock (deploy proceeds, rc 0); a blocking `LOCK_EX` (test hangs; the existing pytest run would time out, so set `timeout=30` on the subprocess in `run` if absent); a lock taken after `git fetch` (log exists).

- [ ] **Step 2:** Run `cd box && uv run --with pytest python -m pytest tests/test_deploy.py -q`. Expected: the lock test FAILS (rc 0).
- [ ] **Step 3:** Add the lock in `box/verbs/deploy`: open the lock file, `flock` non-blocking, on `BlockingIOError` `sys.exit(boxlib.refuse("a deploy is already running"))`. Keep the fd referenced until exit.
- [ ] **Step 4:** Run the whole box suite. Expected: all pass, none skipped.
- [ ] **Step 5: Commit**

```bash
git add box/verbs/deploy box/tests/test_deploy.py
git commit -m "box: deploy refuses while another deploy holds the lock"
```

---

### Task 3: Pending deploy origin, TTL and approve-without-grant

**Files:**
- Modify: `broker/hermes_broker/grants.py`
- Test: `broker/tests/test_grants.py`

**Interfaces:**
- Produces (all on `GrantStore`):
  - `create_deploy_request(sha: str, now: float, target: str = "harness", *, ttl_seconds: float = REQUEST_TTL_SECONDS, origin: str = "agent") -> None` — stores `origin` in `pending_deploy`, `expires_at = now + ttl_seconds`. `ValueError` for an origin not in `DEPLOY_ORIGINS = ("agent", "auto")`. Existing callers (`server.py:212`) unchanged.
  - `pending_deploy_origin(now: float) -> str | None` — origin of an unexpired pending deploy (`"agent"` when the record has no `origin`), else None.
  - `deploy_slot_busy(now: float) -> bool` — True if an unexpired `pending_deploy` has origin other than `"auto"`, or `grants["deploy"]` exists with `expires_at > now` (either target).
  - `approve_auto_deploy(prefix: str, now: float, target: str = "harness") -> str | None` — same checks as `approve_deploy` (expiry pops and writes; sent-before-created, target mismatch and prefix mismatch leave it pending), plus origin must be `"auto"`; on a match pops `pending_deploy` in one `_write` and returns the sha. Writes no grant.
  - `approve_deploy` now also returns None, leaving the request pending, when the pending origin is `"auto"`.
- Share the checks through one private helper rather than copying them.

- [ ] **Step 1: Write the failing tests** (spec tests 8, 14; Review Focus 4)

```python
SHA = "4774e2314f46" + "0" * 28

def test_auto_request_is_valid_for_24h(store):           # spec 8
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=23 * 3600) == SHA
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=25 * 3600) is None

def test_agent_request_still_expires_at_120s(store):      # spec 8: default unchanged
    store.create_deploy_request(SHA, now=0.0)
    assert store.approve_deploy("4774e23", now=121.0) is None
    store.create_deploy_request(SHA, now=0.0)
    assert store.approve_deploy("4774e23", now=119.0) == SHA

def test_auto_approval_writes_no_grant(store):            # spec 14
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=10.0) == SHA
    data = json.loads(store.path.read_text())
    assert "deploy" not in data["grants"] and "pending_deploy" not in data
    assert store.take_deploy_grant(now=11.0) is None

def test_approve_deploy_refuses_an_auto_request(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_deploy("4774e23", now=10.0) is None
    assert store.pending_deploy_origin(10.0) == "auto"     # still pending

def test_approve_auto_deploy_refuses_an_agent_request(store):
    store.create_deploy_request(SHA, now=0.0)
    assert store.approve_auto_deploy("4774e23", now=10.0) is None
    assert store.pending_deploy_origin(10.0) == "agent"

def test_auto_wrong_target_or_prefix_keeps_it_pending(store):   # Review Focus 4
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=10.0, target="fleet") is None
    assert store.approve_auto_deploy("deadbee", now=10.0) is None
    assert store.approve_auto_deploy("4774e23", now=10.0) == SHA

def test_record_without_origin_is_agent(store):
    # write {"pending_deploy": {"sha": SHA, "target": "harness", "created_at": 0, "expires_at": 120}}
    # directly to store.path; pending_deploy_origin(10.0) == "agent"

@pytest.mark.parametrize("setup, busy", [
    ("agent_harness", True), ("agent_fleet", True), ("auto", False), ("grant", True), ("nothing", False)])
def test_deploy_slot_busy(store, setup, busy): ...
    # agent_harness: create_deploy_request(SHA, 0.0); agent_fleet: same with target="fleet";
    # auto: origin="auto", ttl 86400; grant: create + approve_deploy at 1.0 (grant live 600 s).
    # assert store.deploy_slot_busy(50.0) is busy
    # and for every busy case: store.deploy_slot_busy(10_000.0) is False (expired)

def test_unknown_origin_rejected(store):
    with pytest.raises(ValueError):
        store.create_deploy_request(SHA, now=0.0, origin="owner")
```

Catches: TTL ignored (25 h approves); default TTL changed (121 s approves); auto approval writing a grant (`take_deploy_grant` returns the sha); `approve_deploy` granting an auto request (Hermes could deploy it); a busy check that ignores fleet or grants, or ignores expiry.

- [ ] **Step 2:** Run `cd broker && uv run --extra dev pytest tests/test_grants.py -q`. Expected: FAIL (`TypeError` on `ttl_seconds`).
- [ ] **Step 3:** Implement the four methods and the `approve_deploy` origin check in `grants.py`.
- [ ] **Step 4:** Run the whole broker suite. Expected: all pass, including `tests/test_deploy.py` (server deploys unchanged).
- [ ] **Step 5: Commit**

```bash
git add broker/hermes_broker/grants.py broker/tests/test_grants.py
git commit -m "grants: pending deploy carries origin and TTL; auto approval writes no grant"
```

---

### Task 4: Config field `harness_checkout`

**Files:**
- Modify: `broker/hermes_broker/config.py`
- Test: `broker/tests/test_config.py`

**Interfaces:**
- Produces: `BrokerConfig.harness_checkout: Path`, last field, default `Path("~/hermes-harness").expanduser()` (constant `DEFAULT_HARNESS_CHECKOUT`). `load_config` reads optional key `"harness_checkout"` and applies `expanduser()`; absent or empty → default. `BrokerConfig(...)` without it still constructs.

- [ ] **Step 1: Write the failing tests**

```python
def test_harness_checkout_defaults_to_home(tmp_path):
    assert load_config(write(tmp_path)).harness_checkout == Path.home() / "hermes-harness"

def test_harness_checkout_can_be_configured(tmp_path):
    assert load_config(write(tmp_path, harness_checkout=str(tmp_path / "hh"))).harness_checkout == tmp_path / "hh"

def test_harness_checkout_expands_tilde(tmp_path):
    assert load_config(write(tmp_path, harness_checkout="~/x")).harness_checkout == Path.home() / "x"
```

Catches: a missing field (AttributeError); an unexpanded `~` (git -C "~/hermes-harness" fails on the droplet).

- [ ] **Step 2:** `cd broker && uv run --extra dev pytest tests/test_config.py -q` → FAIL.
- [ ] **Step 3:** Add the field and the load.
- [ ] **Step 4:** Re-run → pass.
- [ ] **Step 5: Commit** `git add broker/hermes_broker/config.py broker/tests/test_config.py && git commit -m "config: harness_checkout, default ~/hermes-harness"`

---

### Task 5: State file and message format

**Files:**
- Create: `broker/hermes_broker/deploy_ask.py`
- Test: `broker/tests/test_deploy_ask.py`

**Interfaces:**
- Produces in `deploy_ask.py`:
  - Constants: `STATE_FILE = "deploy_ask.json"`, `SETTLE_SECONDS = 1800`, `ASK_TTL_SECONDS = 86400`, `WARN_EVERY = 3600`, `DEPLOY_TIMEOUT = 900`, `MAX_COMMIT_LINES = 15`, `LINE_CHARS = 100`, `DEPLOY_BUSY = "a deploy is already running"`.
  - `class DeployAskState(path: Path)` with `asked_sha() -> str | None`, `record_asked(sha: str) -> None`, `deploy_running() -> dict | None` (`{"sha": str, "started_at": float}`), `set_running(sha: str, started_at: float) -> None`, `clear_running() -> None`. Each method holds one `threading.Lock` across its read-modify-write (the worker thread and the main loop both write). A missing, unparsable or non-object file reads as `{}`. Writes are atomic and mode 600, as `GrantStore._write`.
  - `format_deploy_ask(box_sha: str | None, master_sha: str, merged_at: float, commits: list[str], ancestor: bool) -> str`. `commits` are `git log --format='%h %s'` lines, newest first; ignored when `box_sha` is None or `ancestor` is False.

Exact text (from the spec's example; `HH:MM` is `merged_at` in UTC):

```
Box is behind master.
box:    <box_sha[:12]>                         | unknown (no deploy stamp)            [when box_sha is None]
                                               | <box_sha[:12]> (not an ancestor of master)  [when not ancestor]
master: <master_sha[:12]> (merged HH:MM UTC)
<N> commits:                                   [only when box_sha and ancestor; "1 commit:" when N == 1]
  <line[:100]>                                 [at most 15]
  and <N-15> more                              [only when N > 15]
Read them, then reply: deploy <master_sha[:12]>
Valid 24 h. Ignore it to skip.
```

- [ ] **Step 1: Write the failing tests** (spec test 11; Review Focus 3)

```python
def test_twenty_commits_show_fifteen_and_a_count():
    lines = [f"{i:07x} subject {i}" for i in range(20)]
    text = format_deploy_ask("b" * 40, "a" * 40, 0.0, lines, ancestor=True)
    shown = [l for l in text.splitlines() if l.startswith("  ") and "more" not in l]
    assert len(shown) == 15 and "  and 5 more" in text.splitlines() and "20 commits:" in text

def test_long_subject_is_cut_to_100():
    text = format_deploy_ask("b" * 40, "a" * 40, 0.0, ["abcdef0 " + "x" * 150], ancestor=True)
    (line,) = [l for l in text.splitlines() if l.startswith("  abcdef0")]
    assert len(line) == 2 + 100

def test_exact_layout():
    text = format_deploy_ask("195a042c401f" + "0" * 28, "4774e2314f46" + "0" * 28,
                             1763034240.0, ["4774e23 Owner todo list (#55)"], ancestor=True)
    assert text == ("Box is behind master.\nbox:    195a042c401f\n"
                    "master: 4774e2314f46 (merged 11:44 UTC)\n1 commit:\n  4774e23 Owner todo list (#55)\n"
                    "Read them, then reply: deploy 4774e2314f46\nValid 24 h. Ignore it to skip.")

def test_null_box_and_not_ancestor_lines():
    # box_sha None -> "box:    unknown (no deploy stamp)", no "commit" line
    # ancestor False -> "box:    <12> (not an ancestor of master)", no "commit" line

def test_state_survives_a_new_instance(tmp_path):
    DeployAskState(tmp_path / STATE_FILE).record_asked("a" * 40)
    s = DeployAskState(tmp_path / STATE_FILE)
    assert s.asked_sha() == "a" * 40
    s.set_running("b" * 40, 5.0)
    assert DeployAskState(tmp_path / STATE_FILE).deploy_running() == {"sha": "b" * 40, "started_at": 5.0}
    s.clear_running()
    assert s.deploy_running() is None and s.asked_sha() == "a" * 40   # clearing keeps asked_sha

def test_corrupt_state_reads_as_empty(tmp_path):                       # Review Focus 3
    (tmp_path / STATE_FILE).write_text("{not json")
    s = DeployAskState(tmp_path / STATE_FILE)
    assert s.asked_sha() is None and s.deploy_running() is None
    s.record_asked("a" * 40)          # and it can be written over
    assert s.asked_sha() == "a" * 40
```

`1763034240` is 11:44:00 UTC (MEASURED while writing this plan with `python3 -c 'import datetime;print(datetime.datetime.fromtimestamp(1763034240, datetime.timezone.utc))'` → `2025-11-13 11:44:00+00:00`).

Catches: unbounded commit list; a per-subject cut instead of per-line (or none); `set_running`/`clear_running` overwriting the whole file and dropping `asked_sha`; a crash on a corrupt file.

- [ ] **Step 2:** `cd broker && uv run --extra dev pytest tests/test_deploy_ask.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement the constants, `DeployAskState` and `format_deploy_ask`.
- [ ] **Step 4:** Re-run → pass.
- [ ] **Step 5: Commit** `git add broker/hermes_broker/deploy_ask.py broker/tests/test_deploy_ask.py && git commit -m "deploy_ask: state file and ask message"`

---

### Task 6: `DeployRunner` and routing auto approvals

**Files:**
- Modify: `broker/hermes_broker/deploy_ask.py`, `broker/hermes_broker/approvals.py` (`handle`, `_dispatch`)
- Test: `broker/tests/test_deploy_ask.py`, `broker/tests/test_approvals.py`

**Interfaces:**
- Consumes: `GrantStore.pending_deploy_origin`, `approve_auto_deploy`, `approve_deploy` (Task 3); `DeployAskState`, `DEPLOY_BUSY`, `DEPLOY_TIMEOUT` (Task 5).
- Produces:
  - `class DeployRunner(call: Call, send: Callable[[str], bool], state: DeployAskState, clock: Callable[[], float] = time.time, spawn: Callable[[Callable[[], None]], None] = <starts a daemon threading.Thread>)`.
    - `busy() -> bool`.
    - `start(sha: str) -> str`: if busy, returns `DEPLOY_BUSY`. Otherwise takes the runner's lock, `state.set_running(sha, clock())`, `spawn(worker)`, returns `f"Deploying {sha[:12]} to tig-server. The result follows when it finishes."`. If `spawn` raises, clear the record, release, re-raise.
    - worker: `r = call("deploy", {"sha": sha, "target": "harness"}, DEPLOY_TIMEOUT)`; sends `f"tig-server: deployed {sha[:12]}"` when `r.get("deployed") == sha`, else `f"tig-server: deploy of {sha[:12]} failed: {r.get('error', r)}"`; an exception from `call` sends `f"tig-server: deploy of {sha[:12]} failed: {exc}"`. Then, in `finally`, `state.clear_running()` and release (send first, clear after, per the spec).
  - `handle(store, verb, arg, now, prod=None, runner: DeployRunner | None = None) -> str`. In the `deploy` branch, when `store.pending_deploy_origin(now) == "auto"`:
    - `runner is None` → `"Auto deploys are not wired in this listener."` (request kept);
    - `runner.busy()` → `DEPLOY_BUSY` (request kept);
    - `sha = store.approve_auto_deploy(prefix, now, target)`; None → the existing `f"No pending {target} deploy of a commit starting {prefix}."`; else `f"Deploy of {sha} to tig-server approved.\n" + runner.start(sha)`. No `_tell_hermes`.
    - Any other origin: today's code path, unchanged.
  - `_dispatch(..., prod=None, runner: DeployRunner | None = None)` passes `runner` to `handle`.

- [ ] **Step 1: Write the failing tests**

In `test_approvals.py` (spec test 9; Review Focus 4):

```python
class FakeRunner:
    def __init__(self, busy=False): self.started, self._busy = [], busy
    def busy(self): return self._busy
    def start(self, sha): self.started.append(sha); return "Deploying."

def test_auto_approval_starts_the_runner_and_does_not_prod(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    prod, runner = Recorder(), FakeRunner()      # Recorder: records titles, returns True
    reply = handle(store, "deploy", "harness:4774e23", 10.0, prod, runner=runner)
    assert runner.started == [SHA] and prod.titles == [] and "Deploying." in reply

def test_agent_approval_prods_and_does_not_start_the_runner(store):
    store.create_deploy_request(SHA, now=0.0)
    prod, runner = Recorder(), FakeRunner()
    handle(store, "deploy", "harness:4774e23", 10.0, prod, runner=runner)
    assert runner.started == [] and len(prod.titles) == 1 and store.deploy_target() == "harness"

def test_auto_approval_while_busy_keeps_the_request(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    r = FakeRunner(busy=True)
    assert handle(store, "deploy", "harness:4774e23", 10.0, runner=r) == "a deploy is already running"
    assert r.started == [] and store.pending_deploy_origin(10.0) == "auto"

def test_auto_request_wrong_target_is_refused(store):        # Review Focus 4
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    r = FakeRunner()
    assert handle(store, "deploy", "fleet:4774e23", 10.0, runner=r).startswith("No pending fleet deploy")
    assert r.started == [] and store.pending_deploy_origin(10.0) == "auto"
```

Check what `Recorder` / `store` fixtures already exist in `test_approvals.py` / `test_prod.py` and reuse them.

In `test_deploy_ask.py` (spec test 10; Review Focus 5):

```python
def test_runner_reports_success_and_clears(tmp_path):
    state, sent, calls = DeployAskState(tmp_path / STATE_FILE), [], []
    def call(verb, args, timeout=60): calls.append((verb, args, timeout)); return {"deployed": SHA}
    r = DeployRunner(call, lambda t: sent.append(t) or True, state, clock=lambda: 7.0, spawn=lambda fn: fn())
    r.start(SHA)
    assert calls == [("deploy", {"sha": SHA, "target": "harness"}, 900)]
    assert sent == [f"tig-server: deployed {SHA[:12]}"] and state.deploy_running() is None and not r.busy()

def test_runner_records_running_before_the_call(tmp_path):
    # in the fake call, assert state.deploy_running() == {"sha": SHA, "started_at": 7.0}

@pytest.mark.parametrize("behaviour", ["error", "raise"])
def test_runner_failure_is_reported_and_frees_the_runner(tmp_path, behaviour):   # Review Focus 5
    # call returns {"error": "tig-server unreachable: timed out"} or raises RuntimeError("boom")
    # sent == [f"tig-server: deploy of {SHA[:12]} failed: tig-server unreachable: timed out"] (or "...: boom")
    # state.deploy_running() is None and r.busy() is False

def test_box_refusal_text_is_relayed(tmp_path):
    # call returns {"error": "master is at 5555…, not the approved 4774…; request a new deploy"}
    # sent[0] ends with that exact error text

def test_deploy_does_not_block_other_updates(tmp_path):     # spec 10, real thread
    store = GrantStore(tmp_path / "grants.json")
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    release, entered, sent = threading.Event(), threading.Event(), []
    def call(verb, args, timeout=60):
        entered.set(); assert release.wait(10); return {"deployed": SHA}
    runner = DeployRunner(call, lambda t: sent.append(t) or True, DeployAskState(tmp_path / STATE_FILE))
    first = _dispatch(owner_update("deploy 4774e23", date=10), OWNER, store, None, None, (), lambda: 10.0, runner=runner)
    assert entered.wait(5) and "Deploying" in first
    store.create_deploy_request(SHA2, now=11.0, ttl_seconds=86400, origin="auto")
    second = _dispatch(owner_update("deploy " + SHA2[:7], date=12), OWNER, store, None, None, (), lambda: 12.0, runner=runner)
    revoke = _dispatch(owner_update("revoke", date=13), OWNER, store, None, None, (), lambda: 13.0, runner=runner)
    assert second == "a deploy is already running" and revoke == "Revoked. All boxes locked."
    assert sent == []                      # the deploy has not returned yet
    release.set()
    # wait (bounded, ≤5 s) until sent == [f"tig-server: deployed {SHA[:12]}"]
```

`owner_update(text, date)` is a local helper building `{"update_id": 1, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": text, "date": date}}`. `SHA2` is a second 40-hex sha.

Catches: a synchronous deploy in the poll loop (the first `_dispatch` blocks inside the fake until `release.wait(10)` times out and its `assert` fails, so the test fails in about 10 s instead of hanging); origin not checked in `handle` (agent test starts the runner, or auto test prods); busy check after consuming (request gone); record not cleared on failure (runner stuck busy).

- [ ] **Step 2:** `cd broker && uv run --extra dev pytest tests/test_deploy_ask.py tests/test_approvals.py -q` → FAIL.
- [ ] **Step 3:** Implement `DeployRunner`, then the `handle` and `_dispatch` changes.
- [ ] **Step 4:** Run the whole broker suite → all pass.
- [ ] **Step 5: Commit**

```bash
git add broker/hermes_broker/deploy_ask.py broker/hermes_broker/approvals.py broker/tests/test_deploy_ask.py broker/tests/test_approvals.py
git commit -m "approvals: the listener deploys an auto-asked commit itself, in a worker thread"
```

---

### Task 7: `DeployAnnouncer`

**Files:**
- Modify: `broker/hermes_broker/deploy_ask.py`
- Test: `broker/tests/test_deploy_ask.py`

**Interfaces:**
- Consumes: `GrantStore.deploy_slot_busy`, `create_deploy_request(..., ttl_seconds=ASK_TTL_SECONDS, origin="auto")` (Task 3); `DeployAskState`, `format_deploy_ask` (Task 5); `DeployRunner.busy` (Task 6).
- Produces: `class DeployAnnouncer(grants: GrantStore, state: DeployAskState, runner: DeployRunner, call: Call, send: Callable[[str], bool], checkout: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run)` with `tick(now: float) -> None`.
  - Every git command is `run(["git", "-C", str(checkout), <sub>, ...], capture_output=True, text=True, timeout=30, check=False)` with `<sub>` in `("rev-parse", "log", "diff", "merge-base")`.
  - One warning limiter `_warn(now, msg)`: logs when `self._last_warn is None or now - self._last_warn >= WARN_EVERY`. Initialise to `None`, not `0.0` (tests use small `now`). Used for `deployed` failures and git failures.

`tick(now)` order:

0. **Recovery.** If `state.deploy_running()` is set and `not runner.busy()`: call `deployed` (timeout 60). On failure `_warn` and return (retry next tick). Stamp equal to the record's sha → send `f"tig-server: deployed {sha[:12]}"`; else send `f"deploy of {sha[:12]} did not finish; the next ask will offer it again"`. Clear the record either way, then continue with the tick.
1. `master = rev-parse origin/master`. Nonzero exit → `_warn`, return.
2. `r = call("deployed", {}, 60)`. Failure is any of: `"error" in r`, `"harness"` key missing, value neither None nor a 40-hex str. Failure → `_warn`, return.
3. `box == master` → return.
4. 3a: `grants.deploy_slot_busy(now)` or `state.deploy_running()` → return.
5. Settle: `ct = int(log -1 --format=%ct origin/master)`; `now - ct < SETTLE_SECONDS` → return.
6. If `box` is not None: `ancestor = merge-base --is-ancestor <box> <master>` exit 0 (any nonzero is "not an ancestor"). If ancestor: `diff --name-only <box>..<master>`; all paths start with `docs/` → return; `commits = log --format=%h %s <box>..<master>` lines. If `box` is None: run none of these.
7. `state.asked_sha() == master` → return. Else `grants.create_deploy_request(master, now, ttl_seconds=ASK_TTL_SECONDS, origin="auto")`, `ok = send(format_deploy_ask(box, master, ct, commits, ancestor))`, `record_asked(master)` only if `ok`.

Test fixture: a real git repo pair in `tmp_path` (bare `origin`, a working copy `up`, the droplet `checkout` cloned from origin), commits made with `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` set to a fixed epoch `T0 = 1_763_000_000` plus an offset, then `git -C checkout fetch -q` by the test (never by the announcer). `run` is a recording wrapper over `subprocess.run` that appends each argv. `call` is the `Box`-style fake from `test_announcer.py` (`{"deployed": {"harness": ..., "fleet": None}}`), `send` the recording sender, `runner` a `FakeRunner` with `busy()`.

The `env` fixture exposes: `checkout`, `first` (an older commit) and `master` (the tip; its commit changes a file outside `docs/` and has commit time `ct`); `state`, `grants`, `grants_path`, `grants_json()`; `box_at(sha)` (fake `deployed` returns `{"harness": sha, "fleet": None}`), `box_reply(dict)`; `sent` (accepted texts), `send_attempts` (all texts), `send_results` (optional list of return values, default all True); `argvs` (every argv given to `run`); `runner` (the `FakeRunner`); `ann`; `new_announcer()` (a fresh `DeployAnnouncer` on the same state and grants files); `make_blocker(kind, now)`; and helpers to add commits on master or a side branch and fetch them into `checkout`. Use `caplog.set_level(logging.WARNING, logger="hermes_broker.deploy_ask")` in the warning test.

- [ ] **Step 1: Write the failing tests** (spec tests 1–7, 13, 15, 16, 19; Review Focus 1, 2)

```python
def test_up_to_date_sends_nothing(env):                       # 1
    env.box_at(env.master); env.ann.tick(env.ct + 3600)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()

def test_settle_time(env):                                    # 2
    env.box_at(env.first)
    env.ann.tick(env.ct + 29 * 60); assert env.sent == []
    env.ann.tick(env.ct + 31 * 60); assert len(env.sent) == 1
    assert env.grants.pending_deploy_origin(env.ct + 31 * 60) == "auto"

def test_once_per_commit_across_restart(env):                 # 3
    env.box_at(env.first)
    env.ann.tick(env.ct + 3600); env.ann.tick(env.ct + 3900)
    env.new_announcer().tick(env.ct + 4200)                    # same state file
    assert len(env.sent) == 1

def test_failed_send_is_retried(env):                         # 4
    env.box_at(env.first); env.send_results = [False, True, True]
    for t in (3600, 3900, 4200): env.ann.tick(env.ct + t)
    assert len(env.send_attempts) == 2 and env.state.asked_sha() == env.master

def test_docs_only_is_skipped(env):                           # 5
    # box at a commit; master adds docs/a.md and docs/b/c.md → no send
    # then master adds README.md as well → one send

def test_not_an_ancestor(env):                                # 6
    # box at a commit on a side branch pushed to origin and fetched, not merged
    # one send; "not an ancestor of master" in it; no "commit" line

def test_box_sha_unknown_to_checkout(env):                    # Review Focus 2
    env.box_at("e" * 40)                                       # object not in the repo
    env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1 and "(not an ancestor of master)" in env.sent[0]

def test_deployed_failure_warns_once_an_hour(env, caplog):    # 7
    env.box_reply({"error": "tig-server unreachable: timed out"})
    for i in range(20): env.ann.tick(env.ct + 3600 + 60 * i)  # spans 19 min
    assert env.sent == [] and "pending_deploy" not in env.grants_json()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    env.ann.tick(env.ct + 3600 + 3600)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

@pytest.mark.parametrize("reply", [{}, {"harness": "195a042"}, {"harness": 5}])
def test_malformed_deployed_reply_is_a_failure(env, reply):   # Review Focus 1
    env.box_reply(reply); env.ann.tick(env.ct + 3600)
    assert env.sent == [] and "pending_deploy" not in env.grants_json()

@pytest.mark.parametrize("blocker", ["agent_harness", "agent_fleet", "grant"])
def test_never_displaces_another_request(env, blocker):       # 13
    env.box_at(env.first); now = env.ct + 3600
    env.make_blocker(blocker, now)       # agent pending (120 s) or approve_deploy → live 10-min grant
    before = env.grants_path.read_bytes()
    env.ann.tick(now + 1)
    assert env.sent == [] and env.grants_path.read_bytes() == before and env.state.asked_sha() is None
    env.ann.tick(now + 700)               # past 120 s and past the 600 s grant
    assert len(env.sent) == 1

def test_deploy_running_blocks_the_ask(env):
    # runner.busy() True and state.set_running(...) → no send, no pending

def test_null_box_stamp(env):                                 # 15
    env.box_reply({"harness": None, "fleet": None}); env.ann.tick(env.ct + 3600)
    assert len(env.sent) == 1 and "box:    unknown (no deploy stamp)" in env.sent[0]
    assert "commit" not in env.sent[0].split("master:")[1].split("Read them")[0]
    assert not any(a[3] in ("diff", "merge-base") or (a[3] == "log" and any(".." in x for x in a)) for a in env.argvs)

@pytest.mark.parametrize("stamp, expect", [("same", "tig-server: deployed "), ("other", "did not finish")])
def test_restart_recovery(env, stamp, expect):                # 16
    env.state.set_running(env.master, env.ct)
    env.box_at(env.master if stamp == "same" else env.first)
    env.new_announcer().tick(env.ct + 60)
    assert expect in env.sent[0] and env.state.deploy_running() is None

def test_recovery_skipped_while_this_process_is_deploying(env):
    # runner.busy() True, record set → no recovery message, record kept

def test_only_read_only_git(env):                             # 19
    # run the behind/ancestor, docs-only, not-ancestor and null scenarios through one env,
    # then: assert {a[3] for a in env.argvs} <= {"rev-parse", "log", "diff", "merge-base"}
    # and every argv starts with ["git", "-C", str(env.checkout)]
```

Mutation each catches: 1 asking when equal; 2 missing or inverted settle; 3 in-memory dedupe; 4 recording before a successful send; 5 `any` vs `all`; 6 and RF2 listing commits for (or crashing on) a non-ancestor, or treating exit 128 as a git failure; 7 log spam and asking blind; RF1 a box reply without a valid stamp read as null and asked about; 13 overwriting the shared slot, or a busy check without expiry (the second tick would never ask); 15 the null case falling into "git failure, skip"; 16 the record never read on start; 19 a fetch or reset in the listener's checkout.

- [ ] **Step 2:** `cd broker && uv run --extra dev pytest tests/test_deploy_ask.py -q` → the new tests FAIL.
- [ ] **Step 3:** Implement `DeployAnnouncer` in `deploy_ask.py`.
- [ ] **Step 4:** Run the whole broker suite → all pass.
- [ ] **Step 5: Commit**

```bash
git add broker/hermes_broker/deploy_ask.py broker/tests/test_deploy_ask.py
git commit -m "deploy_ask: announcer asks once per master commit when the box is behind"
```

---

### Task 8: Wire it into the listener

**Files:**
- Modify: `broker/hermes_broker/approvals.py` (`run_listener`), `broker/hermes_broker/listen.py` (`main`)
- Test: `broker/tests/test_prod.py` (listener and `main` tests live there), or a new `broker/tests/test_deploy_ask_listener.py`

**Interfaces:**
- Consumes: `BrokerConfig.harness_checkout` (Task 4); `DeployAskState`, `DeployRunner`, `DeployAnnouncer`, `STATE_FILE` (Tasks 5–7); `_dispatch(..., runner=)` (Task 6).
- Produces: `run_listener(..., harness_checkout: Path | None = None)`. When `call`, `state_dir` and `harness_checkout` are all not None, it builds `state = DeployAskState(Path(state_dir) / STATE_FILE)`, a `DeployRunner(call, send, state)` and a `DeployAnnouncer(store, state, runner, call, send, harness_checkout)`, where `send` is the same client-bound `_send` lambda the other announcers use. The runner goes to every `_dispatch` call. The announcer ticks every `ANNOUNCE_EVERY` on its own `last_deploy_ask` timestamp inside its own `try/except Exception` (`logger.exception("deploy announcer tick failed")`). Otherwise both are None and deploys behave as today. `listen.main` passes `harness_checkout=cfg.harness_checkout`.

- [ ] **Step 1: Write the failing tests**

```python
def test_listener_deploys_an_auto_request_and_relays_the_result(tmp_path):
    # store with create_deploy_request(SHA, now=1000.0, ttl_seconds=86400, origin="auto")
    # update "deploy 4774e23" dated 1060; call fake: "deploy" -> {"deployed": SHA}, "deployed" -> {"harness": SHA, "fleet": None},
    #   "list_triage" -> {"issues": [], "repos_ok": [], "errors": []}
    # MockTransport as in test_listener_loop_prods_when_the_owner_approves; on the 2nd getUpdates,
    #   wait (bounded, ≤5 s) until a sent text starts with "tig-server: deployed", then raise Stop
    # run_listener(store, "123:abc", OWNER, poll_seconds=0, tasks=TaskStore(...), call=fake, state_dir=tmp_path,
    #              harness_checkout=tmp_path / "nocheckout", prod=prod, transport=...)
    # assert prod.titles == []; sent[0].startswith(f"Deploy of {SHA} to tig-server approved.")
    # assert f"tig-server: deployed {SHA[:12]}" in sent; fake.calls has exactly one "deploy"

def test_main_passes_harness_checkout(tmp_path, monkeypatch):
    # as test_main_starts_the_listener_with_a_prod_for_the_configured_owner, with
    # "harness_checkout": str(tmp_path / "hh") in broker.json; assert started["harness_checkout"] == tmp_path / "hh"
```

`monkeypatch.setenv` for `HERMES_BROKER_CONFIG` follows the existing `main` test; it sets the process environment, not a module default.

Catches: the runner not passed to `_dispatch` (Hermes prodded, no box call); the announcer not built (no change visible here, covered by Task 7); `main` dropping the field.

- [ ] **Step 2:** Run the new tests → FAIL.
- [ ] **Step 3:** Implement the `run_listener` and `main` changes.
- [ ] **Step 4:** Run the whole broker suite → all pass.
- [ ] **Step 5: Commit**

```bash
git add broker/hermes_broker/approvals.py broker/hermes_broker/listen.py broker/tests/test_prod.py
git commit -m "approvals: run the deploy announcer and runner in the listener"
```

(Add `broker/tests/test_deploy_ask_listener.py` instead of `test_prod.py` if that is where the tests went.)

---

### Task 9: harness-pull defers the approvals restart during a deploy

**Files:**
- Modify: `droplet/harness-pull.sh`
- Test: `box/tests/test_harness_pull.py` (the box CI job runs it; `test_remind_owner.py` tests a droplet script from there the same way)

**Interfaces:**
- Consumes: the state file `<state_dir>/deploy_ask.json` and its `deploy_running.started_at` (Task 5), where `state_dir` is read from `${HERMES_BROKER_CONFIG:-$HOME/.hermes/broker/broker.json}` (`~` expanded).
- Produces: in the owner block, before `git checkout`, a `python3 -c` check that exits 0 only when the record exists and `time.time() - started_at < 1200`. On 0 the script echoes `deploy running; approvals restart deferred`, skips the whole owner block (no reset, no broker `uv sync`, no restart, no stamp) and continues to the friends and later steps. Any read error (no config, no state file, bad JSON, no record) → not live → the owner block runs as today. Also replace `/usr/local/bin/uv` with `uv` in both places; `harness-pull.service` already sets `PATH=/usr/local/bin:/usr/bin:/bin`, so production resolves the same binary and the test can stub it.

- [ ] **Step 1: Write the failing tests** (spec test 17; Review Focus 3)

Stubs in `tmp_path/stubs`, first on PATH: `git` (logs `$*`; prints `WANT` for `rev-parse origin/master` and `abc1234` for `rev-parse --short HEAD`; exit 0), `systemctl` and `uv` (log `$*`, exit 0). `HOME=tmp_path/home` with `hermes-harness/broker` and `hermes-harness/friends` directories and the friends stamp preset to `WANT` (so only the owner block is under test). `HERMES_BROKER_CONFIG` points at a `broker.json` whose `state_dir` holds `deploy_ask.json`. Run `bash droplet/harness-pull.sh` with `timeout=30`.

```python
def test_fresh_deploy_record_defers_restart_and_stamp(tmp_path):
    r, logs, home = _run(tmp_path, record_age=300)
    assert r.returncode == 0, r.stderr
    assert "restart hermes-approvals" not in logs["systemctl"]
    assert not (home / ".local/share/harness-deployed.sha").exists()
    assert "reset" not in logs["git"]

def test_stale_record_restarts_and_stamps(tmp_path):
    r, logs, home = _run(tmp_path, record_age=1500)
    assert "--user restart hermes-approvals" in logs["systemctl"]
    assert (home / ".local/share/harness-deployed.sha").read_text() == WANT

@pytest.mark.parametrize("state", ["absent", "corrupt", "no_record"])
def test_unreadable_state_does_not_block(tmp_path, state):     # Review Focus 3
    r, logs, home = _run(tmp_path, state=state)
    assert "--user restart hermes-approvals" in logs["systemctl"]
    assert (home / ".local/share/harness-deployed.sha").read_text() == WANT
```

`record_age` sets `started_at = time.time() - age`; 300 vs 1500 seconds is far from the 1200 boundary, so wall-clock drift during the test cannot flip it.

Catches: the restart killing a running deploy (fresh case restarts); a stale record blocking deploys forever (stale case does not restart); a corrupt file treated as live; the stamp written while deferred (the next tick would never retry).

- [ ] **Step 2:** `cd box && uv run --with pytest python -m pytest tests/test_harness_pull.py -q` → FAIL (fresh case restarts; stubbed `uv` is bypassed by the absolute path).
- [ ] **Step 3:** Edit `droplet/harness-pull.sh`.
- [ ] **Step 4:** Run the box suite and `shellcheck -S warning droplet/harness-pull.sh` → pass, no warnings.
- [ ] **Step 5: Commit**

```bash
git add droplet/harness-pull.sh box/tests/test_harness_pull.py
git commit -m "harness-pull: defer the approvals restart while the listener runs a deploy"
```

---

### Task 10: Runbook

**Files:**
- Modify: `runbooks/dispatch.md`, section "Deploys" (lines 16–31 at 418850a)

- [ ] **Step 1:** Add a paragraph after the first one in "Deploys", covering exactly the spec's Docs list: the bot asks by itself when the box is behind master (30-minute settle time, no ask for a docs-only diff, once per master commit, valid 24 h); the reply is the same `deploy <prefix>`, typed after reading the commits; the listener then deploys that commit itself with no grant and replies `tig-server: deployed <sha12>` or the box's error; an agent's `request_deploy` replaces a pending auto-ask, an auto-ask never replaces an agent's request; `harness-pull` defers the approvals restart while a deploy runs (record younger than 20 minutes, `<state_dir>/deploy_ask.json`); the box refuses two deploys at once (`~/.local/share/deploy.lock`). Add the first-time step: the box needs one deploy through `request_deploy` to install the `deployed` verb; until then the listener logs one warning an hour and asks nothing. Plain statements, no metaphors.
- [ ] **Step 2:** Run both suites once more: broker and box commands from the top of this plan. Expected: all pass; record the counts.
- [ ] **Step 3: Commit**

```bash
git status --short     # only runbooks/dispatch.md
git add runbooks/dispatch.md
git commit -m "runbook: the approvals bot asks to deploy the box when it falls behind"
```

---

## Rollout

Not part of this plan's execution. The owner's session does the spec's three rollout steps after merge.
