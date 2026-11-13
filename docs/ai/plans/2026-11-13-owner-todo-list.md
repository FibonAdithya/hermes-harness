# Owner Todo List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents add follow-up items to the owner's `todos.md` in the wiki (`hermes-wiki todo add|done|list`) instead of sending a Telegram reminder, and `remind-owner` gains `--cancel` and `--prune`.

**Architecture:** A new pure module `wiki/hermes_wiki/todos.py` parses and edits the `## Agent follow-ups` section as text, line by line, and never touches bytes outside it. A small store layer in the same module reads and writes `todos.md` through the GitHub contents API with `gh` (or a dry directory), with the blob `sha` as compare-and-swap and a 409 retry loop. `cli.py` registers a `todo` subcommand. Part B adds two flags to `droplet/remind-owner.sh`, implemented as an embedded `python3` block.

**Tech Stack:** Python ≥3.11 stdlib only (wiki package), pytest, `gh api`, bash + `python3` on the droplet (MEASURED 2026-11-13: `ssh hermes-vm 'python3 --version'` → `Python 3.12.3`).

**Spec:** `docs/ai/specs/2026-11-13-owner-todo-list-design.md`

**Baseline (MEASURED 2026-11-13, this worktree at 30e5200):** `cd wiki && uv run pytest -q` → `402 passed`; `cd box && uv run --with pytest python -m pytest -q tests/test_remind_owner.py` → `5 passed`.

## Global Constraints

- The todo list is `todos.md` at the root of `FibonAdithya/hermes-wiki`, branch `main`; repo comes from `cfg.repo`, `gh` binary from `cfg.gh`.
- Section = the line `## Agent follow-ups` and every line after it up to, not including, the next line that starts with `## ` (or EOF). A `### ` line does not end it.
- Every byte of `todos.md` outside the section is unchanged after any command, frontmatter included.
- Item first line: `- [ ] TITLE (YYYY-MM-DD, SOURCE, id XXXXXX)`; details `  - DETAIL` (two spaces). Date is the UTC date, SOURCE is `cfg.source`, id is 6 lowercase hex from `secrets`, unique within the section.
- Title: one line, no control characters, 1–120 characters after stripping. Detail: one line, no control characters, ≤300 characters, ≤15 details. `secretscan.find_secrets` over title + details: any hit refuses.
- Exit codes: validation failure, secret hit, `list` with `public_only` → 2. Missing `todos.md`, duplicate heading, unknown id, 5 conflicts, PUT timeout, other GitHub failure → 1. Already ticked → 0 with a stderr message.
- Validation runs before any GitHub call. `list` with `cfg.public_only` makes no GitHub call.
- Output never contains `todos.md` content except `list` output: `add` prints only the id line; errors print the id or the path only.
- GitHub writes: GET `repos/{repo}/contents/todos.md?ref=main`, PUT with that `sha`, `branch=main`, message `todo: add XXXXXX` / `todo: done XXXXXX`. HTTP 409 → re-GET, at most 5 attempts, backoff `0.5 * 2 ** attempt + random.uniform(0, 0.5)` (as `notes.publish`). PUT timeout is not retried; stderr says the write "may or may not" have landed.
- `HERMES_WIKI_DRY_PUBLISH=1` → read and write `<cfg.state_dir>/dry/todos.md` instead of GitHub.
- Tests never reach the network and never monkeypatch module defaults: the store is passed explicitly (no default argument). The conftest guard already puts a failing `gh` first on `PATH`.
- `remind-owner` reminder scripts are named `remind-[0-9]{14}-[0-9]+\.sh`; prune age guard is strictly more than 10 minutes (600 s) since the `.sh` mtime.
- No change to the morning briefing, no droplet todo command, no MCP tool, no pruning of ticked items.

## Review Focus

