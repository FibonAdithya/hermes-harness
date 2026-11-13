from datetime import date
from pathlib import Path

import pytest

from hermes_wiki import frontmatter, schema, todos

FIX = Path(__file__).parent / "fixtures" / "todos.md"
TODAY = date(2026, 11, 13)


def ids(*xs):
    it = iter(xs)
    return lambda: next(it)


def with_section(body: str) -> str:
    return FIX.read_text(encoding="utf-8").replace(
        "# Todos\n\n", "# Todos\n\n## Agent follow-ups\n" + body + "\n", 1)


def test_fixture_keeps_trailing_spaces():  # guards the fixture itself: an editor that strips trailing spaces would weaken spec test 1
    text = FIX.read_text(encoding="utf-8")
    assert "\n- \n" in text and "\n- [ ] \n" in text and "\u2014" in text

def test_add_without_section_inserts_after_h1():  # spec 2; catches append-at-EOF, insert-before-H1
    old = FIX.read_text(encoding="utf-8")
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
    new, _ = todos.add_item(FIX.read_text(encoding="utf-8"), "T", [], source="s", today=TODAY, new_id=ids("abcdef"))
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

def test_done_with_two_matching_lines_is_refused():  # catches ticking the first of several matches
    old = with_section("- [ ] A (2026-11-12, claude-box, id 111111)\n- [ ] B (2026-11-12, claude-box, id 111111)\n")
    with pytest.raises(todos.TodoError) as e:
        todos.tick_item(old, "111111")
    assert e.value.code == 1

def test_list_parses_crlf_item_line():  # catches a trailing \r breaking the title/id match
    old = with_section("- [ ] One (2026-11-12, claude-box, id 111111)\r\n")
    assert todos.list_items(old, True) == [todos.Item(id="111111", title="One", done=False)]

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
    assert todos.list_items(FIX.read_text(encoding="utf-8"), True) == []
    with pytest.raises(todos.TodoError): todos.tick_item(FIX.read_text(encoding="utf-8"), "111111")

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
