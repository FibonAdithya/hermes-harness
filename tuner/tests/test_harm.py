from __future__ import annotations

import pytest

from hermes_tuner import history, policy
from hermes_tuner.harm import (
    CHECKS, SIGNALS, Baseline, RunEvidence, classify, error_rate, names_tool, signals)

GET = "mcp__google__get_gmail_message_content"


def WRAP(name: str, body: str) -> str:
    return (f'<untrusted_tool_result source="{name}">\n{history._WRAP_NOTE}\n\n{body}\n'
            f'{history._WRAP_TAIL}')


LOOP = ("\n\n[Tool loop warning: same_tool_failure_warning; count=3; "
        "mcp__google__get_gmail_message_content has failed 3 times this turn.]")
UNKNOWN = ('{"error": "\'mcp__google__tuned_x\' is not a deferrable tool. If you see it in the '
           'tools list already, call it directly; otherwise check the spelling against tool_search."}')
ARG_ERR = WRAP(GET, '{"error": "Error calling tool \'get_gmail_message_content\': '
                    'expected_sha256 and page_size require page_offset."}')
DOWN = WRAP(GET, '{"error": "MCP server \'google\' is unreachable after 3 consecutive failures"}')
API_ERR = WRAP(GET, '{"error": "Error calling tool \'get_events\': API error in get_events: '
                    '<HttpError 503>"}')
OK = WRAP(GET, "Subject: hi")
FIX = "Fetch mail with tuned_x."


def test_wrap_helper_is_unwrapped_by_history():
    """The WRAP helper matches the real wrapper.
    Catches: a helper that history._unwrap does not strip, making every other test vacuous."""
    out = history._unwrap(WRAP(GET, "body"), frozenset({GET})).strip()
    assert out == history._unwrap("body", frozenset()).strip() == "body"


def test_classify_unknown_tool():
    """Catches: a missing unknown-tool pattern (tool_describe, registry or plugin form),
    or the subject not being the unknown name."""
    assert classify("tool_describe", UNKNOWN) == ("unknown_tool", "mcp__google__tuned_x")
    assert classify("tuned_x", '{"error": "Unknown tool: tuned_x"}') == ("unknown_tool", "tuned_x")
    assert classify("tuned_x", '{"error": "tuned_x is no longer available"}') == ("unknown_tool", "tuned_x")


def test_classify_unwraps_mcp_errors():
    """Catches: matching on the raw text without stripping the wrapper, or API
    failures counted as the run's own errors."""
    assert classify(GET, ARG_ERR)[0] == "tool_error"
    assert classify(GET, DOWN) == ("server_unreachable", "google")   # checked before tool_error
    assert classify(GET, API_ERR)[0] == "upstream_error"
    assert classify("mcp__google__search_gmail_messages", WRAP("mcp__google__search_gmail_messages",
        '{"error": "1 validation error for call[search_gmail_messages]\\nmax_results\\n  Unexpected keyword argument"}'))[0] == "tool_error"
    assert classify(GET, '{"error": "tool_call to \'x\' is missing required argument y"}')[0] == "tool_error"


def test_classify_strips_hermes_loop_suffix():
    """The real incident shape. Catches: whole-text json.loads (misses 3 of the 7
    error results in the 11-04 run), or stripping any trailing text."""
    assert classify(GET, ARG_ERR + LOOP)[0] == "tool_error"
    assert classify(GET, DOWN + LOOP)[0] == "server_unreachable"
    assert classify(GET, ARG_ERR + "\n\nmore text") is None
    assert classify(GET, ARG_ERR + LOOP + LOOP) is None


def test_classify_ignores_error_text_inside_content():
    """Review Focus 1. Catches: substring matching anywhere in the result."""
    assert classify(GET, WRAP(GET, 'Subject: hi\n{"error": "Unknown tool: x"}')) is None
    assert classify("tuned_x", '{"result": {"error": "Unknown tool: x"}}') is None
    assert classify(GET, OK) is None
    assert classify(GET, WRAP(GET, '{"error": 5}')) is None


def test_error_rate_counts_only_tool_errors():
    """Catches: upstream_error counted, or a division by zero on no results."""
    assert error_rate(()) == 0.0
    assert error_rate([(GET, ARG_ERR), (GET, API_ERR), (GET, OK), (GET, OK)]) == 0.25


def test_signals_on_the_incident_a_run():
    """The 11-04 briefing run as stored: 2 unknown-tool lookups, 3 argument errors
    (the third with the loop suffix), 2 unreachable (both with it), 35 ok.
    Catches: a signal missing from CHECKS, order not SIGNALS, or the suffix not stripped."""
    res = (("tool_describe", UNKNOWN),) * 2 + ((GET, ARG_ERR),) * 2 + ((GET, ARG_ERR + LOOP),) \
        + ((GET, DOWN + LOOP),) * 2 + ((GET, OK),) * 35
    assert signals(RunEvidence(res, fix_text=FIX), Baseline((0.0,) * 17)) == [
        "unknown_tool", "server_tripped", "self_inflicted_errors"]