1. **Section is the last thing in the file and the file has no trailing newline.** A new item goes on its own line; it must not be glued onto the last line. Test: Task 1 `test_add_at_eof_without_newline`.
2. **Owner puts a `### ` subheading inside the agents' section.** The section still ends only at the next `## ` line; a `startswith("##")` check would end it at the `###` and write the item in the middle. Test: Task 1 `test_h3_inside_section_is_not_a_boundary`.
3. **GitHub returns base64 wrapped at 60/76 characters with `\n`.** The decode must ignore the line breaks. Test: Task 2 `test_gh_get_decodes_wrapped_base64`.
4. **A detail line that looks like an item, e.g. `  - [ ] see (2026-11-01, claude-box, id abc123)`.** `done abc123` must not tick an indented line; only column-0 item first lines count. Test: Task 1 `test_done_ignores_indented_lines`.
5. **`jobs.json` has jobs whose `script` is `null` or a non-reminder name** (MEASURED 2026-11-13 on hermes-vm: 5 of 12 jobs have `script: null`, others include `wiki-worklist.py`, `loop0-reminder.sh`). `--prune` must treat those as "not a reminder script" without crashing, and `--cancel` of such a job refuses. Test: Task 5 `test_prune_tolerates_null_scripts` and spec test 2.
6. **Owner lines sit between `# Todos` and the first `## `, and the section does not exist yet.** Inserting the heading directly after the H1 (the spec's wording) would make those lines part of the agents' section. The plan inserts before the first `## ` after the H1 instead; on the real file the bytes are the same. Tests: Task 1 `test_owner_lines_under_h1_stay_outside_section`, `test_no_h1_no_h2_appends_at_eof`, `test_heading_search_skips_frontmatter`.

---

### Task 1: Section parsing and edits (`todos.py`, pure functions)

**Files:**
- Create: `wiki/hermes_wiki/todos.py`
- Create: `wiki/tests/fixtures/todos.md`
- Test: `wiki/tests/test_todos.py`

**Interfaces:**
- Consumes: `hermes_wiki.secretscan.find_secrets(text) -> list[str]`; `hermes_wiki.notes._CONTROL` (compiled regex; covers every `str.splitlines` boundary, so it also enforces "one line").
- Produces (all in `hermes_wiki.todos`):
  - `HEADING = "## Agent follow-ups"`, `MAX_TITLE = 120`, `MAX_DETAIL = 300`, `MAX_DETAILS = 15`
  - `class TodoError(Exception)`: `__init__(self, message: str, code: int = 1)`, attribute `code`
  - `class TodoRejected(TodoError)`: `__init__(self, errors: list[str])`, `code = 2`, attribute `errors`
  - `@dataclass(frozen=True) class Item: id: str | None; title: str; done: bool`
  - `validate(title: str, details: list[str]) -> tuple[str, list[str]]` — returns stripped title and stripped details; raises `TodoRejected` listing every problem; secret hits appear as `secret pattern: <name>`
  - `add_item(text: str, title: str, details: list[str], *, source: str, today: date, new_id: Callable[[], str]) -> tuple[str, str]` — returns `(new_text, id)`; does not validate (caller does)
  - `tick_item(text: str, item_id: str) -> str | None` — returns new text, or `None` if the item is already ticked; raises `TodoError(code=1)` for no section, no match, or more than one match
  - `list_items(text: str, include_done: bool) -> list[Item]`
  - All three raise `TodoError(code=1)` when `HEADING` appears more than once.

**Decisions the implementer cannot make alone:**
- Split on `"\n"` only, keeping the endings: `re.findall(r"[^\n]*\n|[^\n]+\Z", text)`; join with `""`. Not `str.splitlines`: it also breaks at `\x0b`, `\x0c`, `\x1c`–`\x1e`, `\x85`, `\u2028`, `\u2029`, so an owner line holding one of those would be cut into fake lines (a fragment starting `## ` would end the section). The "no line ending" checks below mean "does not end with `\n`". A line is the heading when `line.rstrip() == HEADING`; a line ends the section when `line.startswith("## ")` (exactly that prefix).
- Item first line: `re.compile(r"^- \[([ xX])\] (.*)$")` on the line with its line ending stripped (column 0 only). Ticked = `x` or `X`.
- `tick_item` matches a first line inside the section whose `rstrip()` ends with `f"id {item_id})"`; `item_id` that does not fullmatch `[0-9a-f]{6}` → no match. It replaces only the first five characters `- [ ]` with `- [x]`.
- Title/id parse for `list_items`: `re.compile(r"^(.*) \(\d{4}-\d{2}-\d{2}, [^,()]*, id ([0-9a-f]{6})\)$")` on the text after `- [ ] `. No match → `Item(id=None, title=<whole rest>)`.
- Ids already in the section: every `id ([0-9a-f]{6})\)` match in the section text. `add_item` calls `new_id()` until it returns an unused id; after 100 tries raise `TodoError`.
- Insert into an existing section: walk back from the section end over blank lines (`line.strip() == ""`) but not past the heading; insert there. If the line before the insertion point has no line ending, append `"\n"` to it first.
- Insert when the section is missing (deviation from the spec's "directly after the H1", decided in the audit): `start` = the line after the closing `---` of a frontmatter block that starts on line 0, else 0. `h1` = the first line at or after `start` that starts with `# ` (never search inside the frontmatter: a `# comment` line there is legal and would put the section inside the frontmatter). Insert at the first line after `h1` (or at or after `start` when there is no H1) that starts with `## `; if there is none, at EOF. Reason: the section runs to the next `## ` line, so inserting directly after the H1 would pull any owner lines between the H1 and the first `## ` into the agents' section (`list` would show them; the curator would be barred from them). On the real file (`# Todos`, blank line, `## Current …`) both rules give the same bytes, so spec test 2 is unchanged. Block = `[HEADING + "\n", *item_lines]`; prepend `"\n"` when the line before the insertion point exists and is not blank; append `"\n"` when a line follows the insertion point. If the line before the insertion point has no line ending, append `"\n"` to it first.
- Item lines: `f"- [ ] {title} ({today:%Y-%m-%d}, {source}, id {id})\n"` then `f"  - {d}\n"` per detail.
- Empty detail after stripping is refused (`detail N: empty`).
- The secret scan runs on `"\n".join([title, *details])` (stripped values).

- [ ] **Step 1: Write the fixture `wiki/tests/fixtures/todos.md`**

Synthetic, same shape as the owner's real file (frontmatter with `tags: [people]`, an H1, owner `##` sections with `###` subsections, blank items with trailing spaces, a wikilink, an indented sub-list). Exact content, note the trailing space after `- ` and `- [ ] `:

```
---
type: log
tags: [people]
updated: 2026-11-07
---

# Todos

## Current (1 Nov 2026)
### Shopping
- 
### Errands
- [ ] 
- [ ] Buy a desk lamp

### Work
- [ ] Draft the quarterly report — see [[report-page]]

## Moving house
- [ ] Book the van
  - call two companies

## See also
- [[index]]
```

- [ ] **Step 2: Write the failing tests in `wiki/tests/test_todos.py`**

Helpers in the test file: `FIX = Path(__file__).parent / "fixtures" / "todos.md"` (always read with `encoding="utf-8"`: the fixture has an em dash); `TODAY = date(2026, 11, 13)`; `ids(*xs)` returns a `new_id` callable yielding `xs` in order; `with_section(body: str) -> str` returns the fixture with `"## Agent follow-ups\n" + body + "\n"` inserted directly after `"# Todos\n\n"`. Each test names the mutation it catches.

```python
def test_fixture_keeps_trailing_spaces():  # guards the fixture itself: an editor that strips trailing spaces would weaken spec test 1
    text = FIX.read_text(encoding="utf-8")
    assert "\n- \n" in text and "\n- [ ] \n" in text and "\u2014" in text

def test_add_without_section_inserts_after_h1():  # spec 2; catches append-at-EOF, insert-before-H1
    old = FIX.read_text()
    new, i = todos.add_item(old, "Merge PR 55", ["CI green"], source="claude-laptop", today=TODAY, new_id=ids("a1b2c3"))
    assert i == "a1b2c3"
    block = "## Agent follow-ups\n- [ ] Merge PR 55 (2026-11-13, claude-laptop, id a1b2c3)\n  - CI green\n\n"
    assert new == old.replace("# Todos\n\n", "# Todos\n\n" + block, 1)

def test_outside_bytes_identical_after_add_and_done():  # spec 1; catches whole-file rewrite, whitespace normalising, frontmatter re-dump
    old = with_section("- [ ] One (2026-11-12, claude-box, id 111111)\n")
    a, b = old.index("## Agent follow-ups"), old.index("## Current")
    new, _ = todos.add_item(old, "Two", [], source="claude-box", today=TODAY, new_id=ids("222222"))
    assert new[:a] == old[:a] and new.endswith(old[b:])
    done = todos.tick_item(new, "111111")
    assert done == new.replace("- [ ] One", "- [x] One", 1)

def test_add_into_section_goes_last_before_next_h2():  # spec 3; catches off-by-one into the owner's next section
    old = with_section("- [ ] One (2026-11-12, claude-box, id 111111)\n- [ ] Two (2026-11-12, claude-box, id 222222)\n")
    new, _ = todos.add_item(old, "Three", [], source="claude-box", today=TODAY, new_id=ids("333333"))
    sec = new[new.index("## Agent follow-ups"):new.index("## Current")]
    assert sec.splitlines()[1:] == ["- [ ] One (2026-11-12, claude-box, id 111111)",
                                    "- [ ] Two (2026-11-12, claude-box, id 222222)",
                                    "- [ ] Three (2026-11-13, claude-box, id 333333)", ""]

def test_frontmatter_stays_private():  # spec 4; catches any frontmatter change dropping `people`
    new, _ = todos.add_item(FIX.read_text(), "T", [], source="s", today=TODAY, new_id=ids("abcdef"))
    assert schema.is_private(frontmatter.parse(new)[0]) is True

def test_done_matches_id_not_title():  # spec 8
    old = with_section("- [ ] Same (2026-11-12, claude-box, id 111111)\n- [ ] Same (2026-11-12, claude-box, id 222222)\n")
    new = todos.tick_item(old, "222222")
    assert "- [ ] Same (2026-11-12, claude-box, id 111111)" in new
    assert "- [x] Same (2026-11-12, claude-box, id 222222)" in new

def test_done_ignores_id_outside_section():  # spec 9; catches matching outside the section
    old = with_section("").replace("- [ ] Book the van", "- [ ] Book the van (2026-11-01, claude-laptop, id abc123)")
    with pytest.raises(todos.TodoError) as e:
        todos.tick_item(old, "abc123")
    assert e.value.code == 1

def test_done_ignores_indented_lines():  # Review Focus 4
    old = with_section("- [ ] Real (2026-11-12, claude-box, id 111111)\n  - [ ] see (2026-11-01, claude-box, id abc123)\n")
    with pytest.raises(todos.TodoError):
        todos.tick_item(old, "abc123")

def test_done_already_ticked_returns_none():  # catches rewriting a ticked item / failing on it
    assert todos.tick_item(with_section("- [x] Old (2026-11-12, claude-box, id 111111)\n"), "111111") is None

@pytest.mark.parametrize("fn", [
    lambda t: todos.add_item(t, "T", [], source="s", today=TODAY, new_id=ids("abcdef")),
    lambda t: todos.tick_item(t, "111111"),
    lambda t: todos.list_items(t, True),
])
def test_duplicate_heading_refused(fn):  # spec 10
    old = with_section("- [ ] One (2026-11-12, claude-box, id 111111)\n").replace("## See also", "## Agent follow-ups\n\n## See also")
    with pytest.raises(todos.TodoError) as e: fn(old)
    assert e.value.code == 1

def test_list_skips_ticked_unless_all():  # spec 11
    old = with_section("- [ ] Open (2026-11-12, claude-box, id 111111)\n- [x] Shut (2026-11-12, claude-box, id 222222)\n- [ ] hand-written\n")
    assert [(i.id, i.title) for i in todos.list_items(old, False)] == [("111111", "Open"), (None, "hand-written")]
    assert [i.id for i in todos.list_items(old, True)] == ["111111", "222222", None]

def test_list_and_done_without_section():  # list → [], done → code 1
    assert todos.list_items(FIX.read_text(), True) == []
    with pytest.raises(todos.TodoError): todos.tick_item(FIX.read_text(), "111111")

def test_validation_boundaries():  # spec 12; catches < vs <= on every limit
    assert todos.validate("  " + "a" * 120 + " ", [])[0] == "a" * 120
    assert todos.validate("t", ["d"] * 15)[1] == ["d"] * 15
    todos.validate("t", ["x" * 300])
    for title, details in [("a" * 121, []), ("t", ["d"] * 16), ("t", ["x" * 301]), ("   ", []),
                           ("a\nb", []), ("a\tb", []), ("t", ["a\u2028b"]), ("t", [" "])]:
        with pytest.raises(todos.TodoRejected) as e: todos.validate(title, details)
        assert e.value.code == 2

def test_secret_in_detail_refused_with_pattern_name():  # spec 7 (pure half)
    with pytest.raises(todos.TodoRejected) as e:
        todos.validate("Rotate key", ["token ghp_" + "a" * 36])
    assert any("github-token" in x for x in e.value.errors)

def test_new_id_regenerated_on_clash():  # catches skipping the uniqueness check
    old = with_section("- [ ] One (2026-11-12, claude-box, id 111111)\n")
    assert todos.add_item(old, "T", [], source="s", today=TODAY, new_id=ids("111111", "222222"))[1] == "222222"

def test_add_at_eof_without_newline():  # Review Focus 1
    old = "# Todos\n\n## Agent follow-ups\n- [ ] One (2026-11-12, claude-box, id 111111)"
    new, _ = todos.add_item(old, "Two", [], source="s", today=TODAY, new_id=ids("222222"))
    assert new == old + "\n- [ ] Two (2026-11-13, s, id 222222)\n"

def test_h3_inside_section_is_not_a_boundary():  # Review Focus 2
    old = with_section("- [ ] One (2026-11-12, claude-box, id 111111)\n### Later\n- [ ] note\n")
    new, _ = todos.add_item(old, "Two", [], source="s", today=TODAY, new_id=ids("222222"))
    assert "- [ ] note\n- [ ] Two (2026-11-13, s, id 222222)\n\n## Current" in new

def test_no_h1_no_h2_appends_at_eof():  # catches inserting right after the frontmatter, which would absorb "owner item"
    old = "---\ntags: [people]\n---\n- [ ] owner item\n"
    new, _ = todos.add_item(old, "T", [], source="s", today=TODAY, new_id=ids("abcdef"))
    assert new == old + "\n## Agent follow-ups\n- [ ] T (2026-11-13, s, id abcdef)\n"
    assert [i.title for i in todos.list_items(new, True)] == ["T"]

def test_owner_lines_under_h1_stay_outside_section():  # catches inserting directly after the H1
    old = "# Todos\n- [ ] owner item\n\n## Current\n- [ ] x\n"
    new, _ = todos.add_item(old, "T", [], source="s", today=TODAY, new_id=ids("abcdef"))
    assert new == "# Todos\n- [ ] owner item\n\n## Agent follow-ups\n- [ ] T (2026-11-13, s, id abcdef)\n\n## Current\n- [ ] x\n"
    assert [i.title for i in todos.list_items(new, True)] == ["T"]

def test_heading_search_skips_frontmatter():  # catches searching for "# " / "## " from line 0 instead of after the frontmatter
    old = "---\n## owner comment\ntags: [people]\n---\n# Todos\n\n## Current\n"
    new, _ = todos.add_item(old, "T", [], source="s", today=TODAY, new_id=ids("abcdef"))
    assert new.startswith("---\n## owner comment\ntags: [people]\n---\n# Todos\n\n## Agent follow-ups\n")
    assert schema.is_private(frontmatter.parse(new)[0]) is True
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd wiki && uv run pytest -q tests/test_todos.py`
Expected: collection error / FAIL, `module 'hermes_wiki' has no attribute 'todos'` or ImportError.

- [ ] **Step 4: Implement the Task 1 interfaces in `wiki/hermes_wiki/todos.py`** per the decisions above.

- [ ] **Step 5: Run to verify they pass, then the whole suite**

Run: `cd wiki && uv run pytest -q tests/test_todos.py && uv run pytest -q`
Expected: all pass; full suite `402 + <new> passed`.

- [ ] **Step 6: Mutation check.** Change `line.startswith("## ")` to `line.startswith("##")` → `test_h3_inside_section_is_not_a_boundary` fails. Change `MAX_TITLE` comparison `>` to `>=` → `test_validation_boundaries` fails. Restore both.

- [ ] **Step 7: Commit**

```bash
git add wiki/hermes_wiki/todos.py wiki/tests/test_todos.py wiki/tests/fixtures/todos.md
git commit -m "feat(wiki): todos.py parses and edits the Agent follow-ups section"
```

---

### Task 2: Contents store, compare-and-swap loop, and the three commands

**Files:**
- Modify: `wiki/hermes_wiki/todos.py`
- Test: `wiki/tests/test_todos_store.py`

**Interfaces:**
- Consumes: Task 1 (`validate`, `add_item`, `tick_item`, `list_items`, `TodoError`, `TodoRejected`, `Item`); `hermes_wiki.config.WikiConfig` (`repo`, `gh`, `source`, `public_only`, `state_dir`).
- Produces (in `hermes_wiki.todos`):
  - `PATH = "todos.md"`, `ATTEMPTS = 5`
  - `class Conflict(Exception)`, `class WriteUncertain(Exception)`, `class StoreError(Exception)`
  - Store protocol: `get(path: str) -> tuple[str, str] | None` (text, sha; `None` = not on main) and `put(path: str, text: str, sha: str, message: str) -> None` raising `Conflict` / `WriteUncertain` / `StoreError`.
  - `class GhContents: __init__(self, repo: str, gh: str, run)` — `run` is required (no default); the CLI passes `subprocess.run`.
  - `class DryContents: __init__(self, dir: Path)` — file `dir / path`; sha = `hashlib.sha256(text.encode()).hexdigest()`; `put` with a stale sha raises `Conflict`; `put` creates `dir` if needed.
  - `store_for(cfg: WikiConfig, run) -> GhContents | DryContents` — `DryContents(cfg.state_dir / "dry")` when `os.environ.get("HERMES_WIKI_DRY_PUBLISH") == "1"`, else `GhContents(cfg.repo, cfg.gh, run)`.
  - `update(store, edit: Callable[[str], tuple[str, str] | None], *, sleep) -> bool` — `edit(text)` returns `(new_text, commit_message)` or `None` for "nothing to write"; returns whether a PUT landed.
  - `cmd_add(cfg, store, title: str, details: list[str], *, today: date | None = None, new_id: Callable[[], str] | None = None, sleep=time.sleep) -> str` (the id)
  - `cmd_done(cfg, store, item_id: str, *, sleep=time.sleep) -> bool` (False = was already ticked)
  - `cmd_list(cfg, store, include_done: bool) -> list[Item]`

**Decisions:**
- `GhContents.get`: argv `[gh, "api", f"repos/{repo}/contents/{path}?ref=main"]`, `capture_output=True, text=True, timeout=30`. Non-zero exit with `(HTTP 404)` in stderr → `None`; other non-zero, `TimeoutExpired` or `OSError` → `StoreError` (first stderr line only). Parse stdout JSON, `base64.b64decode(data["content"]).decode("utf-8")`, sha `data["sha"]`.
- `GhContents.put`: argv `[gh, "api", "-X", "PUT", f"repos/{repo}/contents/{path}", "-f", f"message={message}", "-f", f"content={b64}", "-f", f"sha={sha}", "-f", "branch=main"]`, timeout 30. `(HTTP 409)` in stderr → `Conflict`; `TimeoutExpired` → `WriteUncertain`; `OSError` from `run` (gh missing) or any other non-zero exit → `StoreError`.
- `update`: up to `ATTEMPTS` rounds of GET → edit → PUT. GET `None` → `TodoError("todos.md not found on main")`. `Conflict` on the last round → `TodoError("todos.md kept changing; gave up after 5 attempts")`; otherwise `sleep(0.5 * 2 ** attempt + random.uniform(0, 0.5))` and re-GET (edit re-runs on the new text). `WriteUncertain` → `TodoError("timed out writing todos.md; the write may or may not have landed")`. `StoreError(e)` → `TodoError(f"GitHub: {e}")`.
- `cmd_add` validates first (no store call on failure), uses `datetime.now(timezone.utc).date()` and `lambda: secrets.token_hex(3)` when not injected, and returns the id from the edit of the round that landed. Message `todo: add {id}`.
- `cmd_done`: edit returns `None` when `tick_item` returns `None`; message `todo: done {id}`.
- `cmd_list`: `cfg.public_only` → `TodoError("todo list is not available on a public_only wiki", code=2)` before touching `store`. Otherwise one GET, `None` → `TodoError` code 1.

- [ ] **Step 1: Write the failing tests** (`FakeStore` in the test file: a queue of `(text, sha)` GET results, a list of PUT outcomes (`None` or an exception instance), and logs `gets`, `puts` as `(text, sha, message)`; fixture `cfg` = `WikiConfig(root=tmp_path, source="claude-box", state_dir=tmp_path / "state")`; fixture `sleeps` returns a fresh `[]` per test and tests pass `sleep=sleeps.append`. Never a module-level `sleeps` list: it would accumulate across tests and make `len(sleeps) == 1` / `== 4` depend on test order. Every test below that uses `sleeps` takes it as a parameter, e.g. `def test_five_conflicts_give_up(cfg, sleeps):`).

```python
def test_conflict_regets_and_applies_edit_to_new_content(cfg):  # spec 5; catches retrying the stale body, no retry
    v1 = FIX.read_text(); v2 = v1.replace("- [ ] Buy a desk lamp", "- [x] Buy a desk lamp")
    st = FakeStore(gets=[(v1, "sha1"), (v2, "sha2")], puts=[todos.Conflict(), None])
    i = todos.cmd_add(cfg, st, "T", [], today=TODAY, new_id=lambda: "abcdef", sleep=sleeps.append)
    assert [p[1] for p in st.puts] == ["sha1", "sha2"]
    assert "- [x] Buy a desk lamp" in st.puts[1][0] and "id abcdef)" in st.puts[1][0]
    assert st.puts[1][2] == "todo: add abcdef" and len(sleeps) == 1

def test_five_conflicts_give_up(cfg):  # off-by-one on ATTEMPTS
    st = FakeStore(gets=[(FIX.read_text(), f"s{n}") for n in range(5)], puts=[todos.Conflict()] * 5)
    with pytest.raises(todos.TodoError) as e: todos.cmd_add(cfg, st, "T", [], sleep=sleeps.append)
    assert e.value.code == 1 and len(st.puts) == 5 and len(sleeps) == 4

def test_put_timeout_not_retried(cfg):  # spec 6; catches blind retry
    st = FakeStore(gets=[(FIX.read_text(), "s1")] * 2, puts=[todos.WriteUncertain(), None])
    with pytest.raises(todos.TodoError, match="may or may not") as e: todos.cmd_add(cfg, st, "T", [], sleep=sleeps.append)
    assert e.value.code == 1 and len(st.puts) == 1

def test_secret_title_makes_no_store_call(cfg):  # spec 7; catches scan after write / scan skipped
    st = FakeStore(gets=[], puts=[])
    with pytest.raises(todos.TodoRejected): todos.cmd_add(cfg, st, "key sk-" + "a" * 24, [], sleep=sleeps.append)
    assert st.gets == [] and st.puts == []

def test_done_unknown_id_and_duplicate_heading_make_no_put(cfg):  # spec 9, 10 through the loop
def test_done_already_ticked_makes_no_put(cfg):  # returns False, puts == []
def test_done_puts_tick_with_message(cfg, sleeps):  # one item 111111 open → returns True; puts[0][0] == text with "- [x]" on that line only; puts[0][1] is the GET sha; puts[0][2] == "todo: done 111111"
def test_missing_file_creates_nothing(cfg):  # GET None → TodoError code 1, puts == []
def test_list_public_only_makes_no_store_call(tmp_path):  # spec 13 (module half): code 2, gets == []

def test_gh_get_decodes_wrapped_base64():  # Review Focus 3
    body = FIX.read_text()
    out = json.dumps({"content": base64.encodebytes(body.encode()).decode(), "sha": "abc"})
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, "")
    assert todos.GhContents("o/r", "gh", run).get("todos.md") == (body, "abc")

def test_gh_get_404_is_none_and_argv():  # argv == ["gh", "api", "repos/o/r/contents/todos.md?ref=main"]; rc 1 "(HTTP 404)" → None; rc 1 "(HTTP 500)" → StoreError (catches treating every failure as "missing"); run raising OSError → StoreError
def test_gh_put_argv_and_errors():  # sha=, branch=main, message=, content= in argv; "(HTTP 409)" → Conflict; TimeoutExpired → WriteUncertain; rc 1 other → StoreError
def test_dry_store_round_trip_and_stale_sha(tmp_path):  # get None when absent; put then get; put with old sha → Conflict
def test_store_for_honours_dry_env(cfg, monkeypatch):  # env "1" → DryContents at cfg.state_dir/"dry"; unset → GhContents
```

Write the bodies of the nine one-line-described tests (`test_done_unknown_id_and_duplicate_heading_make_no_put` through `test_store_for_honours_dry_env`) with the assertions given in their comments.

- [ ] **Step 2: Run to verify they fail**

Run: `cd wiki && uv run pytest -q tests/test_todos_store.py`
Expected: FAIL, `AttributeError: module 'hermes_wiki.todos' has no attribute 'cmd_add'` (and similar).

- [ ] **Step 3: Implement the Task 2 interfaces** in `wiki/hermes_wiki/todos.py`.

- [ ] **Step 4: Run to verify they pass, then the whole suite**

Run: `cd wiki && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Mutation check.** Make `update` PUT the first GET's text on retry (cache `text` outside the loop) → `test_conflict_regets_and_applies_edit_to_new_content` fails. Make `WriteUncertain` fall through to the retry branch → `test_put_timeout_not_retried` fails. Restore.

- [ ] **Step 6: Commit**

```bash
git add wiki/hermes_wiki/todos.py wiki/tests/test_todos_store.py
git commit -m "feat(wiki): todo add/done/list over the GitHub contents API with sha retries"
```

---

### Task 3: `hermes-wiki todo` CLI

**Files:**
- Modify: `wiki/hermes_wiki/cli.py` (new `_cmd_todo`, `_add_todo`; append `_add_todo` to `_COMMANDS`)
- Test: `wiki/tests/test_cli_todo.py`

**Interfaces:**
- Consumes: Task 2 (`store_for`, `cmd_add`, `cmd_done`, `cmd_list`, `TodoError`, `TodoRejected`).
- Produces: `hermes-wiki todo add TITLE [--detail LINE]... [--detail-stdin]`, `hermes-wiki todo done ID`, `hermes-wiki todo list [--all]`.

**Decisions:**
- `todo` has nested subparsers `add`, `done`, `list` (required). Each nested parser also takes `--config` with `default=argparse.SUPPRESS`, so `hermes-wiki todo add T --config c.json` works and a `--config` given before `add` is not overwritten by the child's default.
- `--detail` is `action="append", default=[]`. With `--detail-stdin`, read `sys.stdin`, keep stripped non-blank lines, and append them after the `--detail` values.
- `add` prints `f"{id}\n"` to stdout and nothing else. `done` prints nothing on stdout; already ticked → stderr `hermes-wiki: {id} is already done`, exit 0. `list` prints `f"{item.id or '-'} {item.title}"` per item.
- `TodoRejected` → each error on stderr as `hermes-wiki: todo refused: {error}`, exit 2. `TodoError` → `hermes-wiki: {message}` on stderr, exit `e.code`.
- `store_for(cfg, subprocess.run)` is built inside `_cmd_todo`.

- [ ] **Step 1: Write the failing tests.** Config file in `tmp_path` with `root`, `source: "claude-box"`, `state_dir`, and `gh` pointing to a stub script in `tmp_path` that appends `"$@"` to a log file and exits 97. Tests that write use `monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")` and seed `state_dir/dry/todos.md` from the fixture.

```python
def test_add_prints_only_id_and_writes_dry_file(...):  # stdout matches r"\A[0-9a-f]{6}\n\Z"; dry file contains that id; gh log absent
def test_public_only_list_exit2_no_gh_call(...):  # spec 13: public_only true, NOT dry; rc 2; gh log absent
def test_public_only_add_prints_exactly_id(...):  # spec 13: public_only true, dry; stdout is one id line; "desk lamp" not in stdout+stderr
def test_detail_stdin_combines_after_flags(...):  # --detail a --detail-stdin with stdin "b\n\n c \n" → details ["a", "b", "c"] in that order
def test_secret_exit2_and_pattern_name(...):  # title with ghp_ token → rc 2, "github-token" in stderr, dry file unchanged
def test_done_then_done_again(...):  # first rc 0 ticks; second rc 0 with "already done" on stderr; unknown id rc 1
def test_list_all(...):  # list shows open only; --all shows both, format "<id> <title>"
def test_config_after_subcommand_args(...):  # ["todo", "add", "T", "--config", str(conf)] → rc 0
def test_missing_todos_rc1(...):  # dry mode, no dry file → rc 1, no file created
```

Use `capsys` with `cli.main([...])`; for stdin use `monkeypatch.setattr("sys.stdin", io.StringIO(...))` (replacing the stream object is not a module default).

- [ ] **Step 2: Run to verify they fail**

Run: `cd wiki && uv run pytest -q tests/test_cli_todo.py`
Expected: FAIL, argparse `invalid choice: 'todo'` (SystemExit 2).

- [ ] **Step 3: Implement `_add_todo` and `_cmd_todo` in `cli.py`.**

- [ ] **Step 4: Run to verify they pass, then the whole suite and the installed entry point**

Run: `cd wiki && uv run pytest -q && uv run hermes-wiki todo --help`
Expected: all pass; help lists `add`, `done`, `list`. This shows the subcommand ships in the existing `hermes-wiki` entry point that `wiki/install-laptop.sh` and `box/install.sh` link (`.venv/bin/hermes-wiki`), so neither installer changes. Record the command and output for the PR body.

- [ ] **Step 5: Mutation check.** Remove the `public_only` check in `cmd_list` → `test_public_only_list_exit2_no_gh_call` fails (stub gh is called, rc 1). Restore.

- [ ] **Step 6: Commit**

```bash
git add wiki/hermes_wiki/cli.py wiki/tests/test_cli_todo.py
git commit -m "feat(wiki): hermes-wiki todo add|done|list"
```

---

### Task 4: Curator rule and runbook

**Files:**
- Modify: `droplet/wiki-curator-prompt.md` (the `## Gardening` section)
- Modify: `runbooks/wiki.md` (intro paragraph; new `## Todo items` section before `## Day to day`)
- Test: `wiki/tests/test_worklist_shim.py` (new test next to `test_curator_prompt_states_page_keys_rule`)

- [ ] **Step 1: Write the failing test**

```python
def test_curator_prompt_leaves_agent_follow_ups_alone():
    text = " ".join(PROMPT.read_text(encoding="utf-8").split())
    assert "Never edit, move, split, trim or file anything into the `## Agent follow-ups` section of `todos.md`" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd wiki && uv run pytest -q tests/test_worklist_shim.py -k agent_follow_ups`
Expected: FAIL (assertion).

- [ ] **Step 3: Add the rule** to `## Gardening` as its own bullet or paragraph: "Never edit, move, split, trim or file anything into the `## Agent follow-ups` section of `todos.md`. Agents and the owner keep that section. The rest of `todos.md` follows the usual rules." Do not use any phrase in `INJECTION_PHRASES`.

- [ ] **Step 4: Runbook.** In the intro, change "Other agents … only add notes under `inbox/`" to say they only add notes under `inbox/`, except todo items (see "Todo items"). Add `## Todo items`: what writes where (laptop and box sessions run `hermes-wiki todo`, which edits `todos.md` on `main` through the GitHub contents API; Hermes on the droplet edits its checkout directly); the section rule (heading, boundary at the next `## `, outside bytes untouched, duplicate heading refused); the three commands with exit codes; `list` refused on the box (`public_only`); the curator leaves the section alone; ping vs todo split (time-bound → `remind-owner`, everything else → todo item); `remind-owner --cancel ID` instead of `hermes cron remove`, and `remind-owner --prune` (from Task 5).

- [ ] **Step 5: Run the wiki suite**

Run: `cd wiki && uv run pytest -q`
Expected: all pass, including `test_curator_prompt_has_rules_and_no_injection_phrases`.

- [ ] **Step 6: Commit**

```bash
git add droplet/wiki-curator-prompt.md runbooks/wiki.md wiki/tests/test_worklist_shim.py
git commit -m "docs(wiki): curator leaves Agent follow-ups alone; runbook for todo items"
```

---

### Task 5: `remind-owner --cancel` and `--prune` (Part B)

**Files:**
- Modify: `droplet/remind-owner.sh`
- Test: `box/tests/test_remind_owner.py`

**Interfaces:**
- Consumes: nothing from Tasks 1–4.
- Produces: `remind-owner --cancel JOB_ID`, `remind-owner --prune`; `remind-owner WHEN NAME < message` unchanged.

**Decisions:**
- Dispatch at the top, before `when=`: `case "${1:-}" in --cancel|--prune) ... exit;; esac`. `--cancel` without an id → usage on stderr, exit 2.
- Logic in one embedded `python3 - "$@" <<'PY'` block (stdlib only), reading `$HOME/.hermes/cron/jobs.json` and `$HOME/.hermes/scripts/`. Hermes stores `script` as a bare filename or `null` (MEASURED 2026-11-13 on hermes-vm); compare `os.path.basename(script)` when it is a string, ignore other values.
- Reminder name check: `re.fullmatch(r"remind-[0-9]{14}-[0-9]+\.sh", base)`.
- `jobs.json` unreadable, not JSON, not a dict, `jobs` not a list, or a job not a dict → stderr message, exit 1, nothing deleted (both flags).
- `--cancel`: no job with that `id` → exit 1. Script not a reminder name → exit 1 with `not a reminder job`. Else `subprocess.run(["hermes", "cron", "remove", job_id])`; non-zero → exit 1, files kept. Zero → `os.remove` the `.sh` and the `.txt` (ignore `FileNotFoundError`), exit 0.
- `--prune`: for each `remind-*.sh` in the scripts dir whose name fullmatches the pattern, is not a live job's script basename, and `time.time() - st_mtime > 600`: remove it and its `.txt`, print each removed path. Exit 0.
- Update the header comment with both new forms, and the `usage` string.

- [ ] **Step 1: Write the failing tests.** Extend `_run` with `jobs=None` (when a dict/str is given, write it to `home/.hermes/cron/jobs.json`; a str is written verbatim for the malformed case) and `files=()` (a sequence of `(base_name, age_seconds)`; each creates `base_name.sh` and `base_name.txt` in the scripts dir and sets both mtimes to `time.time() - age_seconds` with `os.utime`) and `extra=()` (a sequence of `(file_name, age_seconds)` for single non-pair files such as `loop0-reminder.sh`).

```python
def test_cancel_removes_job_and_only_its_files(tmp_path):  # spec 1; catches deleting by glob
    # jobs: j1 → remind-20261113094637-5830.sh, j2 → remind-20261113103942-30175.sh; both pairs exist
    # args ("--cancel", "j1") → rc 0; calls == [["cron", "remove", "j1"]]; j1 pair gone; j2 pair present
def test_cancel_refuses_non_reminder_job(tmp_path):  # spec 2: script "wiki-worklist.py" → rc 1, calls == [], files unchanged
def test_cancel_refuses_null_script_and_unknown_id(tmp_path):  # script None → rc 1; id "nope" → rc 1; calls == []
def test_cancel_keeps_files_when_remove_fails(tmp_path):  # spec 3: rc=3 → returncode != 0, pair still present
def test_prune_deletes_only_orphans(tmp_path):  # spec 4: live pair + orphan pair, both 11 min old → only orphan gone; stdout names both orphan paths
def test_prune_malformed_jobs_deletes_nothing(tmp_path):  # spec 5: jobs.json "{not json" → rc 1, orphan pair present; also jobs.json missing → rc 1
def test_prune_age_guard(tmp_path):  # spec 6: orphans aged 300 s and 660 s → 300 s kept, 660 s deleted
def test_prune_tolerates_null_scripts(tmp_path):  # Review Focus 5: jobs with script None and "loop0-reminder.sh"; files: loop0-reminder.sh, remind-notes.sh and one orphan pair, all aged 660 s → rc 0, orphan pair deleted, loop0-reminder.sh and remind-notes.sh both still present (catches dropping the fullmatch and globbing remind-*.sh)
def test_cancel_without_id_is_usage_error(tmp_path):  # rc 2, calls == []
```

The existing five tests must keep passing unchanged.

- [ ] **Step 2: Run to verify they fail**

Run: `cd box && uv run --with pytest python -m pytest -q tests/test_remind_owner.py`
Expected: the new tests FAIL (the script treats `--cancel` as WHEN and calls `hermes cron create`).

- [ ] **Step 3: Implement** in `droplet/remind-owner.sh`.

- [ ] **Step 4: Run tests and shellcheck**

Run: `cd box && uv run --with pytest python -m pytest -q tests/test_remind_owner.py && cd .. && uv run --with shellcheck-py shellcheck -S warning droplet/remind-owner.sh`
Expected: all pass; shellcheck prints nothing. (shellcheck is not installed on the laptop, MEASURED: `command -v shellcheck` printed nothing; CI installs it in the `box` job.)

- [ ] **Step 5: Mutation check.** Invert the membership test in prune → `test_prune_deletes_only_orphans` fails. Change `> 600` to `>= 0` → `test_prune_age_guard` fails. Move the file removal before the `hermes cron remove` call → `test_cancel_keeps_files_when_remove_fails` fails. Restore all three.

- [ ] **Step 6: Commit**

```bash
git add droplet/remind-owner.sh box/tests/test_remind_owner.py
git commit -m "feat(droplet): remind-owner --cancel and --prune"
```

---

## After the build (owner's session, not the builder)

From the spec, unchanged: rewrite "Reminders for me" in `~/.claude/CLAUDE.md` around the split; move jobs `8cd254d6253e` and `7319b2ed9950` to todo items and cancel them with `remind-owner --cancel`; keep `6f192f9fab05`; run `remind-owner --prune` once. The PR body states that neither installer changed and pastes the Task 3 Step 4 `--help` output.
