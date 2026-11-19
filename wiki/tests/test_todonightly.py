import json
import subprocess
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from hermes_wiki import cli
from hermes_wiki.config import WikiConfig
from hermes_wiki.todonightly import nightly
from hermes_wiki.todopage import render
from hermes_wiki.todorecords import Record, dumps, loads
from hermes_wiki.todos import Conflict
from hermes_wiki.todostore import JSONL, PAGE, NotMigrated

TODAY = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 0, 45, tzinfo=timezone.utc)
GH = "gh-test"


def rec(id, status="open", title=None, details=(), done_when=(), suggestion=None, closed=None):
    return Record(id=id, title=title or f"Item {id}", details=list(details), project="hermes",
                  created="2026-11-10", source="claude-box", status=status,
                  done_when=list(done_when), suggestion=suggestion, closed=closed)


def pr(repo_n):
    return {"pr": repo_n, "state": "merged"}


def issue(repo_n):
    return {"issue": repo_n, "state": "closed"}


class Tree:
    """In-memory wiki tree: commit advances head; `conflicts` scripts Conflict on the next commits."""

    def __init__(self, records, page_day=TODAY, conflicts=0, on_commit=None):
        self.files = {JSONL: dumps(records), PAGE: render(records, page_day), "todos.md": None}
        self.version = 0
        self.conflicts = conflicts
        self.on_commit = on_commit
        self.heads = 0
        self.commits = []

    def head(self, paths=None):
        self.heads += 1
        return str(self.version), dict(self.files)

    def commit(self, base, files, message):
        if self.on_commit:
            self.on_commit()
        self.commits.append((base, dict(files), message))
        if self.conflicts:
            self.conflicts -= 1
            raise Conflict()
        assert base == str(self.version)
        self.files.update(files)
        self.version += 1

    def records(self):
        return {r.id: r for r in loads(self.files[JSONL])}


