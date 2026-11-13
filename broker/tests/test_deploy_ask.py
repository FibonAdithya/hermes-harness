import json
import os
import stat

import pytest

from hermes_broker.deploy_ask import STATE_FILE, DeployAskState, format_deploy_ask


def test_twenty_commits_show_fifteen_and_a_count():
    lines = [f"{i:07x} subject {i}" for i in range(20)]
    text = format_deploy_ask("b" * 40, "a" * 40, 0.0, lines, ancestor=True)
    shown = [l for l in text.splitlines() if l.startswith("  ") and "more" not in l]
    assert len(shown) == 15 and "  and 5 more" in text.splitlines() and "20 commits:" in text


@pytest.mark.parametrize("n, more", [(15, None), (16, "  and 1 more")])
def test_more_line_boundary(n, more):
    lines = [f"{i:07x} subject {i}" for i in range(n)]
    out = format_deploy_ask("b" * 40, "a" * 40, 0.0, lines, ancestor=True).splitlines()
    assert [l for l in out if "more" in l] == ([more] if more else [])


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
    commits = ["4774e23 Owner todo list (#55)"]
    master = "4774e2314f46" + "0" * 28
    null = format_deploy_ask(None, master, 1763034240.0, commits, ancestor=True)
    assert null == ("Box is behind master.\nbox:    unknown (no deploy stamp)\n"
                    "master: 4774e2314f46 (merged 11:44 UTC)\n"
                    "Read them, then reply: deploy 4774e2314f46\nValid 24 h. Ignore it to skip.")
    assert "commit" not in null.replace("Box is behind", "")
    box = "195a042c401f" + "0" * 28
    orphan = format_deploy_ask(box, master, 1763034240.0, commits, ancestor=False)
    assert orphan == ("Box is behind master.\nbox:    195a042c401f (not an ancestor of master)\n"
                      "master: 4774e2314f46 (merged 11:44 UTC)\n"
                      "Read them, then reply: deploy 4774e2314f46\nValid 24 h. Ignore it to skip.")
    assert "commit" not in orphan


def test_state_survives_a_new_instance(tmp_path):
    DeployAskState(tmp_path / STATE_FILE).record_asked("a" * 40)
    s = DeployAskState(tmp_path / STATE_FILE)
    assert s.asked_sha() == "a" * 40
    s.set_running("b" * 40, 5.0)
    assert DeployAskState(tmp_path / STATE_FILE).deploy_running() == {"sha": "b" * 40, "started_at": 5.0}
    s.clear_running()
    assert s.deploy_running() is None and s.asked_sha() == "a" * 40
    s.set_running("b" * 40, 6.0)
    s.clear_asked()
    assert s.asked_sha() is None and s.deploy_running() == {"sha": "b" * 40, "started_at": 6.0}


def test_state_file_shape_and_mode(tmp_path):
    path = tmp_path / STATE_FILE
    s = DeployAskState(path)
    s.record_asked("a" * 40)
    s.set_running("b" * 40, 5.0)
    assert json.loads(path.read_text()) == {
        "asked_sha": "a" * 40, "deploy_running": {"sha": "b" * 40, "started_at": 5.0}}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    s.clear_asked()
    s.clear_running()
    assert json.loads(path.read_text()) == {}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "null", ""])
def test_corrupt_state_reads_as_empty(tmp_path, content):
    (tmp_path / STATE_FILE).write_text(content)
    s = DeployAskState(tmp_path / STATE_FILE)
    assert s.asked_sha() is None and s.deploy_running() is None
    s.record_asked("a" * 40)
    assert s.asked_sha() == "a" * 40


def test_missing_file_reads_as_empty(tmp_path):
    s = DeployAskState(tmp_path / "sub" / STATE_FILE)
    assert s.asked_sha() is None and s.deploy_running() is None
    s.clear_running()
    s.record_asked("c" * 40)
    assert s.asked_sha() == "c" * 40
