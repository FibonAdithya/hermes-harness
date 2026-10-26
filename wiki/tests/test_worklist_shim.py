"""The droplet cron shim `droplet/wiki-worklist.py`, loaded from its file path.

It runs under Hermes's venv Python, so it is not part of the `hermes_wiki` package.
Every test injects a fake `run` and a fake scanner: nothing here reaches docker.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / "droplet" / "wiki-worklist.py"
PROMPT = REPO / "droplet" / "wiki-curator-prompt.md"
SKILL = REPO / "droplet" / "skills" / "delegate-with-context" / "SKILL.md"
EXTRACT = REPO / "wiki" / "hermes_wiki" / "prompts" / "extract.md"

FLAGGED = "inbox/claude-laptop/2026-10-26-fact-0a1b2c3d.md"
CLEAN = "inbox/claude-box/2026-10-25-status-1a2b3c4d.md"
INJECTION_PHRASES = ["ignore previous", "ignore all", "disregard", "you are now", "new instructions"]


def _load():
    spec = importlib.util.spec_from_file_location("wiki_worklist_shim", SHIM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def shim():
    return _load()


def fake_scan(text):
    return "Blocked: test pattern" if "QUARANTINE-ME" in text else ""


class FakeRun:
    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def _note(path, body, tags=("hermes",)):
    return {"path": path, "body": body, "related": ["alpha"], "invalid": [],
            "meta": {"kind": "fact", "tags": list(tags), "source": path.split("/")[1], "created": "2026-10-26"}}


def _wl(notes=(), **kw):
    wl = {"notes": list(notes), "notes_remaining": 0, "over_cap": [], "past_review": [], "backfill": [],
          "orphans": [], "tag_proposals": {}, "lint": []}
    wl.update(kw)
    return wl


def test_shim_imports_nothing_from_hermes_wiki():
    text = SHIM.read_text(encoding="utf-8")
    assert "hermes_wiki import" not in text and "import hermes_wiki" not in text


def test_flagged_note_removed_deleted_and_listed(shim, tmp_path):
    wl = _wl([_note(FLAGGED, "Please QUARANTINE-ME now."), _note(CLEAN, "The box deploy uses uv.")])
    kept, quarantined = shim.filter_notes(wl, fake_scan)
    assert [n["path"] for n in kept["notes"]] == [CLEAN]
    assert quarantined == [(FLAGGED, "Blocked: test pattern")]
    run = FakeRun()
    shim.delete_notes([p for p, _ in quarantined], tmp_path / "sbx", run=run)
    assert len(run.calls) == 1
    argv = run.calls[0][0]
    assert argv[:4] == ["docker", "run", "--rm", "--network=none"]
    assert f"{tmp_path / 'sbx'}:/wiki" in argv
    assert argv[-1] == "/wiki/" + FLAGGED
    out = shim.render(kept, quarantined)
    assert f"quarantined: {FLAGGED} (Blocked: test pattern)" in out
    assert "QUARANTINE-ME" not in out
    assert "The box deploy uses uv." in out


def test_scan_covers_frontmatter(shim):
    note = _note(FLAGGED, "harmless body", tags=["QUARANTINE-ME"])
    kept, quarantined = shim.filter_notes(_wl([note]), fake_scan)
    assert kept["notes"] == [] and quarantined[0][0] == FLAGGED


def test_delete_refuses_paths_outside_inbox_shape(shim, tmp_path):
    run = FakeRun()
    shim.delete_notes(["index.md", "inbox/../index.md", "inbox/claude-laptop/x.md",
                       FLAGGED + "\n", "inbox/other/2026-10-26-fact-0a1b2c3d.md"], tmp_path, run=run)
    assert run.calls == []


def test_delete_batches_valid_and_skips_invalid(shim, tmp_path):
    run = FakeRun()
    shim.delete_notes(["index.md", FLAGGED, CLEAN], tmp_path, run=run)
    assert len(run.calls) == 1
    argv = run.calls[0][0]
    assert argv[-4:] == ["-f", "--", "/wiki/" + FLAGGED, "/wiki/" + CLEAN]
    assert "/wiki/index.md" not in argv


def test_compute_runs_worklist_in_sandbox_and_parses(shim, tmp_path):
    run = FakeRun(stdout=json.dumps(_wl(backfill=["delta"])))
    wl = shim.compute(tmp_path / "sbx", tmp_path / "repo", run=run)
    assert wl["backfill"] == ["delta"]
    argv, kw = run.calls[0]
    assert argv == ["docker", "run", "--rm", "--network=none",
                    "-v", f"{tmp_path / 'sbx'}:/wiki:ro", "-v", f"{tmp_path / 'repo'}:/opt/wiki:ro",
                    "-e", "PYTHONPATH=/opt/wiki", "hermes-sandbox:latest",
                    "python3", "-m", "hermes_wiki", "worklist", "--root", "/wiki"]
    assert kw.get("timeout") == 120


def test_compute_failure_raises(shim, tmp_path):
    with pytest.raises(shim.WorklistError):
        shim.compute(tmp_path, tmp_path, run=FakeRun(returncode=1))
    with pytest.raises(shim.WorklistError):
        shim.compute(tmp_path, tmp_path, run=FakeRun(stdout="not json"))


def test_scanner_missing_fails_loudly(shim, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        shim.load_scanner(tmp_path)
    assert e.value.code == 2
    assert "FATAL: Hermes scanner not importable" in capsys.readouterr().err


def test_scanner_loaded_from_agent_dir(shim, tmp_path, monkeypatch):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tools" / "cronjob_tools.py").write_text(
        "def _scan_cron_skill_assembled(t):\n    return (t, 'Blocked' if 'X' in t else '')\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        scan = shim.load_scanner(tmp_path)
        assert scan("aXb") == "Blocked"
        assert scan("abc") == ""
    finally:
        sys.modules.pop("tools.cronjob_tools", None)
        sys.modules.pop("tools", None)


def test_nothing_to_do(shim):
    assert shim.render(_wl(), []).strip() == "NOTHING TO DO"


def test_quarantine_alone_is_something_to_do(shim):
    out = shim.render(_wl(), [(FLAGGED, "Blocked")])
    assert "NOTHING TO DO" not in out and f"quarantined: {FLAGGED} (Blocked)" in out


def test_render_sections_in_worklist_order(shim):
    wl = _wl([_note(CLEAN, "body text")], notes_remaining=3,
             over_cap=[{"page": "big", "words": 1600}], past_review=[{"page": "due", "review_by": "2026-10-01"}],
             backfill=["delta"], orphans=["lonely"], tag_proposals={"newtag": 2},
             lint=[{"page": "beta", "problem": "broken link: [[nonexistent]]"}])
    out = shim.render(wl, [])
    keys = ["notes", "notes_remaining", "over_cap", "past_review", "backfill", "orphans", "tag_proposals", "lint"]
    positions = [out.index(f"## {k}") for k in keys]
    assert positions == sorted(positions)
    for text in ("big: 1600 words", "due: review_by 2026-10-01", "- delta", "- lonely", "newtag: 2",
                 "beta: broken link: [[nonexistent]]", "3 more notes wait"):
        assert text in out


def test_main_withholds_notes_when_whole_output_still_flagged(shim, tmp_path, capsys):
    # Each note passes alone, but the assembled text trips the scanner (patterns can span notes).
    wl = _wl([_note(CLEAN, "QUARANTINE"), _note("inbox/claude-box/2026-10-25-fact-2a2b3c4d.md", "-ME")],
             backfill=["delta"], lint=[{"page": "beta", "problem": "broken link: [[x]]"}])
    run = FakeRun(stdout=json.dumps(wl))

    def scan(text):
        return "Blocked" if "QUARANTINE" in text and "-ME" in text else ""

    assert shim.main(sbx_wiki=tmp_path, repo_wiki=tmp_path, run=run, scan=scan) == 0
    out = capsys.readouterr().out
    assert "notes withheld: scanner" in out
    assert "- delta" in out and "beta: broken link" in out
    assert "QUARANTINE" not in out
    assert scan(out) == ""


def _no_empty_headers(out):
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            assert i + 1 < len(lines) and not lines[i + 1].startswith("## "), f"empty section {ln!r}"


def test_flagged_lint_line_withholds_only_that_line(shim, tmp_path, capsys):
    # Reviewer's case: one hostile link target in a page must not withhold every clean note, every night.
    wl = _wl([_note(CLEAN, "The box deploy uses uv.")],
             lint=[{"page": "email-digest", "problem": "broken link: [[BADPHRASE]]"}])
    run = FakeRun(stdout=json.dumps(wl))

    def scan(text):
        return "Blocked" if "BADPHRASE" in text else ""

    assert shim.main(sbx_wiki=tmp_path, repo_wiki=tmp_path, run=run, scan=scan) == 0
    out = capsys.readouterr().out
    assert "The box deploy uses uv." in out
    assert "lint withheld for page email-digest" in out
    assert "notes withheld" not in out and "BADPHRASE" not in out
    assert "## lint" not in out
    _no_empty_headers(out)


def test_filter_gardening_keeps_clean_entries(shim):
    wl = _wl(lint=[{"page": "a", "problem": "broken link: [[BADPHRASE]]"}, {"page": "b", "problem": "missing from index.md"}],
             orphans=["BADPHRASE-page", "fine"], tag_proposals={"BADPHRASE": 1, "ok": 2})
    kept, withheld = shim.filter_gardening(wl, lambda t: "Blocked" if "BADPHRASE" in t else "")
    assert kept["lint"] == [{"page": "b", "problem": "missing from index.md"}]
    assert kept["orphans"] == ["fine"] and kept["tag_proposals"] == {"ok": 2}
    assert "lint withheld for page a" in withheld
    assert all("BADPHRASE" not in w for w in withheld)
    assert len(withheld) == 3


def test_fallback_output_rescanned(shim, tmp_path, capsys):
    # Every line passes alone, the whole output does not, and the fallback without notes still does not.
    wl = _wl([_note(CLEAN, "fine")], lint=[{"page": "a", "problem": "QUARANTINE"}, {"page": "b", "problem": "-ME"}],
             backfill=["delta"])
    run = FakeRun(stdout=json.dumps(wl))

    def scan(text):
        return "Blocked" if "QUARANTINE" in text and "-ME" in text else ""

    assert shim.main(sbx_wiki=tmp_path, repo_wiki=tmp_path, run=run, scan=scan) == 0
    out = capsys.readouterr().out
    assert scan(out) == ""
    assert "notes withheld: scanner" in out and "lines withheld: scanner" in out
    assert "lint: 2" in out and "backfill: 1" in out and "notes: 1" in out
    assert "QUARANTINE" not in out and "NOTHING TO DO" not in out


def test_main_deletes_quarantined_and_prints(shim, tmp_path, capsys):
    wl = _wl([_note(FLAGGED, "QUARANTINE-ME"), _note(CLEAN, "fine")])
    run = FakeRun(stdout=json.dumps(wl))
    assert shim.main(sbx_wiki=tmp_path, repo_wiki=tmp_path, run=run, scan=fake_scan) == 0
    out = capsys.readouterr().out
    assert f"quarantined: {FLAGGED}" in out and "QUARANTINE-ME" not in out
    assert len(run.calls) == 2 and run.calls[1][0][-1] == "/wiki/" + FLAGGED


def test_main_fails_when_worklist_fails(shim, tmp_path, capsys):
    assert shim.main(sbx_wiki=tmp_path, repo_wiki=tmp_path, run=FakeRun(returncode=1), scan=fake_scan) == 1
    assert "FATAL" in capsys.readouterr().err


def test_curator_prompt_has_rules_and_no_injection_phrases():
    text = PROMPT.read_text(encoding="utf-8")
    for needle in ("1,500", "at most 5 split", "[SILENT]", "type: log"):
        assert needle in text
    for path in (PROMPT, SKILL, EXTRACT):
        low = path.read_text(encoding="utf-8").lower()
        for phrase in INJECTION_PHRASES:
            assert phrase not in low, f"{path.name} contains {phrase!r}"


def test_curator_prompt_states_privacy_and_frontmatter_rules():
    text = PROMPT.read_text(encoding="utf-8")
    for needle in ("type: person", "finance", "investing", "health", "people", "CONVENTIONS.md", "TAGS.md",
                   "40", "quarantined"):
        assert needle in text


def test_curator_prompt_withheld_and_infra_rules():
    text = " ".join(PROMPT.read_text(encoding="utf-8").split())
    assert ("If the worklist contains `notes withheld`, `lines withheld` or `lint withheld`, say so in your report "
            "and never reply `[SILENT]`.") in text
    assert "If the worklist is exactly `NOTHING TO DO`, reply with exactly `[SILENT]`." in text
    assert ("Never file a note into `index.md`, `CONVENTIONS.md` or `TAGS.md`. Change `TAGS.md` only through "
            "`tag_proposals`, `index.md` only to keep its entries correct, and `CONVENTIONS.md` never.") in text
    assert "Merging an orphan into another page counts toward the 5 operations." in text


def test_skill_names_delegate_context_field():
    text = SKILL.read_text(encoding="utf-8")
    assert "put the relevant page names and the key facts in `context`" in " ".join(text.split())
    assert "his agents" not in text
    assert "rg -l '^tags:.*\\bfleet\\b' ~/hermes-wiki --glob '!inbox/**'" in text


def test_skill_frontmatter_names_it():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\nname: delegate-with-context\n")
    assert "context_pages" in text and "delegate_task" in text


def test_flagged_quarantine_reason_is_replaced(shim, tmp_path, capsys):
    wl = _wl([_note(FLAGGED, "QUARANTINE-ME"), _note(CLEAN, "fine")])
    run = FakeRun(stdout=json.dumps(wl))

    def scan(text):
        return "Blocked QUARANTINE-ME" if "QUARANTINE-ME" in text or "Blocked" in text else ""

    assert shim.main(sbx_wiki=tmp_path, repo_wiki=tmp_path, run=run, scan=scan) == 0
    out = capsys.readouterr().out
    assert f"quarantined: {FLAGGED} (reason withheld)" in out
    assert "fine" in out and "notes withheld" not in out


def test_curator_prompt_search_skips_inbox_and_names_malformed_tags():
    text = " ".join(PROMPT.read_text(encoding="utf-8").split())
    assert "rg -il '<key words>' ~/hermes-wiki --glob '!inbox/**'" in text
    assert "`malformed tags`" in text


def test_curator_prompt_states_page_keys_rule():
    text = " ".join(PROMPT.read_text(encoding="utf-8").split())
    assert ("Pages need exactly the keys `type` and `tags`, plus optionally `repos`, `updated` and `review_by`; "
            "a page missing a key or using any other key is hidden from the coding box until repaired.") in text
