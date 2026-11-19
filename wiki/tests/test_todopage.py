import random
from datetime import date, timedelta

from hermes_wiki import schema
from hermes_wiki.frontmatter import parse
from hermes_wiki.todopage import data_marker, owner_actions, render, ticks
from hermes_wiki.todorecords import Record

TODAY = date(2026, 11, 19)


def rec(i, title="t", project="alpha", created="2026-11-10", **kw):
    return Record(id=i, title=title, details=kw.pop("details", []), project=project,
                  created=created, source="agent", **kw)


def done(i, days_ago, **kw):
    at = (TODAY - timedelta(days=days_ago)).isoformat()
    return rec(i, status="done", closed={"at": at, "by": "owner", "reason": "ticked"}, **kw)


def headings(page):
    return [ln for ln in page.splitlines() if ln.startswith("## ")]


def test_groups_order_and_unsorted_last():
    sugg = rec("aaaaaa", status="suggested",
               suggestion={"at": "2026-11-18", "reason": "merged", "evidence": "pr"})
    page = render([rec("111111", project="zeta"), rec("222222", project="unsorted"),
                   rec("333333", project="alpha"), sugg], TODAY)
    assert headings(page) == ["## Probably done, confirm", "## alpha", "## zeta", "## unsorted"]
    # catches a plain sort putting unsorted mid-list


def test_recently_done_window():
    page = render([done("111111", 6), done("222222", 7)], TODAY)
    assert "id 111111)" in page and "id 222222)" not in page
    assert "## Recently done" in page
    # catches a <= boundary on the 7-day window


def test_render_deterministic_and_date_independent():
    recs = [rec("111111", created="2026-11-01"), rec("222222", project="beta"),
            done("333333", 2), rec("444444", created="2026-11-01")]
    a = render(recs, TODAY)
    assert render(recs, TODAY + timedelta(days=1)) == a
    shuffled = recs[:]
    random.Random(1).shuffle(shuffled)
    assert render(shuffled, TODAY) == a
    # catches updated: today, and input-order dependence


def test_frontmatter_private():
    meta, _ = parse(render([rec("111111")], TODAY))
    assert meta == {"type": "log", "tags": ["people"], "updated": "2026-11-10"}
    assert schema.is_private(meta, allowed_tags={"people"}) is True
    # catches dropping the people tag or a wrong updated


def test_updated_is_latest_record_date():
    r = rec("111111", created="2026-11-01", status="suggested",
            suggestion={"at": "2026-11-15", "reason": "x", "evidence": "e"})
    meta, _ = parse(render([r, done("222222", 3, created="2026-11-02")], TODAY))
    assert meta["updated"] == "2026-11-16"
    # catches updated taking only created dates


def test_ticks_roundtrip():
    sugg = rec("333333", status="suggested",
               suggestion={"at": "2026-11-18", "reason": "merged", "evidence": "pr"})
    page = render([rec("111111", details=["a detail"]), sugg, done("222222", 1)], TODAY)
    assert ticks(page) == {"111111": False, "333333": False, "222222": True}
    assert "  - Looks done: merged (curator, 2026-11-18)" in page
    assert "  - Closed " in page and " by owner: ticked" in page
    assert "- [ ] t (2026-11-10, id 111111)\n  - a detail\n" in page
    # catches parser/renderer format drift


def test_ticks_ignore_lookalikes():
    r = rec("111111", title="sneaky id abcdef)", details=["- [x] fake (2026-11-01, id 123456)"])
    page = render([r], TODAY)
    assert ticks(page) == {"111111": False}
    # catches an unanchored regex (the detail half); the title half passes only because the match is greedy to the last id


def test_render_empty_store():
    page = render([], TODAY)
    meta, _ = parse(page)
    assert meta == {"type": "log", "tags": ["people"]}
    assert schema.is_private(meta, allowed_tags={"people"}) is True
    assert ticks(page) == {}
    # catches max() of an empty sequence


def test_duplicate_id_line_needs_all_checked():
    page = "- [x] a (2026-11-01, id 111111)\n- [ ] b (2026-11-01, id 111111)\n- [x] c (2026-11-01, id 222222)\n"
    assert ticks(page) == {"111111": False, "222222": True}
    assert ticks("- [X] a (2026-11-01, id 111111)\n- [x] b (2026-11-01, id 111111)\n") == {"111111": True}
    # catches first-match-wins


def test_data_marker_changes_with_records():
    a, b = render([rec("111111")], TODAY), render([rec("111111", title="u")], TODAY)
    ma, mb = data_marker(a), data_marker(b)
    assert len(ma) == 64 and ma != mb
    assert data_marker("no marker here\n") is None
    # catches a marker that ignores the records


def test_ticks_tolerate_crlf_and_trailing_space():
    page = "- [x] a (2026-11-01, id 111111)\r\n- [x] b (2026-11-01, id 222222)  \n"
    assert ticks(page) == {"111111": True, "222222": True}
    # catches plain `$` at end of tick regex


def test_same_created_tie_breaks_by_id():
    a, b = rec("aaaaaa", created="2026-11-05"), rec("bbbbbb", created="2026-11-05")
    p1, p2 = render([a, b], TODAY), render([b, a], TODAY)
    assert p1 == p2
    assert p1.index("id aaaaaa") < p1.index("id bbbbbb")
    # catches dropping the id sort before the stable created sort


def test_owner_actions_are_section_aware():
    page = render([rec("111111"), rec("222222"), done("333333", 1), done("444444", 2)], TODAY)
    page = page.replace("- [ ] t (2026-11-10, id 111111)", "- [x] t (2026-11-10, id 111111)")
    page = page.replace("- [x] t (alpha, 2026-11-10, id 333333)", "- [ ] t (alpha, 2026-11-10, id 333333)")
    assert owner_actions(page) == ({"111111"}, {"333333"})
    # catches using ticks() directly: 444444 ([x] in Recently done) would close, 222222 ([ ]) would reopen


def test_owner_actions_mixed_or_cross_section_lines_do_nothing():
    page = ("## alpha\n- [x] a (2026-11-01, id 111111)\n- [ ] a (2026-11-01, id 222222)\n"
            "## Recently done\n- [x] a (alpha, 2026-11-01, id 111111)\n"
            "- [ ] a (alpha, 2026-11-01, id 222222)\n- [ ] b (alpha, 2026-11-01, id 333333)\r\n"
            "- [x] c (alpha, 2026-11-01, id 444444)\n- [ ] c (alpha, 2026-11-01, id 444444)  \n")
    assert owner_actions(page) == (set(), {"333333"})
    # catches an id counted from one section only, or first-match-wins on duplicate lines; 333333 checks CRLF tolerance


def test_owner_actions_ignore_detail_lookalikes():
    r = done("111111", 1, details=["- [ ] fake (2026-11-01, id 222222)"])
    assert owner_actions(render([r, rec("222222")], TODAY)) == (set(), set())
    # catches an unanchored line regex reading a detail as an untick


def test_owner_actions_section_after_recently_done():
    page = ("## Recently done\n- [ ] a (alpha, 2026-11-01, id 111111)\n"
            "## alpha\n- [x] b (2026-11-01, id 222222)\n")
    assert owner_actions(page) == ({"222222"}, {"111111"})
    # catches the Recently done flag sticking past the next heading
