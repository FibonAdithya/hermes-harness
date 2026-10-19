import hashlib
import json
from datetime import datetime, timezone

import pytest

from hermes_tuner import draft as D
from factory import FLAT, FLAT_RATE
from hermes_tuner import policy
from hermes_tuner.bridge import BridgeError, FakeBridge
from hermes_tuner.detect import Candidate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp()
CAND = Candidate(kind="sequence", key="seq:a>b>c", job_id="3bc4e11d29b1", sessions=12,
                 occurrences=30, result_chars=90_000, est_monthly_usd=1.5)
JOB_PAYLOAD = {"prompt": "Do the thing.", "add_toolsets": []}


def reply(kind="job_prompt", target="3bc4e11d29b1", payload=None):
    return json.dumps({"kind": kind, "target": target,
                       "payload": JOB_PAYLOAD if payload is None else payload})


def test_fix_id_stable_for_same_draft():
    a = D.parse_draft(reply(), CAND, NOW)
    b = D.parse_draft(reply(), CAND, NOW + 5)
    assert a.id == b.id
    h = hashlib.sha256((CAND.key + json.dumps(JOB_PAYLOAD, sort_keys=True)).encode()).hexdigest()[:8]
    assert a.id == f"20261019-{h}"
    assert a.candidate_key == CAND.key
    other = D.parse_draft(reply(payload={"prompt": "Other.", "add_toolsets": []}), CAND, NOW)
    assert other.id != a.id


def test_fix_id_date_is_utc():
    late = datetime(2026, 10, 19, 23, 30, tzinfo=timezone.utc).timestamp()
    assert D.parse_draft(reply(), CAND, late).id.startswith("20261019-")


def test_parse_accepts_valid_job_prompt_fix():
    fix = D.parse_draft(reply(), CAND, NOW)
    assert fix.kind == "job_prompt" and fix.target == "3bc4e11d29b1"
    assert fix.payload == JOB_PAYLOAD


def test_parse_rejects_unknown_kind():
    assert D.parse_draft(reply(kind="shell"), CAND, NOW) is None


@pytest.mark.parametrize("text", [
    "not json", "", "   ",
    "Here you go: " + reply(),
    reply() + " Hope that helps",
    reply() + reply(),
    "```json\n" + reply() + "\n```",
    "[" + reply() + "]",
])
def test_parse_rejects_non_json_and_trailing_prose(text):
    assert D.parse_draft(text, CAND, NOW) is None


def test_parse_allows_surrounding_whitespace():
    assert D.parse_draft("\n  " + reply() + " \n", CAND, NOW) is not None


def test_parse_drops_unknown_payload_keys():
    fix = D.parse_draft(reply(payload={**JOB_PAYLOAD, "extra": "x"}), CAND, NOW)
    assert fix.payload == JOB_PAYLOAD


def test_unknown_key_does_not_change_id():
    a = D.parse_draft(reply(), CAND, NOW)
    b = D.parse_draft(reply(payload={**JOB_PAYLOAD, "extra": "x"}), CAND, NOW)
    assert a.id == b.id


COMPOSED = {"name": "tuned_x", "description": "d", "parameters": {"type": "object"},
            "calls": ["read_file"], "mutating": False, "program": "def main(a, call): return 1",
            "job_prompt": None}
CASES = [
    ("composed_tool", COMPOSED),
    ("composed_tool", {**COMPOSED, "job_prompt": "use it"}),
    ("job_prompt", JOB_PAYLOAD),
    ("config", {"key": "compression.threshold", "value": 0.5}),
    ("config", {"key": "compression.enabled", "value": True}),
    ("config", {"key": "compression.mode", "value": "x"}),
    ("skill", {"path": "a/SKILL.md", "text": "t"}),
    ("new_reach", {"change": {"type": "callable_add", "tool": "t"}, "why": "w"}),
    ("new_reach", {"change": {"type": "mcp_include_add", "server": "s", "tool": "t"}, "why": "w"}),
]


@pytest.mark.parametrize("kind,payload", CASES)
def test_each_kind_parses(kind, payload):
    fix = D.parse_draft(reply(kind=kind, payload=payload), CAND, NOW)
    assert fix is not None and fix.kind == kind and fix.payload == payload


