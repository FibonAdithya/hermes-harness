from hermes_wiki.reposcan import Checkout, GhRepo, Overrides, PageRef
import random

from hermes_wiki.repospage import Row, band, build_rows, private_names, private_names_in, render, same_page


def gh(slug, fork=False, archived=False, n=1, last="2026-11-20", desc=""):
    return GhRepo(slug, desc, fork, archived, n, last)


def co(host, path, slug, name=None, n=None):
    return Checkout(host, path, slug, name or path.rstrip("/").rsplit("/", 1)[-1], n,
                    "2026-11-01" if n is not None else None)


def no_readme(path):
    return None


# --- brief tests ---

def test_band_edges():
    assert [band(x) for x in (None, 0, 1, 19, 20)] == ["lookup failed", "dormant", "active", "active", "hot"]


def test_listing_rules():
    owned = [gh("FibonAdithya/chain"), gh("FibonAdithya/tokenmaxxer", fork=True), gh("FibonAdithya/old", archived=True)]
    cos = [co("laptop", "/h/.nvm", "nvm-sh/nvm"), co("laptop", "/h/MIP_gen", None, n=5),
           co("laptop", "/h/tokenmaxxer", "FibonAdithya/tokenmaxxer"),
           co("laptop", "/h/TIG/knowledge-graph", "tig-foundation/tig-knowledge-base")]
    rows = build_rows(cos, owned, {"tig-foundation/tig-knowledge-base": gh("tig-foundation/tig-knowledge-base")},
                      {}, Overrides(), lambda p: None)
    assert sorted(r.key for r in rows) == ["FibonAdithya/chain", "local:MIP_gen", "tig-foundation/tig-knowledge-base"]


def test_exclude_drops_listed_repo():
    rows = build_rows([], [gh("FibonAdithya/chain"), gh("FibonAdithya/notes")], {}, {},
                      Overrides(exclude=frozenset({"FibonAdithya/notes"})), lambda p: None)
    assert [r.key for r in rows] == ["FibonAdithya/chain"]


def test_include_beats_fork():
    rows = build_rows([], [gh("FibonAdithya/tokenmaxxer", fork=True)], {}, {},
                      Overrides(include=frozenset({"FibonAdithya/tokenmaxxer"})), lambda p: None)
    assert [r.key for r in rows] == ["FibonAdithya/tokenmaxxer"]


def test_two_checkouts_one_row_and_mirror_by_name():
    cos = [co("laptop", "/h/TIG/Talos", "FibonAdithya/talos"), co("laptop", "/h/talos-c3", "FibonAdithya/talos"),
           co("hermes-vm", "/root/repos/talos", None, name="talos"), co("hermes-vm", "/root/repos/stale", None, name="stale")]
    rows = build_rows(cos, [gh("FibonAdithya/talos")], {}, {}, Overrides(), lambda p: None)
    assert len(rows) == 1 and rows[0].paths == {"laptop": ["/h/TIG/Talos", "/h/talos-c3"], "hermes-vm": ["/root/repos/talos"]}


def test_slug_case_merges_and_keeps_activity():
    rows = build_rows([co("tig-adi", "/home/adi/talos", "fibonadithya/Talos")], [gh("FibonAdithya/talos", n=25)],
                      {}, {}, Overrides(), lambda p: None)
    assert len(rows) == 1 and rows[0].key == "FibonAdithya/talos" and rows[0].band == "hot"
    assert rows[0].paths == {"tig-adi": ["/home/adi/talos"]}


def test_mirror_never_lists_a_repo():
    rows = build_rows([co("hermes-vm", "/root/repos/x", None, name="x")], [gh("FibonAdithya/y")], {}, {}, Overrides(), lambda p: None)
    assert [r.key for r in rows] == ["FibonAdithya/y"]


