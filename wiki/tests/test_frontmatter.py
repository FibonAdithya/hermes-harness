from hermes_wiki import frontmatter


def test_parses_scalars_lists_quotes_and_comments():
    text = (
        "---\ntype: project  # comment\ntags: [hermes, cost]\nempty: []\n"
        'verified: "a: b # c"\n---\nBody\n'
    )
    meta, body = frontmatter.parse(text)
    assert meta == {
        "type": "project",
        "tags": ["hermes", "cost"],
        "empty": [],
        "verified": "a: b # c",
    }
    assert body == "Body\n"


def test_hr_page_is_not_frontmatter():
    text = "---\n\nSome prose with a colon: here and more prose\n---\nrest"
    assert frontmatter.parse(text) == (None, text)


def test_unclosed_block_is_not_frontmatter():
    text = "---\ntype: x\nno close"
    assert frontmatter.parse(text) == (None, text)


def test_close_beyond_40_lines_is_not_frontmatter():
    text = "---\n" + "a: b\n" * 45 + "---\nrest"
    assert frontmatter.parse(text) == (None, text)


def test_first_line_must_be_exactly_dashes():
    text = "--- \ntype: x\n---\nrest"
    assert frontmatter.parse(text) == (None, text)


def test_roundtrip():
    meta = {"title": 'a: b # "c"', "tags": ["x", "y"], "none": [], "plain": "ok"}
    body = "Body text\nmore\n"
    assert frontmatter.parse(frontmatter.dump(meta, body)) == (meta, body)


def test_dump_quotes_only_when_needed():
    out = frontmatter.dump({"a": "plain", "b": "x: y", "c": " pad"}, "")
    assert 'a: plain\n' in out
    assert 'b: "x: y"\n' in out
    assert 'c: " pad"\n' in out


def test_list_items_are_unquoted():
    meta, _ = frontmatter.parse("---\ntags: [\"finance\", 'health', plain]\n---\nx\n")
    assert meta == {"tags": ["finance", "health", "plain"]}


def test_repeated_key_is_not_frontmatter():
    text = "---\ntype: topic\ntags: [finance]\ntags: [hermes]\n---\nx\n"
    assert frontmatter.parse(text) == (None, text)