class Gh:
    """Fake subprocess.run for `gh api repos/...`: answers by endpoint, logs every call."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def __call__(self, argv, **kw):
        assert argv[:2] == [GH, "api"] and kw == {"capture_output": True, "text": True, "timeout": 30}
        self.calls.append(argv[2])
        a = self.answers.get(argv[2])
        if a is None:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\nmore\n")
        if isinstance(a, BaseException):
            raise a
        if isinstance(a, str):
            return subprocess.CompletedProcess(argv, 0, a, "")
        if isinstance(a, tuple):
            return subprocess.CompletedProcess(argv, a[0], "", a[1])
        return subprocess.CompletedProcess(argv, 0, json.dumps(a), "")


def merged(day="2026-11-18"):
    return {"state": "closed", "merged_at": f"{day}T10:00:00Z", "closed_at": f"{day}T10:00:00Z",
            "body": "secret body text"}


UNMERGED = {"state": "closed", "merged_at": None, "closed_at": "2026-11-17T09:00:00Z"}
OPEN_PR = {"state": "open", "merged_at": None, "closed_at": None}


def go(tmp_path, tree, gh=None, suggestions="", today=TODAY, now=None, logs=None):
    cfg = WikiConfig(root=tmp_path, source="claude-droplet", gh=GH, state_dir=tmp_path / "st")
    out = nightly(cfg, tree, suggestions_text=suggestions, evidence_path=tmp_path / "ev.json",
                  state_path=tmp_path / "state.json", today=today,
                  now=now or datetime.combine(today, NOW.timetz()),
                  run=gh if gh is not None else Gh(), sleep=lambda s: None,
                  log=(logs if logs is not None else []).append, new_id=lambda: "ffffff")
    return out


def evidence(tmp_path):
    return json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))


def sline(**kw):
    return json.dumps(kw)


def test_merged_pr_closes_item_with_reason(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")]), rec("bbbbbb", done_when=[pr("o/r#6")])])
    gh = Gh({"repos/o/r/pulls/5": merged(), "repos/o/r/pulls/6": UNMERGED})
    out = go(tmp_path, tree, gh)
    got = tree.records()
    assert got["aaaaaa"].status == "done"
    assert got["aaaaaa"].closed == {"at": "2026-11-19", "by": "check", "reason": "PR #5 merged 2026-11-18"}
    assert got["bbbbbb"].status != "done"
    assert out["closed"] == 1 and out["committed"] is True
    assert tree.commits[-1][2] == "todo: nightly"  # catches closing on state == closed alone


def test_closed_issue_closes_item(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[issue("o/r#9")]), rec("bbbbbb", done_when=[issue("o/r#10")])])
    gh = Gh({"repos/o/r/issues/9": {"state": "closed", "closed_at": "2026-11-15T01:00:00Z"},
             "repos/o/r/issues/10": {"state": "open", "closed_at": None}})
    go(tmp_path, tree, gh)
    got = tree.records()
    assert got["aaaaaa"].closed == {"at": "2026-11-19", "by": "check", "reason": "issue #9 closed 2026-11-15"}
    assert got["bbbbbb"].status == "open"  # catches issue checks closing open issues or using the PR endpoint


def test_closed_unmerged_pr_suggests_not_closes(tmp_path):
    kept = {"at": "2026-11-01", "reason": "r", "evidence": "pr:o/r#6 closed unmerged", "rejected": True}
    other = {"at": "2026-11-18", "reason": "curator", "evidence": "e"}
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#6")]),
                 rec("bbbbbb", done_when=[pr("o/r#6")], suggestion=kept),
                 rec("cccccc", "suggested", done_when=[pr("o/r#6")], suggestion=other)])
    out = go(tmp_path, tree, Gh({"repos/o/r/pulls/6": UNMERGED}))
    got = tree.records()
    assert got["aaaaaa"].status == "suggested"
    assert got["aaaaaa"].suggestion == {"at": "2026-11-19", "reason": "PR #6 was closed unmerged",
                                        "evidence": "pr:o/r#6 closed unmerged"}
    assert got["bbbbbb"].status == "open" and got["bbbbbb"].suggestion == kept
    assert got["cccccc"].suggestion == other
    assert out["suggested_by_checks"] == 1 and out["closed"] == 0  # catches a wrong close / overriding a rejection


def test_check_failure_leaves_item_and_counts(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")])])
    state = tmp_path / "state.json"
    d1, d2, d3 = TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=2)
    for day in (d1, d1, d2):
        out = go(tmp_path, tree, Gh(), today=day)
    assert out["fetch_failures"] == 1 and tree.records()["aaaaaa"].status == "open"
    assert json.loads(state.read_text())["failures"] == {"pr:o/r#5": ["2026-11-19", "2026-11-20"]}
    assert evidence(tmp_path)["check_failures"] == []
    go(tmp_path, tree, Gh(), today=d3)
    assert evidence(tmp_path)["check_failures"] == ["pr:o/r#5"]
    go(tmp_path, tree, Gh({"repos/o/r/pulls/5": OPEN_PR}), today=d3)
    assert json.loads(state.read_text())["failures"] == {}
    assert evidence(tmp_path)["check_failures"] == []  # catches never resetting / off-by-one at 3 / double counting


def test_check_failures_lists_only_done_when_checks(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")], details=["see https://github.com/o/r/pull/7"])])
    for n in range(3):
        go(tmp_path, tree, Gh(), today=TODAY + timedelta(days=n))
    ev = evidence(tmp_path)
    assert ev["check_failures"] == ["pr:o/r#5"]  # catches alarming on a context URL in the details
    assert [x["error"] for x in ev["items"][0]["refs"] if x["key"] == "pr:o/r#7"] == ["gh: Not Found (HTTP 404)"]


def test_owner_untick_of_check_closed_item_stays_open_through_nightlies(tmp_path):
    r = rec("aaaaaa", "done", done_when=[pr("o/r#5")],
            closed={"at": "2026-11-18", "by": "check", "reason": "PR #5 merged 2026-11-18"})
    tree = Tree([r])
    page = tree.files[PAGE].split("\n")
    hit = [n for n, ln in enumerate(page) if ln.endswith("id aaaaaa)")]
    assert len(hit) == 1 and page[hit[0]].startswith("- [x]")
    page[hit[0]] = "- [ ]" + page[hit[0]][5:]
    tree.files[PAGE] = "\n".join(page)
    gh = Gh({"repos/o/r/pulls/5": merged()})
    for n in range(2):
        go(tmp_path, tree, gh, today=TODAY + timedelta(days=n))
        got = tree.records()["aaaaaa"]
        assert got.status == "open" and got.done_when == []  # catches the nightly re-closing a reopened item


def test_corrupt_state_file_starts_empty(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    logs = []
    go(tmp_path, Tree([rec("aaaaaa", done_when=[pr("o/r#5")])]), Gh(), logs=logs)
    assert json.loads((tmp_path / "state.json").read_text())["failures"] == {"pr:o/r#5": ["2026-11-19"]}
    assert any("state" in m for m in logs)  # catches crashing the nightly job on a bad state file


def test_fetch_errors_never_carry_the_response(tmp_path):
    tree = Tree([rec("aaaaaa", details=["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2",
                                        "https://github.com/o/r/pull/3", "https://github.com/o/r/pull/4"])])
    gh = Gh({"repos/o/r/pulls/2": "not json body", "repos/o/r/pulls/3": subprocess.TimeoutExpired("gh", 30),
             "repos/o/r/pulls/4": FileNotFoundError("gh")})
    out = go(tmp_path, tree, gh)
    errors = {r["key"]: r["error"] for r in evidence(tmp_path)["items"][0]["refs"]}
    assert errors == {"pr:o/r#1": "gh: Not Found (HTTP 404)", "pr:o/r#2": "JSONDecodeError",
                      "pr:o/r#3": "TimeoutExpired", "pr:o/r#4": "FileNotFoundError"}
    assert out["fetch_failures"] == 4  # catches an exception escaping the fetch or the body leaking into evidence


def test_conflict_retry_does_not_refetch(tmp_path):
    state = tmp_path / "state.json"
    seen = []
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")], details=["see https://github.com/o/r/pull/7"])],
                conflicts=1, on_commit=lambda: seen.append(state.stat().st_ino if state.exists() else None))
    gh = Gh({"repos/o/r/pulls/5": merged()})
    out = go(tmp_path, tree, gh)
    assert sorted(gh.calls) == ["repos/o/r/pulls/5", "repos/o/r/pulls/7"]
    assert len(tree.commits) == 2 and out["committed"] is True and out["closed"] == 1
    assert seen[0] is not None and seen[0] == seen[1] == state.stat().st_ino  # catches fetching or writing state inside fn


def test_duplicate_checks_one_call(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")]), rec("bbbbbb", done_when=[pr("o/r#5")]),
                 rec("cccccc", details=["https://github.com/o/r/pull/5 again"])])
    gh = Gh({"repos/o/r/pulls/5": OPEN_PR})
    go(tmp_path, tree, gh)
    assert gh.calls == ["repos/o/r/pulls/5"]  # catches rate blow-up


def test_done_items_not_fetched(tmp_path):
    tree = Tree([rec("aaaaaa", "done", done_when=[pr("o/r#5")],
                     closed={"at": "2026-11-18", "by": "agent", "reason": "r"})])
    gh = Gh()
    go(tmp_path, tree, gh)
    assert gh.calls == []  # catches fetching checks of done items every night


def test_suggestion_accepted(tmp_path):
    tree = Tree([rec("aaaaaa")])
    out = go(tmp_path, tree, suggestions="\n" + sline(id="aaaaaa", reason="  looks merged ", evidence="pr #3") + "\n\n")
    r = tree.records()["aaaaaa"]
    assert r.status == "suggested"
    assert r.suggestion == {"at": "2026-11-19", "reason": "looks merged", "evidence": "pr #3"}
    assert out["suggestions_accepted"] == 1 and out["suggestions_dropped"] == 0  # catches ignoring the suggestions text


def test_suggestion_on_rejected_evidence_dropped(tmp_path):
    rej = {"at": "2026-11-01", "reason": "r", "evidence": "E", "rejected": True}
    exp = {"at": "2026-11-01", "reason": "r", "evidence": "E", "expired": True}
    tree = Tree([rec("aaaaaa", suggestion=rej), rec("bbbbbb", suggestion=exp), rec("cccccc", suggestion=rej)])
    lines = [sline(id="aaaaaa", reason="again", evidence=" E "), sline(id="bbbbbb", reason="again", evidence="E"),
             sline(id="cccccc", reason="new", evidence="F")]
    logs = []
    out = go(tmp_path, tree, suggestions="\n".join(lines), logs=logs)
    got = tree.records()
    assert got["aaaaaa"].status == "open" and got["aaaaaa"].suggestion == rej
    assert got["bbbbbb"].status == "open" and got["bbbbbb"].suggestion == exp
    assert got["cccccc"].status == "suggested" and got["cccccc"].suggestion["evidence"] == "F"
    assert out["suggestions_dropped"] == 2
    assert [m.split(":")[0] for m in logs if m.startswith("dropped")] == [
        "dropped suggestion line 1", "dropped suggestion line 2"]  # catches re-suggesting rejected evidence (Review Focus 5)


def test_suggestion_for_unknown_or_done_id_dropped(tmp_path):
    closed = {"at": "2026-11-18", "by": "agent", "reason": "r"}
    sug = {"at": "2026-11-18", "reason": "x", "evidence": "y"}
    tree = Tree([rec("aaaaaa", "done", closed=closed), rec("bbbbbb", "suggested", suggestion=sug)])
    lines = [sline(id="aaaaaa", reason="r", evidence="e"), sline(id="bbbbbb", reason="r", evidence="e2"),
             sline(id="zzzzzz-secretish", reason="r", evidence="e")]
    logs = []
    out = go(tmp_path, tree, suggestions="\n".join(lines), logs=logs)
    got = tree.records()
    assert got["aaaaaa"].status == "done" and got["aaaaaa"].closed == closed
    assert got["bbbbbb"].suggestion == sug and "zzzzzz" not in tree.records()
    assert out["suggestions_dropped"] == 3 and out["suggestions_accepted"] == 0
    assert not any("secretish" in m for m in logs)  # catches resurrecting done items


def test_suggestion_secret_dropped(tmp_path):
    token = "ghp_" + "a" * 36
    tree = Tree([rec("aaaaaa"), rec("bbbbbb")])
    lines = [sline(id="aaaaaa", reason=f"token {token}", evidence="e"),
             sline(id="bbbbbb", reason="r", evidence=f"see {token}")]
    logs = []
    out = go(tmp_path, tree, suggestions="\n".join(lines), logs=logs)
    assert all(r.status == "open" for r in tree.records().values())
    assert token not in tree.files[JSONL] and token not in tree.files[PAGE]
    assert out["suggestions_dropped"] == 2 and not any(token in m for m in logs)  # catches unscanned curator text reaching the page


@pytest.mark.parametrize("line,why", [
    ("not json", "invalid JSON"),
    ('["aaaaaa"]', "not an object"),
    ('{"id": "aaaaaa", "reason": "r"}', "evidence"),
    ('{"id": "aaaaaa", "reason": 5, "evidence": "e"}', "reason"),
    ('{"id": "aaaaaa", "reason": "", "evidence": "e"}', "reason"),
    ('{"id": "aaaaaa", "reason": "a\\nb", "evidence": "e"}', "reason"),
    ('{"id": "aaaaaa", "reason": "r", "evidence": "' + "x" * 301 + '"}', "evidence"),
])
def test_malformed_suggestion_lines_dropped(tmp_path, line, why):
    tree = Tree([rec("aaaaaa")])
    logs = []
    out = go(tmp_path, tree, suggestions=line, logs=logs)
    assert tree.records()["aaaaaa"].status == "open" and out["suggestions_dropped"] == 1
    drops = [m for m in logs if m.startswith("dropped suggestion line 1: ")]
    assert len(drops) == 1 and why in drops[0] and line not in drops[0]  # catches a bad line crashing or being half-applied


def test_suggestion_300_chars_accepted(tmp_path):
    tree = Tree([rec("aaaaaa")])
    out = go(tmp_path, tree, suggestions=sline(id="aaaaaa", reason="r" * 300, evidence="e"))
    assert out["suggestions_accepted"] == 1  # catches an off-by-one at the 300-character limit


def test_duplicate_suggestion_first_wins(tmp_path):
    tree = Tree([rec("aaaaaa")])
    lines = [sline(id="aaaaaa", reason="first", evidence="e1"), sline(id="aaaaaa", reason="second", evidence="e2")]
    out = go(tmp_path, tree, suggestions="\n".join(lines))
    assert tree.records()["aaaaaa"].suggestion["reason"] == "first"
    assert out["suggestions_accepted"] == 1 and out["suggestions_dropped"] == 1  # catches last-wins overwrite


def test_expiry_boundary(tmp_path):
    six = {"at": (TODAY - timedelta(days=6)).isoformat(), "reason": "r", "evidence": "e6"}
    seven = {"at": (TODAY - timedelta(days=7)).isoformat(), "reason": "r", "evidence": "e7"}
    tree = Tree([rec("aaaaaa", "suggested", suggestion=six), rec("bbbbbb", "suggested", suggestion=seven)])
    out = go(tmp_path, tree)
    got = tree.records()
    assert got["aaaaaa"].status == "suggested" and got["aaaaaa"].suggestion == six
    assert got["bbbbbb"].status == "open" and got["bbbbbb"].suggestion == dict(seven, expired=True)
    assert out["expired"] == 1  # catches `>` instead of `>=`


def test_quiet_night_commits_page_refresh(tmp_path):
    closed = {"at": "2026-11-12", "by": "agent", "reason": "r"}
    tree = Tree([rec("aaaaaa", "done", closed=closed)], page_day=TODAY - timedelta(days=1))
    assert "Recently done" in tree.files[PAGE]
    out = go(tmp_path, tree)
    assert out["committed"] is True and "Recently done" not in tree.files[PAGE]  # catches fn returning None on a quiet night


def test_evidence_written_even_without_commit(tmp_path):
    tree = Tree([rec("aaaaaa", details=["https://github.com/o/r/pull/5"])])
    gh = Gh({"repos/o/r/pulls/5": OPEN_PR})
    first = go(tmp_path, tree, gh, now=NOW)
    later = NOW + timedelta(hours=1)
    second = go(tmp_path, tree, gh, now=later)
    assert first["committed"] is False and second["committed"] is False and tree.commits == []
    ev = evidence(tmp_path)
    assert ev["generated_at"] == "2026-11-19T01:45:00+00:00"
    assert ev["items"] == [{
        "id": "aaaaaa", "title": "Item aaaaaa", "details": ["https://github.com/o/r/pull/5"],
        "project": "hermes", "created": "2026-11-10", "status": "open", "suggestion": None,
        "refs": [{"url": "https://github.com/o/r/pull/5", "key": "pr:o/r#5", "state": "open",
                  "merged_at": None, "closed_at": None}],
        "hash": ev["items"][0]["hash"]}]  # catches stale evidence on a night with no commit


def test_evidence_lists_open_and_suggested_sorted(tmp_path):
    sug = {"at": "2026-11-18", "reason": "x", "evidence": "y"}
    closed = {"at": "2026-11-18", "by": "agent", "reason": "r"}
    tree = Tree([rec("cccccc"), rec("aaaaaa", "suggested", suggestion=sug), rec("bbbbbb", "done", closed=closed),
                 rec("dddddd", done_when=[pr("o/r#5")])])
    go(tmp_path, tree, Gh({"repos/o/r/pulls/5": merged()}))
    items = evidence(tmp_path)["items"]
    assert [i["id"] for i in items] == ["aaaaaa", "cccccc"]
    assert items[0]["suggestion"] == sug  # catches evidence built from the pre-transaction records


def test_evidence_hash_changes_when_ref_state_changes(tmp_path):
    tree = Tree([rec("aaaaaa", details=["https://github.com/o/r/issues/5"])])
    go(tmp_path, tree, Gh({"repos/o/r/issues/5": {"state": "open", "closed_at": None}}))
    h1 = evidence(tmp_path)["items"][0]["hash"]
    go(tmp_path, tree, Gh({"repos/o/r/issues/5": {"state": "open", "closed_at": None}}),
       now=NOW + timedelta(hours=2))
    assert evidence(tmp_path)["items"][0]["hash"] == h1
    go(tmp_path, tree, Gh({"repos/o/r/issues/5": {"state": "closed", "closed_at": "2026-11-19T00:00:00Z"}}))
    ev = evidence(tmp_path)["items"][0]
    assert ev["refs"][0]["state"] == "closed" and ev["hash"] != h1  # catches stale evidence / hash ignoring refs


def test_not_migrated(tmp_path):
    tree = Tree([])
    tree.files[JSONL] = None
    gh = Gh()
    with pytest.raises(NotMigrated):
        go(tmp_path, tree, gh)
    assert gh.calls == [] and not (tmp_path / "ev.json").exists()  # catches running on an unmigrated wiki


def test_rejected_evidence_survives_a_later_cycle_curator(tmp_path):
    rej = {"at": "2026-11-01", "reason": "r", "evidence": "E", "rejected": True}
    tree = Tree([rec("aaaaaa", suggestion=rej)])
    go(tmp_path, tree, suggestions=sline(id="aaaaaa", reason="r", evidence="F"))
    s = tree.records()["aaaaaa"].suggestion
    assert s["evidence"] == "F" and s["blocked_evidence"] == ["E"]
    assert evidence(tmp_path)["items"][0]["suggestion"]["blocked_evidence"] == ["E"]
    go(tmp_path, tree, today=TODAY + timedelta(days=7))
    r = tree.records()["aaaaaa"]
    assert r.status == "open" and r.suggestion["expired"] is True
    out = go(tmp_path, tree, suggestions=sline(id="aaaaaa", reason="again", evidence="E"),
             today=TODAY + timedelta(days=8))
    assert tree.records()["aaaaaa"].status == "open" and out["suggestions_dropped"] == 1
    go(tmp_path, tree, suggestions=sline(id="aaaaaa", reason="new", evidence="G"), today=TODAY + timedelta(days=8))
    assert tree.records()["aaaaaa"].suggestion["blocked_evidence"] == ["E", "F"]  # catches rejection memory lost after one cycle


def test_rejected_unmerged_evidence_survives_a_later_cycle_check(tmp_path):
    ev = "pr:o/r#6 closed unmerged"
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#6")],
                     suggestion={"at": "2026-11-01", "reason": "r", "evidence": ev, "rejected": True})])
    answers = {"repos/o/r/pulls/6": UNMERGED}
    go(tmp_path, tree, Gh(answers), suggestions=sline(id="aaaaaa", reason="r", evidence="F"))
    assert tree.records()["aaaaaa"].suggestion["blocked_evidence"] == [ev]
    go(tmp_path, tree, Gh(answers), today=TODAY + timedelta(days=7))
    assert tree.records()["aaaaaa"].status == "open"
    out = go(tmp_path, tree, Gh(answers), today=TODAY + timedelta(days=8))
    r = tree.records()["aaaaaa"]
    assert r.status == "open" and r.suggestion["evidence"] == "F"
    assert out["suggested_by_checks"] == 0  # catches the check re-suggesting a rejected unmerged PR one cycle later


def test_check_suggestion_blocked_by_expired_evidence(tmp_path):
    exp = {"at": "2026-11-01", "reason": "r", "evidence": "pr:o/r#6 closed unmerged", "expired": True}
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#6")], suggestion=exp)])
    out = go(tmp_path, tree, Gh({"repos/o/r/pulls/6": UNMERGED}))
    assert tree.records()["aaaaaa"].status == "open" and out["suggested_by_checks"] == 0  # catches blocking only rejected, not expired


def test_passing_check_closes_suggested_item(tmp_path):
    sug = {"at": "2026-11-18", "reason": "x", "evidence": "y"}
    tree = Tree([rec("aaaaaa", "suggested", done_when=[pr("o/r#5")], suggestion=sug)])
    out = go(tmp_path, tree, Gh({"repos/o/r/pulls/5": merged()}))
    r = tree.records()["aaaaaa"]
    assert r.status == "done" and r.closed["by"] == "check" and out["closed"] == 1  # catches checks applied to open items only


def test_unparseable_dates_omitted_from_reason(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")]), rec("bbbbbb", done_when=[issue("o/r#9")])])
    gh = Gh({"repos/o/r/pulls/5": {"state": "closed", "merged_at": "garbage", "closed_at": None},
             "repos/o/r/issues/9": {"state": "closed", "closed_at": None}})
    go(tmp_path, tree, gh)
    got = tree.records()
    assert got["aaaaaa"].closed["reason"] == "PR #5 merged"
    assert got["bbbbbb"].closed["reason"] == "issue #9 closed"  # catches a garbage date slice in the reason


def test_fetch_error_sanitised(tmp_path):
    token = "ghp_" + "a" * 36
    tree = Tree([rec("aaaaaa", details=[f"https://github.com/o/r/pull/{n}" for n in (1, 2, 3, 4)])])
    gh = Gh({"repos/o/r/pulls/1": (1, "\x1b[31mgh: boom\x1b[0m\nsecond"), "repos/o/r/pulls/2": (1, "x" * 500),
             "repos/o/r/pulls/3": (4, f"bad {token}"), "repos/o/r/pulls/4": (5, "")})
    go(tmp_path, tree, gh)
    errors = {r["key"]: r["error"] for r in evidence(tmp_path)["items"][0]["refs"]}
    assert errors == {"pr:o/r#1": "[31mgh: boom[0m", "pr:o/r#2": "x" * 300,
                      "pr:o/r#3": "gh failed (rc 4)", "pr:o/r#4": "gh failed (rc 5)"}
    assert token not in (tmp_path / "ev.json").read_text()  # catches raw stderr (secrets, escapes, length) reaching the evidence file


def test_failure_dates_trimmed_to_last_three(tmp_path):
    tree = Tree([rec("aaaaaa", done_when=[pr("o/r#5")])])
    for n in range(4):
        go(tmp_path, tree, Gh(), today=TODAY + timedelta(days=n))
    assert json.loads((tmp_path / "state.json").read_text())["failures"] == {
        "pr:o/r#5": ["2026-11-20", "2026-11-21", "2026-11-22"]}
    assert evidence(tmp_path)["check_failures"] == ["pr:o/r#5"]  # catches unbounded date lists


@pytest.mark.parametrize("field,value,same", [
    ("title", "Other title", False),
    ("details", ["https://github.com/o/r/pull/5", "extra"], False),
    ("project", "other", False),
    ("status", "suggested", False),
    ("created", "2026-11-01", True),
])
def test_evidence_hash_covers_fields(tmp_path, field, value, same):
    base = rec("aaaaaa", details=["https://github.com/o/r/pull/5"])
    changed = replace(base, **{field: value})
    if field == "status":
        changed.suggestion = {"at": TODAY.isoformat(), "reason": "r", "evidence": "e"}
    hashes = []
    for name, r in (("a", base), ("b", changed)):
        (tmp_path / name).mkdir()
        go(tmp_path / name, Tree([r]), Gh({"repos/o/r/pulls/5": OPEN_PR}))
        hashes.append(evidence(tmp_path / name)["items"][0]["hash"])
    assert (hashes[0] == hashes[1]) is same  # catches a hashed field left out, or created hashed


# --- CLI


def _conf(tmp_path, **extra):
    gh = tmp_path / "gh-stub"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "gh.log"}\nexit 97\n', encoding="utf-8")
    gh.chmod(0o755)
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"root": str(tmp_path), "source": "claude-droplet",
                                "state_dir": str(tmp_path / "state"), "gh": str(gh), **extra}), encoding="utf-8")
    return str(conf)


def _argv(tmp_path, conf):
    return ["todo", "nightly", "--suggestions", str(tmp_path / "missing.jsonl"),
            "--evidence", str(tmp_path / "ev.json"), "--state", str(tmp_path / "fail.json"), "--config", conf]


def test_cli_nightly_dry_prints_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WIKI_DRY_PUBLISH", "1")
    d = tmp_path / "state" / "dry"
    d.mkdir(parents=True)
    records = [rec("aaaaaa", done_when=[pr("o/r#5")])]
    (d / JSONL).write_text(dumps(records), encoding="utf-8")
    (d / PAGE).write_text(render(records, TODAY), encoding="utf-8")
    assert cli.main(_argv(tmp_path, _conf(tmp_path))) == 0
    cap = capsys.readouterr()
    summary = json.loads(cap.out)
    assert summary["fetch_failures"] == 1 and set(summary) == {
        "closed", "suggested_by_checks", "suggestions_accepted", "suggestions_dropped", "expired",
        "fetch_failures", "committed"}
    assert (tmp_path / "gh.log").read_text().split() == ["api", "repos/o/r/pulls/5"]
    assert json.loads((tmp_path / "ev.json").read_text())["items"][0]["id"] == "aaaaaa"  # catches the CLI not wiring paths through


def test_cli_nightly_public_only_refused(tmp_path, capsys):
    assert cli.main(_argv(tmp_path, _conf(tmp_path, public_only=True))) == 2
    assert not (tmp_path / "gh.log").exists() and not (tmp_path / "ev.json").exists()  # catches nightly running on a public box