def test_activity_from_github_not_local():
    rows = build_rows([co("laptop", "/h/hermes-harness", "FibonAdithya/hermes-harness")],
                      [gh("FibonAdithya/hermes-harness", n=37)], {}, {}, Overrides(), lambda p: None)
    assert rows[0].band == "hot"


def test_privacy_any_page_and_scrub():
    idx = {"valuebook": [PageRef("fleet", False, "x"), PageRef("valuebook", True, "Valuation.")],
           "fleet": [PageRef("fleet", False, "Runs fleet and valuebook jobs.")]}
    rows = build_rows([], [gh("FibonAdithya/valuebook"), gh("FibonAdithya/fleet", desc="Agents")], {}, idx,
                      Overrides(), lambda p: None)
    pub = [r for r in rows if not r.private]; priv = [r for r in rows if r.private]
    assert [r.key for r in priv] == ["FibonAdithya/valuebook"]
    assert pub[0].purpose == "Agents"
    assert private_names_in(render(pub, private=False, today="2026-11-24", host_notes=[]),
                            private_names(rows, idx, Overrides())) == []


def test_private_names_in_finds_word():
    assert private_names_in("see ValueBook here", ["valuebook"]) == ["valuebook"]
    assert private_names_in("valuebooks", ["valuebook"]) == []
    assert private_names_in("Demo-Swarm-X and ValueBook", ["demo-swarm-x", "ValueBook"]) == ["demo-swarm-x",
                                                                                               "valuebook"]


def test_private_names_covers_rows_private_pages_and_overrides():
    priv = [Row("FibonAdithya/valuebook", "FibonAdithya/valuebook", "", [], "dormant", None, {}, True),
            Row("FibonAdithya/chain", "FibonAdithya/chain", "", [], "dormant", None, {}, False)]
    idx = {"demo-swarm-x": [PageRef("hackpage", True, "x")], "someone/Secret": [PageRef("s", True, "x")],
           "fleet": [PageRef("fleet", False, "x")]}
    ov = Overrides(private=frozenset({"local:MoneyNotes", "Owner/Hidden", "bare"}))
    assert private_names(priv, idx, ov) == frozenset(
        {"valuebook", "demo-swarm-x", "secret", "moneynotes", "hidden", "bare"})


def test_unlisted_private_page_repo_is_scrubbed_from_public_rows():
    # hackpage.md is private and names demo-swarm-x, which never becomes a row.
    idx = {"demo-swarm-x": [PageRef("hackpage", True, "x")],
           "chain": [PageRef("chain", False, "Built on demo-swarm-x.")]}
    ov = Overrides()
    rows = build_rows([co("laptop", "/h/chain", "FibonAdithya/chain")],
                      [gh("FibonAdithya/chain", desc="Fork of demo-swarm-x.")], {}, idx, ov,
                      lambda p: "Clean readme.")
    assert [r.key for r in rows] == ["FibonAdithya/chain"] and not rows[0].private
    assert rows[0].purpose == "Clean readme."
    text = render(rows, private=False, today="2026-11-24", host_notes=[])
    assert private_names_in(text, private_names(rows, idx, ov)) == []


def test_slug_form_repos_key_makes_row_private():
    idx = {"fibonadithya/valuebook": [PageRef("valuebook", True, "Valuation.")]}
    rows = build_rows([], [gh("FibonAdithya/valuebook")], {}, idx, Overrides(), no_readme)
    assert rows[0].private and rows[0].pages == ["valuebook"]


def test_render_escapes_cells():
    r = Row("o/r", "o/r", "a|b\nc [[evil]]", ["p"], "hot", "2026-11-20", {"laptop": ["/h/a|b"]}, False)
    text = render([r], private=False, today="2026-11-24", host_notes=[])
    table = [l for l in text.splitlines() if l.startswith("| o/r")]
    assert len(table) == 1 and "[[evil]]" not in text and "[[p]]" in text and "[[repos-private]]" in text