BAD = [
    ("composed_tool", {**COMPOSED, "name": 3}),
    ("composed_tool", {**COMPOSED, "parameters": []}),
    ("composed_tool", {**COMPOSED, "calls": "read_file"}),
    ("composed_tool", {**COMPOSED, "calls": [1]}),
    ("composed_tool", {**COMPOSED, "mutating": "yes"}),
    ("composed_tool", {**COMPOSED, "job_prompt": 5}),
    ("composed_tool", {k: v for k, v in COMPOSED.items() if k != "program"}),
    ("job_prompt", {"prompt": "p"}),
    ("job_prompt", {"prompt": "p", "add_toolsets": "tuner"}),
    ("job_prompt", {"prompt": "p", "add_toolsets": [1]}),
    ("job_prompt", {"prompt": 1, "add_toolsets": []}),
    ("config", {"key": "compression.x", "value": None}),
    ("config", {"key": "compression.x", "value": [1]}),
    ("config", {"key": "compression.x"}),
    ("config", {"key": 1, "value": 1}),
    ("skill", {"path": "a/SKILL.md"}),
    ("skill", {"path": 1, "text": "t"}),
    ("new_reach", {"change": {"type": "other"}, "why": "w"}),
    ("new_reach", {"change": "x", "why": "w"}),
    ("new_reach", {"change": {"type": "callable_add", "tool": "t"}}),
    ("new_reach", {"change": {"type": "callable_add", "tool": "t"}, "why": 1}),
    ("new_reach", {"change": {"type": "callable_add"}, "why": "w"}),
    ("new_reach", {"change": {"type": "mcp_include_add", "server": "s"}, "why": "w"}),
]


@pytest.mark.parametrize("kind,payload", BAD)
def test_bad_payload_types_rejected(kind, payload):
    assert D.parse_draft(reply(kind=kind, payload=payload), CAND, NOW) is None


def test_missing_or_mistyped_top_level_keys_rejected():
    ok = json.loads(reply())
    for k in ("kind", "target", "payload"):
        assert D.parse_draft(json.dumps({x: v for x, v in ok.items() if x != k}), CAND, NOW) is None
    assert D.parse_draft(json.dumps({**ok, "target": 5}), CAND, NOW) is None
    assert D.parse_draft(json.dumps({**ok, "payload": []}), CAND, NOW) is None


def test_bool_is_not_a_number_for_config_but_is_a_valid_value():
    # bool is allowed as a config value; None / list / dict are not.
    assert D.parse_draft(reply("config", payload={"key": "compression.a", "value": False}),
                         CAND, NOW) is not None


def script(text, cost=0.01, i=100, o=50):
    return {"text": text, "input_tokens": i, "output_tokens": o, "estimated_cost_usd": cost}


def call_draft(bridge):
    return D.draft(CAND, None, None, {}, [("a", ("k",))], bridge, NOW, FLAT)


def test_draft_returns_fix_and_priced_spend():
    fix, spend = call_draft(FakeBridge(oneshot_script=[script(reply(), i=100, o=50)]))
    assert fix is not None and spend == pytest.approx(150 * FLAT_RATE / 1e6)


def test_draft_returns_spend_even_when_unparseable():
    fix, spend = call_draft(FakeBridge(oneshot_script=[script("sorry, no", i=2000, o=1000)]))
    assert fix is None and spend == pytest.approx(3000 * FLAT_RATE / 1e6)


@pytest.mark.parametrize("cost", [0.0, None, -1.0, 0.03, 5.0])
def test_spend_is_priced_from_tokens_whatever_the_billing_says(cost):
    _fix, spend = call_draft(FakeBridge(oneshot_script=[script("x", cost=cost, i=1_000_000, o=0)]))
    assert spend == pytest.approx(FLAT_RATE)


def test_spend_adds_input_and_output():
    _f, spend = call_draft(FakeBridge(oneshot_script=[
        script("x", cost=None, i=500_000, o=500_000)]))
    assert spend == pytest.approx(FLAT_RATE)


