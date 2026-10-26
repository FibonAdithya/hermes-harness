import pytest

from hermes_wiki import pages as pg
from hermes_wiki import search as sr
from wikifixture import build_wiki


@pytest.fixture
def root(tmp_path):
    return build_wiki(tmp_path)


def names(results):
    return [r["page"] for r in results]


def test_query_orders_by_matches_and_skips_inbox_and_infra(root):
    got = names(sr.search(root, "deploy"))
    assert got == ["alpha", "beta", "delta"]
    assert not any(n.startswith("inbox") or n == "index" for n in names(sr.search(root, "")))


def test_result_shape(root):
    r = sr.search(root, "droplet")[0]
    assert r["page"] == "alpha" and r["type"] == "project"
    assert r["tags"] == ["hermes", "cost"] and r["updated"] == "2026-10-01"
    assert r["lines"] == ["We moved the droplet and ran deploy again."]


def test_tag_filter_is_all_of(root):
    assert set(names(sr.search(root, "", tags=["cost"]))) == {"alpha", "beta"}
    assert names(sr.search(root, "", tags=["cost", "hermes"])) == ["alpha"]


def test_type_and_updated_after(root):
    assert names(sr.search(root, "", type="decision")) == ["beta"]
    assert names(sr.search(root, "", updated_after="2026-10-10")) == ["beta"]
    assert "delta" not in names(sr.search(root, "", type="decision"))


def test_repos_filter_is_any_of(root, tmp_path):
    extra = {"r1": "---\ntype: topic\ntags: [ops]\nrepos: [x, y]\n---\nbody\n"}
    r = build_wiki(tmp_path / "w", extra_pages=extra)
    assert names(sr.search(r, "", repos=["y", "z"])) == ["r1"]
    assert sr.search(r, "", repos=["z"]) == []


def test_public_only_hides_private(root):
    got = names(sr.search(root, "", public_only=True))
    assert "gamma" not in got and "delta" not in got
    assert {"alpha", "beta", "nights/2026-09-25"} <= set(got)


def test_missing_updated_sorts_last_and_limit(root):
    assert names(sr.search(root, "deploy", limit=2)) == ["alpha", "beta"]
    assert names(sr.search(root, "", tags=[], limit=0)) == []


def test_read_follow_links(root):
    text = sr.read(root, "alpha", follow_links=True)
    assert "--- linked: beta ---" in text and "--- linked: gamma ---" in text
    pub = sr.read(root, "alpha", follow_links=True, public_only=True)
    assert "--- linked: beta ---" in pub and "gamma" not in pub.replace("[[gamma|g]]", "")


def test_read_plain_returns_page_text(root):
    assert sr.read(root, "beta").startswith("---\ntype: decision")


@pytest.mark.parametrize("name,public", [
    ("../x", False), ("/etc/passwd", False), ("a/../../b", False),
    ("index", False), ("nope", False), ("gamma", True), ("inbox/claude-laptop/x", False),
])
def test_read_rejects_traversal_and_private_alike(root, name, public):
    with pytest.raises(sr.PageNotFound) as e:
        sr.read(root, name, public_only=public)
    assert str(e.value) == f"no page {name!r}"


def test_links_and_resolve(root):
    assert pg.links("[[a]] [[b|x]] [[c#h]] [[a]]") == ["a", "b", "c"]
    pages = pg.iter_pages(root)
    assert pg.resolve("2026-09-25", pages).name == "nights/2026-09-25"
    assert pg.resolve("alpha", pages).name == "alpha"
    assert pg.resolve("nonexistent", pages) is None


def test_resolve_ambiguous_basename_is_none(tmp_path):
    r = build_wiki(tmp_path, extra_pages={"a/same": "x\n", "b/same": "y\n"})
    assert pg.resolve("same", pg.iter_pages(r)) is None


def test_inbound(root):
    pages = pg.iter_pages(root)
    inb = pg.inbound(pages)
    assert "alpha" in inb["beta"] and "alpha" in inb["gamma"]
    assert inb["alpha"] == {"index"}


def test_valid_name():
    assert pg.valid_name("a") and pg.valid_name("nights/2026-09-25")
    for bad in ["", "..", "a/..", "../a", "/a", "a/b/c", "a//b", "-a", "a b", "a\n"]:
        assert not pg.valid_name(bad), bad


def test_iter_pages_skips_inbox_and_git(tmp_path):
    r = build_wiki(tmp_path)
    (r / ".git").mkdir()
    (r / ".git" / "z.md").write_text("z")
    got = [p.name for p in pg.iter_pages(r)]
    assert got == sorted(got) and "alpha" in got
    assert not any(n.startswith(("inbox", ".git")) for n in got)
    assert {p.name: p.infra for p in pg.iter_pages(r)}["index"] is True


def test_multi_term_query_is_all_of(root):
    assert names(sr.search(root, "deploy alpha-only-word")) == ["alpha"]


def test_related_scores_shared_tokens(root):
    pages = pg.iter_pages(root)
    assert pg.related("we changed the droplet deploy schedule", pages) == ["alpha"]


def test_related_caps_k_and_orders(tmp_path):
    extra = {n: "droplet deploy schedule\n" for n in ("p1", "p2", "p3", "p4")}
    r = build_wiki(tmp_path, extra_pages=extra)
    pages = pg.iter_pages(r)
    assert pg.related("droplet deploy schedule", pages, k=3) == ["p1", "p2", "p3"]


def test_words_excludes_frontmatter(tmp_path):
    fm = "---\ntype: topic\ntags: [ops]\nupdated: 2026-10-01\n---\n"
    r = build_wiki(tmp_path, extra_pages={"w": fm + "one two three four five six seven\n"})
    page = next(p for p in pg.iter_pages(r) if p.name == "w")
    assert page.words == 7


def test_fixture_index_and_tags_are_complete(root):
    index = (root / "index.md").read_text()
    for p in pg.iter_pages(root):
        if not p.infra:
            assert f"[[{p.name}]]" in index
    from hermes_wiki.schema import load_tags
    assert {"hermes", "cost", "people", "ops"} <= load_tags(root)


def test_type_filter_is_normalised(tmp_path):
    extra = {"d2": '---\ntype: " Decision"\ntags: [ops]\n---\nbody\n'}
    root = build_wiki(tmp_path, extra_pages=extra)
    assert names(sr.search(root, "", type="decision")) == ["beta", "d2"]
    assert names(sr.search(root, "", type="DECISION ")) == ["beta", "d2"]