def test_private_page_has_finance_tag_and_no_footer():
    text = render([], private=True, today="2026-11-24", host_notes=[])
    assert "tags: [coding, finance]" in text and "[[repos-private]]" not in text


def test_same_page_ignores_updated():
    a = render([], private=False, today="2026-11-24", host_notes=[])
    assert same_page(a, a.replace("2026-11-24", "2026-11-25"))
    assert not same_page(a, a + "x")


def test_sort_order():
    rows = build_rows([], [gh("o/a", n=0, last="2026-01-01"), gh("o/b", n=25, last="2026-11-01"),
                           gh("o/c", n=3, last="2026-11-20"), gh("o/d", n=3, last="2026-11-21")], {}, {},
                      Overrides(owners=("o",)), lambda p: None)
    assert [r.key for r in rows] == ["o/b", "o/d", "o/c", "o/a"]


# --- privacy contract ---

def test_private_page_makes_row_private_and_never_reaches_public_page():
    # One public and one private page name the repo. "any private" puts the row on the
    # private page; nothing from the private page (name or purpose) reaches the public page.
    idx = {"talos": [PageRef("alpha-secret", True, "Hidden plan."), PageRef("talos", False, "Solver.")]}
    rows = build_rows([], [gh("FibonAdithya/talos"), gh("FibonAdithya/chain", desc="Chain.")], {}, idx,
                      Overrides(), no_readme)
    talos = next(r for r in rows if r.key == "FibonAdithya/talos")
    assert talos.private
    assert talos.pages == ["alpha-secret", "talos"]  # a private row may link any page
    pub_text = render([r for r in rows if not r.private], private=False, today="2026-11-24", host_notes=[])
    assert "alpha-secret" not in pub_text and "Hidden plan" not in pub_text and "talos" not in pub_text.lower()


def test_public_row_links_and_purpose_only_from_public_pages():
    idx = {"chain": [PageRef("aaa", False, "First page."), PageRef("zzz", False, "Last page.")]}
    rows = build_rows([], [gh("FibonAdithya/chain")], {}, idx, Overrides(), no_readme)
    assert rows[0].pages == ["aaa", "zzz"] and rows[0].purpose == "First page." and not rows[0].private


def test_public_row_drops_page_link_naming_a_private_repo():
    # A public page whose filename contains a private repo's name would make the final
    # text check abort every run; the link is dropped from the public row instead.
    idx = {"valuebook": [PageRef("valuebook", True, "Valuation.")],
           "fleet": [PageRef("valuebook-notes", False, "Notes."), PageRef("fleet", False, "Runs agents.")]}
    rows = build_rows([], [gh("FibonAdithya/valuebook"), gh("FibonAdithya/fleet")], {}, idx, Overrides(), no_readme)
    fleet = next(r for r in rows if r.key == "FibonAdithya/fleet")
    assert fleet.pages == ["fleet"] and fleet.purpose == "Runs agents."
    assert private_names_in(render([fleet], private=False, today="2026-11-24", host_notes=[]),
                            private_names(rows, idx, Overrides())) == []


def test_override_private_matches_case_insensitively():
    rows = build_rows([co("laptop", "/h/moneynotes", None, n=0)], [], {}, {},
                      Overrides(private=frozenset({"local:MoneyNotes"})), no_readme)
    assert rows[0].private and rows[0].key == "local:moneynotes"


# --- purpose order and scrub ---

def test_purpose_override_wins_and_is_capped():
    long = "x" * 200
    rows = build_rows([], [gh("FibonAdithya/chain", desc="Desc.")], {}, {"chain": [PageRef("chain", False, "Page.")]},
                      Overrides(purpose={"fibonadithya/Chain": long}), no_readme)
    assert rows[0].purpose == "x" * 159 + "…"


def test_purpose_page_named_like_repo_beats_alphabetical():
    idx = {"chain": [PageRef("aaa", False, "First."), PageRef("chain", False, "Named.")]}
    rows = build_rows([], [gh("FibonAdithya/chain", desc="Desc.")], {}, idx, Overrides(), no_readme)
    assert rows[0].purpose == "Named."


