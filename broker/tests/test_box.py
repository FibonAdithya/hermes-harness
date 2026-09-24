import json

import pytest

from hermes_broker import box


def test_call_sends_verb_and_json_on_stdin(monkeypatch):
    seen = {}

    def fake_run_ssh(target, argv, timeout=60, stdin_text=None):
        seen.update(target=target, argv=argv, stdin=stdin_text, timeout=timeout)
        return 0, json.dumps({"id": "fleet-20260924-2300-abcd"}), ""

    monkeypatch.setattr(box, "run_ssh", fake_run_ssh)
    out = box.call("tig-server", "run_fleet", {"repo": "fleet-fixture", "hours": 8})
    assert seen["argv"] == ["run_fleet"]
    assert json.loads(seen["stdin"]) == {"repo": "fleet-fixture", "hours": 8}
    assert out == {"id": "fleet-20260924-2300-abcd"}


def test_call_refuses_a_bad_verb_locally():
    with pytest.raises(ValueError):
        box.call("tig-server", "bash -c id", {})


def test_call_reports_non_json_reply(monkeypatch):
    monkeypatch.setattr(box, "run_ssh", lambda *a, **k: (1, "garbage", "boom"))
    out = box.call("tig-server", "status", {})
    assert "error" in out
    assert "boom" in out["error"] or "garbage" in out["error"]


def test_call_reports_unreachable(monkeypatch):
    def dead(*a, **k):
        raise box.Unreachable("tig-server: timed out after 60s")

    monkeypatch.setattr(box, "run_ssh", dead)
    out = box.call("tig-server", "status", {})
    assert out["error"].startswith("tig-server unreachable")


def test_prompt_never_reaches_argv(monkeypatch):
    seen = {}

    def fake(t, argv, timeout=60, stdin_text=None):
        seen.update(argv=argv, stdin=stdin_text)
        return 0, "{}", ""

    monkeypatch.setattr(box, "run_ssh", fake)
    box.call("tig-server", "run_task", {"repo": "o/r", "prompt": "rm -rf / ; echo pwned", "minutes": 5})
    assert seen["argv"] == ["run_task"]
    assert "pwned" not in " ".join(seen["argv"])
    assert "pwned" in seen["stdin"]