def test_bridge_error_propagates():
    with pytest.raises(BridgeError):
        call_draft(FakeBridge(oneshot_script=[script("x")], fail={"oneshot"}))


def test_draft_passes_request_and_max_tokens_to_oneshot():
    b = FakeBridge(oneshot_script=[script(reply())])
    D.draft(CAND, {"id": "j"}, None, {}, [], b, NOW, FLAT, max_tokens=123)
    (_m, (instr, user, mt)), = b.calls
    assert instr == D.INSTRUCTIONS and mt == 123
    assert json.loads(user)["candidate"]["key"] == CAND.key


JOB = {"id": "3bc4e11d29b1", "name": "triage", "schedule": "every 30m", "prompt": "P",
       "enabled_toolsets": ["x"], "origin": {"chat_id": "SECRET_x"}, "deliver": "SECRET_y",
       "extra": "SECRET_z"}


def test_build_request_content():
    instr, user = D.build_request(
        CAND, JOB, "SKILLTEXT", {"read_file": {"type": "object"}},
        [("mcp__google__search_gmail_messages", ("query",)), ("read_file", ())])
    doc = json.loads(user)
    assert doc["candidate"] == {"kind": "sequence", "key": "seq:a>b>c", "job_id": "3bc4e11d29b1",
                                "sessions": 12, "occurrences": 30, "result_chars": 90_000,
                                "est_monthly_usd": 1.5}
    assert doc["job"] == {"id": "3bc4e11d29b1", "name": "triage", "schedule": "every 30m",
                          "prompt": "P", "enabled_toolsets": ["x"]}
    assert doc["skill_text"] == "SKILLTEXT"
    assert doc["tool_schemas"] == {"read_file": {"type": "object"}}
    assert doc["call_shape"] == [["mcp__google__search_gmail_messages", ["query"]],
                                 ["read_file", []]]
    assert instr == D.INSTRUCTIONS


def test_build_request_omits_other_job_fields():
    _i, user = D.build_request(CAND, JOB, None, {}, [])
    assert "SECRET_" not in user and "chat_id" not in user
    doc = json.loads(user)
    assert "skill_text" not in doc


def test_build_request_without_job():
    _i, user = D.build_request(CAND, None, None, {}, [])
    assert "job" not in json.loads(user)


def test_instructions_state_the_contract():
    t = D.INSTRUCTIONS
    for name in sorted(policy.CALLABLE_TOOLS):
        assert name in t
    for prefix in policy.CONFIG_ALLOW_PREFIXES:
        assert prefix in t
    for kind in ("composed_tool", "job_prompt", "config", "skill", "new_reach"):
        assert kind in t
    for phrase in ("def main(args: dict, call)", "dry_run", "mutating", "byte-identical",
                   "web_search", "web_extract", "single JSON object", "why"):
        assert phrase in t
    assert "ignore previous" not in t.lower()
    assert policy.TOOL_PREFIX in t and policy.TOOLSET in t
    assert "fence" in t


def test_instructions_pass_no_obvious_injection_phrases():
    low = D.INSTRUCTIONS.lower()
    for bad in ("ignore previous", "ignore all", "disregard", "system prompt", "you are now"):
        assert bad not in low


@pytest.mark.parametrize("text", [
    '{"kind": "job_prompt", "target": "t", "payload": ' + "[" * 200_000 + "]" * 200_000 + "}",
    "[" * 200_000,
], ids=["nested-payload", "bare-brackets"])
def test_parse_draft_deeply_nested_reply_is_none(text):
    """m1: a reply that blows the JSON decoder's recursion is unparseable, not a crash."""
    assert D.parse_draft(text, CAND, NOW) is None


def test_draft_spend_priced_from_breakdown():
    from factory import V4_PRO
    reply_ = {"text": "x", "input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
              "cache_write_tokens": 1_000_000, "output_tokens": 1_000_000}
    _f, spend = D.draft(CAND, None, None, {}, [], FakeBridge(oneshot_script=[reply_]), NOW,
                        V4_PRO)
    # fresh 0.435 + cache read 2 x 0.003625 + cache write at the input rate 0.435 + out 0.87
    assert spend == pytest.approx(0.435 + 0.00725 + 0.435 + 0.87)