def test_purpose_github_description_capped():
    rows = build_rows([], [gh("FibonAdithya/chain", desc="d" * 161)], {}, {}, Overrides(), no_readme)
    assert rows[0].purpose == "d" * 159 + "…" and len(rows[0].purpose) == 160


def test_purpose_readme_from_laptop_then_no_description():
    seen = []

    def readme(path):
        seen.append(path)
        return "From readme." if path == "/h/chain" else None

    cos = [co("tig-adi", "/a/chain", "FibonAdithya/chain"), co("laptop", "/h/chain", "FibonAdithya/chain")]
    rows = build_rows(cos, [gh("FibonAdithya/chain"), gh("FibonAdithya/bare")], {}, {}, Overrides(), readme)
    by = {r.key: r for r in rows}
    assert by["FibonAdithya/chain"].purpose == "From readme."
    assert by["FibonAdithya/bare"].purpose == "(no description)"
    assert "/a/chain" not in seen


def test_scrub_falls_through_every_source():
    idx = {"valuebook": [PageRef("valuebook", True, "Valuation.")],
           "fleet": [PageRef("fleet", False, "Feeds ValueBook.")]}
    cos = [co("laptop", "/h/fleet", "FibonAdithya/fleet")]
    rows = build_rows(cos, [gh("FibonAdithya/valuebook"), gh("FibonAdithya/fleet", desc="valuebook runner")], {}, idx,
                      Overrides(purpose={"FibonAdithya/fleet": "about valuebook"}),
                      lambda p: "Readme mentions valuebook.")
    fleet = next(r for r in rows if r.key == "FibonAdithya/fleet")
    assert fleet.purpose == "(no description)"


def test_scrub_does_not_apply_to_private_rows():
    idx = {"valuebook": [PageRef("valuebook", True, "Valuation for valuebook.")]}
    rows = build_rows([], [gh("FibonAdithya/valuebook")], {}, idx, Overrides(), no_readme)
    assert rows[0].purpose == "Valuation for valuebook."


# --- listing edge cases ---

def test_clone_owner_lookup_failed_is_listed_with_local_paths():
    cos = [co("laptop", "/h/TIG/kb", "tig-foundation/tig-knowledge-base")]
    rows = build_rows(cos, [], {"tig-foundation/tig-knowledge-base": None}, {}, Overrides(), lambda p: "Readme line.")
    assert len(rows) == 1
    r = rows[0]
    assert (r.key, r.band, r.last_commit, r.purpose) == ("tig-foundation/tig-knowledge-base", "lookup failed",
                                                         None, "Readme line.")
    assert r.paths == {"laptop": ["/h/TIG/kb"]}


def test_clone_owner_fork_in_extra_is_excluded():
    cos = [co("laptop", "/h/x", "tig-foundation/x")]
    rows = build_rows(cos, [], {"tig-foundation/x": gh("tig-foundation/x", fork=True)}, {}, Overrides(), no_readme)
    assert rows == []


def test_include_known_extra_and_ignored_local_include():
    rows = build_rows([], [], {"nvm-sh/nvm": gh("nvm-sh/nvm")}, {},
                      Overrides(include=frozenset({"nvm-sh/nvm", "local:ghost", "other/unknown"})), no_readme)
    assert [r.key for r in rows] == ["nvm-sh/nvm"]


def test_include_attaches_non_clone_owner_checkout():
    cos = [co("laptop", "/h/.nvm", "nvm-sh/nvm")]
    rows = build_rows(cos, [], {"nvm-sh/nvm": gh("nvm-sh/nvm")}, {}, Overrides(include=frozenset({"NVM-sh/nvm"})),
                      no_readme)
    assert rows[0].paths == {"laptop": ["/h/.nvm"]}


