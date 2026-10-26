import json
from pathlib import Path

from hermes_wiki import transcript as tr

FIXTURE = Path(__file__).parent / "fixtures" / "transcript.jsonl"
MARK = "[earlier turns omitted]\n"


def test_owner_messages_counts_only_owner_text():
    assert tr.owner_messages(FIXTURE) == 5


def test_condense_fixture_shapes():
    out = tr.condense(FIXTURE)
    assert "OWNER: How do I configure the widget service?" in out
    assert "ASSISTANT: Check the config file first." in out
    assert 'TOOL Bash: {"command": "cat widget.toml\\nsecond line"}' in out
    for dropped in ("SECRET-THOUGHT", "TOOL-OUTPUT", "system noise", "Meta caveat", "<command-name>"):
        assert dropped not in out


def test_condense_survives_bad_lines_and_caps(tmp_path):
    p = tmp_path / "t.jsonl"
    last = "é" * 9980 + "ÀÉÎÕÜ" * 4
    lines = [
        "{not json",
        json.dumps({"type": "user", "message": {"content": "hello"}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "TR-SECRET " * 200000}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": last}]}}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tr.condense(p, cap=5000)
    assert len(out) <= 5000 + len(MARK)
    assert out.startswith(MARK)
    assert out.endswith(last[-20:])
    assert "TR-SECRET" not in out


def test_condense_under_cap_has_no_marker():
    assert MARK not in tr.condense(FIXTURE)


def _write(p, objs):
    p.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")


def test_compact_summary_and_interrupt_not_owner(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"type": "user", "isCompactSummary": True, "message": {"content": "S" * 5000}},
        {"type": "user", "message": {"content": "[Request interrupted by user for tool use]"}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        {"type": "user", "message": {"content": "real owner words"}},
    ])
    assert tr.owner_messages(p) == 1
    out = tr.condense(p)
    assert "OWNER: real owner words" in out
    assert "OWNER: SSS" not in out
    assert "SUMMARY: " + "S" * 2000 in out and "S" * 2001 not in out
    assert "Request interrupted" not in out