def test_registry_keys_match_signals():
    """Catches: a signal without a check, or an extra check, or a changed order."""
    assert SIGNALS == ("unknown_tool", "server_tripped", "self_inflicted_errors", "run_failed")
    assert tuple(CHECKS) == SIGNALS


def test_unknown_tool_needs_the_fix_to_name_it():
    """10-30..11-03: a stale rolled-back tool called by every run, named by no fix.
    Catches: unknown_tool firing on any unknown name."""
    stale = (("tuned_old", '{"error": "tuned_old is no longer available"}'),)
    assert "unknown_tool" not in signals(RunEvidence(stale, fix_text=FIX), Baseline())
    assert "unknown_tool" in signals(RunEvidence(stale, fix_text="Use tuned_old."), Baseline())
    assert not names_tool("Use tuned_older.", "tuned_old")


def test_names_tool_matches_last_segment():
    """Catches: requiring the full mcp__ name, or matching inside a longer word."""
    assert names_tool("call tuned_x now", "mcp__google__tuned_x")
    assert names_tool("call mcp__google__tuned_x.", "mcp__google__tuned_x")
    assert not names_tool("call xtuned_x", "mcp__google__tuned_x")


def test_unknown_tool_excuses_registered_core_tools():
    """Hermes says "not a deferrable tool" for core tools too. Catches: ignoring
    RunEvidence.tools."""
    core = (("tool_describe", '{"error": "\'terminal\' is not a deferrable tool. If you see it in the tools list already, call it directly."}'),)
    assert "unknown_tool" not in signals(RunEvidence(core, fix_text="Run terminal.", tools=frozenset({"terminal"})), Baseline())
    assert "unknown_tool" in signals(RunEvidence(core, fix_text="Run terminal."), Baseline())


def test_upstream_errors_are_not_harm():
    """A Google API failure followed by the server being cut off is an outage.
    Catches: upstream_error counted in self_inflicted_errors or server_tripped."""
    res = ((GET, API_ERR),) * 5 + ((GET, DOWN),) + ((GET, OK),) * 5
    assert signals(RunEvidence(res), Baseline()) == []


def test_server_tripped_needs_the_run_s_own_errors_first():
    """Catches: firing on a plain outage, or on errors from another server."""
    other = "mcp__compute__run_task"
    assert "server_tripped" not in signals(RunEvidence(((GET, DOWN), (GET, OK))), Baseline())
    assert "server_tripped" not in signals(
        RunEvidence(((other, WRAP(other, '{"error": "Error calling tool \'run_task\': x"}')), (GET, DOWN))), Baseline())
    assert "server_tripped" in signals(RunEvidence(((GET, ARG_ERR), (GET, DOWN))), Baseline())
    assert "server_tripped" not in signals(RunEvidence(((GET, DOWN), (GET, ARG_ERR))), Baseline())


@pytest.mark.parametrize("errors, total, base_mean, fired", [
    (2, 38, 0.0, False),   # the 11-02 run: under SELF_ERR_MIN
    (3, 40, 0.0, True),
    (3, 60, 0.0, False),   # rate 0.05 == threshold: strictly above only
    (3, 10, 0.2, False),   # 0.30 <= 2*0.2 + 0.05
    (5, 10, 0.2, True)])
def test_self_inflicted_errors_threshold(errors, total, base_mean, fired):
    """Catches: SELF_ERR_MIN/FACTOR/MARGIN off by one, or >= instead of >."""
    res = ((GET, ARG_ERR),) * errors + ((GET, OK),) * (total - errors)
    assert ("self_inflicted_errors" in signals(RunEvidence(res), Baseline((base_mean,) * 4))) is fired


def test_self_inflicted_errors_without_baseline_uses_zero():
    """Catches: an empty baseline raising or being treated as always-fire/never-fire."""
    res = ((GET, ARG_ERR),) * 3 + ((GET, OK),) * 37
    assert "self_inflicted_errors" in signals(RunEvidence(res), Baseline())


def test_run_failed_unless_outage():
    """Catches: run_failed ignoring the outage flag, or firing on unknown status."""
    assert "run_failed" in signals(RunEvidence((), status="error"), Baseline())
    assert "run_failed" in signals(RunEvidence((), status="ok", delivery_failed=True), Baseline())
    assert "run_failed" not in signals(RunEvidence((), status="error"), Baseline(outage=True))
    assert "run_failed" not in signals(RunEvidence((), status=None), Baseline())
    assert "run_failed" not in signals(RunEvidence((), status="ok"), Baseline())


def test_kill_switch_silences_all_signals(monkeypatch):
    """Catches: policy.HARM_CHECKS imported by value at import time, or not consulted."""
    run = RunEvidence((), status="error")
    assert signals(run, Baseline()) == ["run_failed"]
    monkeypatch.setattr(policy, "HARM_CHECKS", False)
    assert signals(run, Baseline()) == []


def test_deeply_nested_result_is_unclassifiable():
    """Tool results are untrusted text; json.loads raises RecursionError on deep nesting.
    Catches: catching only ValueError around json.loads, letting RecursionError abort a cycle."""
    deep = "[" * 200_000
    assert classify(GET, deep) is None
    assert error_rate([(GET, deep)]) == 0.0
    assert signals(RunEvidence(((GET, deep),)), Baseline()) == []