def test_include_beats_exclude():
    rows = build_rows([], [gh("FibonAdithya/chain")], {}, {},
                      Overrides(include=frozenset({"FibonAdithya/chain"}), exclude=frozenset({"fibonadithya/chain"})),
                      no_readme)
    assert [r.key for r in rows] == ["FibonAdithya/chain"]


def test_exclude_local_case_insensitive():
    rows = build_rows([co("laptop", "/h/MIP_gen", None, n=1)], [], {}, {},
                      Overrides(exclude=frozenset({"local:mip_gen"})), no_readme)
    assert rows == []


def test_local_activity_laptop_then_tig_adi_and_zero_is_dormant():
    cos = [co("tig-adi", "/home/adi/MIP_gen", None, n=30), co("laptop", "/h/MIP_gen", None, n=0)]
    rows = build_rows(cos, [], {}, {}, Overrides(), no_readme)
    assert rows[0].band == "dormant" and rows[0].paths == {"tig-adi": ["/home/adi/MIP_gen"], "laptop": ["/h/MIP_gen"]}
    rows = build_rows([co("tig-adi", "/home/adi/MIP_gen", None, n=30)], [], {}, {}, Overrides(), no_readme)
    assert rows[0].band == "hot"


def test_local_lookup_failed_on_laptop_falls_to_tig_adi():
    cos = [co("laptop", "/h/MIP_gen", None, n=None), co("tig-adi", "/home/adi/MIP_gen", None, n=4)]
    rows = build_rows(cos, [], {}, {}, Overrides(), no_readme)
    assert rows[0].band == "active"


def test_local_row_pages_by_local_name():
    rows = build_rows([co("laptop", "/h/MIP_gen", None, n=1)], [], {}, {"mip_gen": [PageRef("mip", False, "Solver.")]},
                      Overrides(), no_readme)
    assert rows[0].pages == ["mip"] and rows[0].purpose == "Solver."


def test_mirror_attaches_case_insensitively_to_owner_in_owners_only():
    cos = [co("hermes-vm", "/root/repos/Chain", None, name="Chain"),
           co("hermes-vm", "/root/repos/kb", None, name="kb")]
    rows = build_rows(cos, [gh("FibonAdithya/chain")], {"tig-foundation/kb": gh("tig-foundation/kb")}, {},
                      Overrides(include=frozenset({"tig-foundation/kb"})), no_readme)
    by = {r.key: r for r in rows}
    assert by["FibonAdithya/chain"].paths == {"hermes-vm": ["/root/repos/Chain"]}
    assert by["tig-foundation/kb"].paths == {}


def test_slug_display_falls_back_to_sorted_first_path_spelling():
    cos = [co("laptop", "/h/a", "TIG-Foundation/KB"), co("tig-adi", "/home/adi/kb", "tig-foundation/kb")]
    rows = build_rows(cos, [], {"tig-foundation/kb": None}, {}, Overrides(), no_readme)
    assert [r.key for r in rows] == ["TIG-Foundation/KB"] and rows[0].slug == "TIG-Foundation/KB"


# --- render ---

def _table_lines(text):
    return [l for l in text.splitlines() if l.startswith("|")]


def test_render_layout_and_columns():
    r = Row("o/r", "o/r", "Does things.", [], "active", "2026-11-20",
            {"tig-adi": ["/home/adi/r"], "laptop": ["/h/r", "/h/r2"]}, False)
    text = render([r], private=False, today="2026-11-24", host_notes=["tig-adi paths unknown (unreachable today)"])
    assert text.startswith("---\ntype: log\ntags: [coding]\nupdated: 2026-11-24\n---\n")
    assert ("<!-- Generated by hermes-wiki repos. Edits are lost; "
            "change ~/.config/hermes-wiki/repos.toml instead. -->") in text
    assert "tig-adi paths unknown (unreachable today)" in text
    lines = _table_lines(text)
    assert lines[0] == "| repo | purpose | activity | last commit | laptop | hermes-vm | tig-adi |"
    assert lines[2] == "| o/r | Does things. | active | 2026-11-20 | /h/r<br>/h/r2 |  | /home/adi/r |"
    assert text.rstrip("\n").endswith("Private repos: [[repos-private]]")


