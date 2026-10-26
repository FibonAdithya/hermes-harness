import json
import subprocess
import sys
from datetime import date

from hermes_wiki import worklist
from wikifixture import build_wiki

TODAY = date(2026, 10, 26)


def _page(n, fm="type: topic\ntags: [ops]\nupdated: 2026-10-01\n", word="word"):
    head = f"---\n{fm}---\n" if fm else ""
    return head + " ".join([word] * n) + "\n"


def _note(created, kind="fact", tags="[ops]", body="Plain note text.", extra=""):
    return (f"---\nkind: {kind}\nsource: claude-laptop\ntags: {tags}\ncreated: {created}\n"
            f"verified: yes\n{extra}---\n{body}\n")


NOTE_A = "claude-laptop/2026-10-02-fact-0a1b2c3d.md"
NOTE_B = "claude-laptop/2026-10-01-fact-1a2b3c4d.md"


def make(tmp_path, **kw):
    extra = {
        "roundup": _page(1600, "type: log\ntags: [ops]\nupdated: 2026-10-01\n"),
        "nofm": _page(2000, fm=""),
        "exact": _page(1500),
        "due": _page(3, "type: topic\ntags: [ops]\nupdated: 2026-10-01\nreview_by: 2026-10-25\n"),
        "today": _page(3, "type: topic\ntags: [ops]\nupdated: 2026-10-01\nreview_by: 2026-10-26\n"),
        "beta": ("---\ntype: decision\ntags: [cost]\nupdated: 2026-10-20\n---\n"
                 "Beta records a single deploy choice. See [[nonexistent]].\n"),
    }
    unindexed = {"big": _page(1501)}
    inbox = {
        NOTE_A: _note("2026-10-02", body="The alpha deploy pipeline needs the droplet moved.",
                      extra="suggest_tags: [newtag, other]\n"),
        NOTE_B: _note("2026-10-01", tags="[bogus]", extra="suggest_tags: [newtag]\n"),
    }
    return build_wiki(tmp_path / "wiki", extra_pages=extra, unindexed=unindexed, inbox=inbox, **kw)


def run(tmp_path, **kw):
    return worklist.build(make(tmp_path), TODAY, **kw)


def test_notes_oldest_first_and_limited(tmp_path):
    w = run(tmp_path, note_limit=1)
    assert [n["path"] for n in w["notes"]] == ["inbox/" + NOTE_B]
    assert w["notes_remaining"] == 2


def test_notes_sorted_by_created_then_path(tmp_path):
    w = run(tmp_path)
    assert [n["path"] for n in w["notes"]]== [
        "inbox/" + NOTE_B, "inbox/" + NOTE_A, "inbox/claude-laptop/x.md"]
    assert all(set(n) == {"path", "meta", "body", "related", "invalid"} for n in w["notes"])


def test_invalid_notes_listed_not_dropped(tmp_path):
    w = run(tmp_path)
    by = {n["path"]: n for n in w["notes"]}
    assert any("bogus" in e for e in by["inbox/" + NOTE_B]["invalid"])
    assert by["inbox/" + NOTE_A]["invalid"] == []
    assert by["inbox/claude-laptop/x.md"]["invalid"]


def test_note_without_frontmatter(tmp_path):
    root = make(tmp_path)
    (root / "inbox/claude-laptop/plain.md").write_text("just text\n")
    w = worklist.build(root, TODAY)
    n = next(n for n in w["notes"] if n["path"].endswith("plain.md"))
    assert n["meta"] == {} and n["invalid"] == ["no frontmatter"]


def test_over_cap_boundary_and_log_exempt(tmp_path):
    names = [o["page"] for o in run(tmp_path)["over_cap"]]
    assert "big" in names and "roundup" not in names and "exact" not in names
    assert {"page": "big", "words": 1501} in run(tmp_path / "again")["over_cap"]


def test_backfill_puts_over_cap_first(tmp_path):
    w = run(tmp_path)
    assert w["backfill"][0] == "nofm"
    assert "nofm" not in [o["page"] for o in w["over_cap"]]
    assert "delta" in w["backfill"]
    assert run(tmp_path / "l", backfill_limit=1)["backfill"] == ["nofm"]


def test_infra_never_listed(tmp_path):
    w = run(tmp_path)
    infra = {"index", "CONVENTIONS", "TAGS"}
    assert not infra & {o["page"] for o in w["over_cap"]}
    assert not infra & set(w["backfill"])
    assert not infra & set(w["orphans"])


def test_past_review(tmp_path):
    pr = run(tmp_path)["past_review"]
    assert {"page": "due", "review_by": "2026-10-25"} in pr
    assert "today" not in [p["page"] for p in pr]


def test_lint_finds_broken_link_and_missing_index_line(tmp_path):
    lint = run(tmp_path)["lint"]
    assert len(lint) == 2
    assert {x["page"] for x in lint} == {"beta", "big"}
    by = {x["page"]: x["problem"] for x in lint}
    assert "nonexistent" in by["beta"]
    assert "index" in by["big"]


def test_lint_other_problems(tmp_path):
    root = make(tmp_path)
    (root / "badtag.md").write_text("---\ntype: topic\ntags: [zzz]\n---\nx\n")
    (root / "leak.md").write_text("---\ntype: topic\ntags: [ops]\n---\npassword: hunter2\n")
    with open(root / "index.md", "a") as f:
        f.write("- [[ghost]] — gone\n- [[badtag]]\n- [[leak]]\n")
    problems = [(x["page"], x["problem"]) for x in worklist.build(root, TODAY)["lint"]]
    assert any(p == "badtag" and "zzz" in t for p, t in problems)
    assert any(p == "leak" and "password" in t for p, t in problems)
    assert any(p == "index" and "ghost" in t for p, t in problems)


def test_orphans(tmp_path):
    o = run(tmp_path)["orphans"]
    assert "delta" in o and "exact" in o  # exact is linked only from index
    assert "beta" not in o


def test_self_link_does_not_save_orphan(tmp_path):
    root = build_wiki(tmp_path / "w", extra_pages={"solo": _page(2) + "see [[solo]]\n"})
    assert "solo" in worklist.build(root, TODAY)["orphans"]


def test_notes_carry_related_pages(tmp_path):
    w = run(tmp_path)
    n = next(n for n in w["notes"] if n["path"] == "inbox/" + NOTE_A)
    assert "alpha" in n["related"]


def test_tag_proposals(tmp_path):
    assert run(tmp_path)["tag_proposals"] == {"newtag": 2, "other": 1}


def test_cli_worklist_json(tmp_path):
    root = make(tmp_path)
    r = subprocess.run([sys.executable, "-m", "hermes_wiki", "worklist", "--root", str(root),
                        "--today", "2026-10-26"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert set(json.loads(r.stdout)) == {"notes", "notes_remaining", "over_cap", "past_review",
                                         "backfill", "orphans", "tag_proposals", "lint"}