def test_render_local_row_and_lookup_failed():
    r = Row("local:MIP_gen", None, "(no description)", [], "lookup failed", None, {"laptop": ["/h/MIP_gen"]}, False)
    line = _table_lines(render([r], private=False, today="2026-11-24", host_notes=[]))[2]
    assert line == "| MIP_gen (local only) | (no description) | lookup failed |  | /h/MIP_gen |  |  |"


def test_render_escape_edge_cases():
    r = Row("o/r", "o/r", "back\\|slash [[[triple]] x\r\ny z <!-- hide", ["p|q"], "hot", None,
            {"laptop": ["/h/[[w]]"]}, False)
    text = render([r], private=False, today="2026-11-24", host_notes=[])
    rows = [l for l in text.splitlines() if l.startswith("| o/r")]
    assert len(rows) == 1
    line = rows[0]
    # every '|' in the line is either a cell separator or escaped by an odd run of backslashes
    cells = 0
    i = 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "|":
            cells += 1
        i += 1
    assert cells == 8
    assert "[[triple" not in text and "[[w" not in text and "<!-- hide" not in text
    assert "[[p\\|q]]" in line


def test_render_footer_appears_once():
    r = Row("o/r", "o/r", "x", [], "hot", "2026-11-20", {}, False)
    text = render([r], private=False, today="2026-11-24", host_notes=[])
    assert text.count("[[repos-private]]") == 1


def test_same_page_only_ignores_frontmatter_updated():
    a = render([], private=False, today="2026-11-24", host_notes=["updated: a"])
    b = render([], private=False, today="2026-11-24", host_notes=["updated: b"])
    assert not same_page(a, b)
    assert not same_page(a, a.replace("tags: [coding]", "tags: [coding, finance]"))


def test_same_page_does_not_ignore_updated_in_body():
    head = "---\ntype: log\nupdated: 2026-11-24\n---\n"
    assert same_page(head + "body\n", head.replace("24", "25") + "body\n")
    assert not same_page(head + "updated: x\n", head + "updated: y\n")


def test_sort_none_last_commit_last_within_band():
    cos = [Checkout("laptop", "/h/a", None, "a", 0, None), Checkout("laptop", "/h/b", None, "b", 0, "2020-01-01")]
    rows = build_rows(cos, [], {}, {}, Overrides(), no_readme)
    assert [r.key for r in rows] == ["local:b", "local:a"]


def test_pages_sorted_case_insensitively():
    idx = {"chain": [PageRef("Zeta", False, "Z."), PageRef("alpha", False, "A.")]}
    rows = build_rows([], [gh("FibonAdithya/chain")], {}, idx, Overrides(), no_readme)
    assert rows[0].pages == ["alpha", "Zeta"]


def test_checkout_order_does_not_change_the_page():
    cos = [co("laptop", "/h/zz/talos", "FibonAdithya/talos"), co("laptop", "/h/TIG/Talos", "fibonadithya/TALOS"),
           co("tig-adi", "/home/adi/b", "FibonAdithya/talos"), co("tig-adi", "/home/adi/a", "FibonAdithya/talos"),
           co("laptop", "/h/x/Mip", None, n=2), co("tig-adi", "/home/adi/mip", None, n=9),
           co("laptop", "/h/kb2", "tig-foundation/KB"), co("laptop", "/h/kb1", "Tig-Foundation/kb"),
           co("hermes-vm", "/root/repos/talos", None, name="talos"),
           co("hermes-vm", "/root/repos/Talos", None, name="Talos")]
    owned = [gh("FibonAdithya/talos")]
    extra = {"tig-foundation/kb": None}

    def page(order):
        rows = build_rows(order, owned, extra, {}, Overrides(), no_readme)
        return render(rows, private=False, today="2026-11-24", host_notes=[])

    want = page(cos)
    rng = random.Random(5)
    for _ in range(20):
        shuffled = cos[:]
        rng.shuffle(shuffled)
        assert page(shuffled) == want
    assert "/h/TIG/Talos<br>/h/zz/talos" in want and "Tig-Foundation/kb" in want


def test_bare_private_name_moves_local_and_slug_rows():
    cos = [co("laptop", "/h/MoneyNotes", None, n=1), co("laptop", "/h/ledger", "FibonAdithya/Ledger")]
    rows = build_rows(cos, [gh("FibonAdithya/Ledger"), gh("FibonAdithya/chain")], {}, {},
                      Overrides(private=frozenset({"MoneyNotes", "ledger"})), no_readme)
    assert {r.key: r.private for r in rows} == {"local:MoneyNotes": True, "FibonAdithya/Ledger": True,
                                                "FibonAdithya/chain": False}
    text = render([r for r in rows if not r.private], private=False, today="2026-11-24", host_notes=[])
    assert "moneynotes" not in text.lower() and "ledger" not in text.lower()


def test_no_private_overrides_means_public():
    rows = build_rows([co("laptop", "/h/MoneyNotes", None, n=1)], [], {}, {}, Overrides(), no_readme)
    assert not rows[0].private


def test_local_override_still_hides_repo_after_it_gains_an_origin():
    # `local:MoneyNotes` must still hide the repo once it gains an origin
    cos = [co("laptop", "/h/MoneyNotes", "FibonAdithya/MoneyNotes")]
    owned = [gh("FibonAdithya/MoneyNotes"), gh("FibonAdithya/chain")]
    ov = Overrides(private=frozenset({"local:MoneyNotes"}))
    rows = build_rows(cos, owned, {}, {}, ov, no_readme)
    assert {r.key: r.private for r in rows} == {"FibonAdithya/MoneyNotes": True, "FibonAdithya/chain": False}
    text = render([r for r in rows if not r.private], private=False, today="2026-11-24", host_notes=[])
    assert "moneynotes" not in text.lower()
    assert private_names_in(text, private_names(rows, {}, ov)) == []


def test_private_page_other_owner_slug_makes_owned_row_private():
    # a private page naming tig-foundation/valuebook hides FibonAdithya/valuebook too: privacy
    # follows the repo name, the same unit the public-page check scrubs by
    idx = {"tig-foundation/valuebook": [PageRef("fv", True, "Valuation.")]}
    ov = Overrides()
    rows = build_rows([], [gh("FibonAdithya/valuebook"), gh("FibonAdithya/chain")], {}, idx, ov, no_readme)
    assert {r.key: r.private for r in rows} == {"FibonAdithya/valuebook": True, "FibonAdithya/chain": False}
    text = render([r for r in rows if not r.private], private=False, today="2026-11-24", host_notes=[])
    assert private_names_in(text, private_names(rows, idx, ov)) == []


def test_include_with_failed_lookup_is_listed():
    # spec rule 4: an include slug is listed even when one_repo failed and nothing is checked out
    rows = build_rows([], [], {"Other/Thing": None}, {}, Overrides(include=frozenset({"other/thing"})),
                      no_readme)
    assert [(r.key, r.slug, r.band, r.purpose, r.paths) for r in rows] == [
        ("Other/Thing", "Other/Thing", "lookup failed", "(no description)", {})]
    rows = build_rows([], [], {"Other/Thing": None}, {},
                      Overrides(include=frozenset({"other/thing"}), purpose={"other/thing": "Hand-written."}),
                      no_readme)
    assert [(r.key, r.purpose) for r in rows] == [("Other/Thing", "Hand-written.")]
