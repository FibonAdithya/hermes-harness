"""The drafting call's timeout and the read-only settings listing (fix/tuner-first-run)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_tuner import bridge as B
from hermes_tuner import bridge_entry, policy
from hermes_tuner.bridge import FakeBridge, HermesBridge

UNIT = Path(__file__).resolve().parents[2] / "droplet" / "units" / "hermes-tuner.service"
# MEASURED 2026-10-21 on the droplet: two real drafting requests (triage, calendar) to
# deepseek/deepseek-v4-pro via Nous took 139.2 s (8174 output tokens) and 64.7 s (4247).
SLOWEST_MEASURED_DRAFT_S = 139.2


def capture_runner(result):
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["input"], seen["timeout"] = argv, json.loads(kw["input"]), kw["timeout"]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")
    return seen, runner


# --- drafting timeout ------------------------------------------------------------------

def test_hermes_bridge_oneshot_sends_timeout_and_waits_past_it():
    """Catches: the oneshot op sent without a timeout (Hermes then waits 30 s), or the
    tuner's subprocess wait left at CALL_TIMEOUT_S (120 s, below one slow draft)."""
    seen, runner = capture_runner({"text": "{}"})
    HermesBridge(runner=runner).oneshot("i", "u", 4000)
    assert seen["argv"][-1] == "oneshot"
    assert seen["input"] == {"instructions": "i", "user_input": "u", "max_tokens": 4000,
                             "timeout": B.ONESHOT_TIMEOUT_S}
    assert seen["timeout"] == B.ONESHOT_WAIT_S


def test_other_ops_keep_the_short_wait():
    """Catches: the long drafting wait applied to every op."""
    seen, runner = capture_runner([])
    HermesBridge(runner=runner).scan("x")
    assert seen["timeout"] == B.CALL_TIMEOUT_S


def test_oneshot_timeout_is_well_above_the_slowest_measured_draft():
    """Catches: the timeout dropped back toward Hermes's 30 s default."""
    assert B.ONESHOT_TIMEOUT_S >= 2 * SLOWEST_MEASURED_DRAFT_S
    # the tuner waits for one full attempt plus room for the reply to come back
    assert B.ONESHOT_WAIT_S > B.ONESHOT_TIMEOUT_S


def test_a_cycle_of_drafts_fits_the_service_timeout():
    """A cycle drafts at most one fix per editable job plus one global fix, in sequence.
    Catches: a per-call wait so long that systemd kills the cycle mid-run."""
    m = re.search(r"^TimeoutStartSec=(\d+)$", UNIT.read_text(), re.M)
    assert m, f"no TimeoutStartSec in {UNIT}"
    max_drafts = len(policy.EDITABLE_JOBS) + 1
    # 300 s left for everything else the cycle does. ESTIMATE (unverified): ~40 s on
    # 2026-10-21 (230 s run in the journal minus two drafts of ~3 x 30 s each).
    assert max_drafts * B.ONESHOT_WAIT_S + 300 <= int(m.group(1))


def fake_hermes(monkeypatch, seen):
    """Stand-ins for the Hermes modules bridge_entry.oneshot imports."""
    aux = types.ModuleType("agent.auxiliary_client")

    def call_llm(**kw):
        seen.update(kw)
        msg = SimpleNamespace(content="{}")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
    aux.call_llm = call_llm
    up = types.ModuleType("agent.usage_pricing")
    up.normalize_usage = lambda usage, provider=None: SimpleNamespace(
        input_tokens=1, cache_read_tokens=0, cache_write_tokens=0, output_tokens=2)
    up.estimate_usage_cost = lambda *a, **k: SimpleNamespace(amount_usd=None, status="unknown")
    cfg = types.ModuleType("hermes_cli.config")
    cfg.load_config = lambda: {"model": {"default": "m", "provider": "p", "base_url": None}}
    for name, mod in {"agent": types.ModuleType("agent"), "agent.auxiliary_client": aux,
                      "agent.usage_pricing": up, "hermes_cli": types.ModuleType("hermes_cli"),
                      "hermes_cli.config": cfg}.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_bridge_entry_oneshot_passes_the_timeout_to_call_llm(monkeypatch):
    """Catches: bridge_entry dropping the timeout, so call_llm falls back to 30 s."""
    seen = {}
    fake_hermes(monkeypatch, seen)
    out = bridge_entry.oneshot("i", "u", 4000, timeout=300)
    assert seen["timeout"] == 300 and seen["max_tokens"] == 4000
    assert seen["provider"] == "p" and seen["model"] == "m"
    assert out["output_tokens"] == 2


# --- settings listing ------------------------------------------------------------------

def test_bridge_entry_config_settings_lists_scalar_leaves_under_prefixes(monkeypatch):
    """Catches: listing nested dicts, nulls or lists (none can be set and rolled back
    through `hermes config set`), or keys outside the prefixes."""
    cfg = types.ModuleType("hermes_cli.config")
    cfg.load_config = lambda: {
        "tools": {"tool_search": {"enabled": "auto", "threshold_pct": 5},
                  "other": {"x": 1}},
        "compression": {"enabled": True, "threshold": 0.5, "threshold_tokens": None,
                        "model_thresholds": {}, "list": [1], "deep": {"a": 2}},
        "display": {"y": 1}}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg)
    got = bridge_entry.config_settings(["tools.tool_search.", "compression."])
    assert got == {"tools.tool_search.enabled": "auto", "tools.tool_search.threshold_pct": 5,
                   "compression.enabled": True, "compression.threshold": 0.5,
                   "compression.deep.a": 2}


def test_hermes_bridge_config_settings_sends_op():
    seen, runner = capture_runner({"compression.enabled": True})
    got = HermesBridge(runner=runner).config_settings(["compression."])
    assert got == {"compression.enabled": True}
    assert seen["argv"][-1] == "config_settings"
    assert seen["input"] == {"prefixes": ["compression."]}


def test_fake_bridge_config_settings_is_read_only_and_filters():
    b = FakeBridge(config={"compression.threshold": 0.5, "compression.none": None,
                           "compression.list": [1], "display.x": 1})
    assert b.config_settings(["compression."]) == {"compression.threshold": 0.5}
    assert "config_settings" not in FakeBridge.MUTATING
    assert b.calls == [("config_settings", (["compression."],))]


def test_fake_bridge_config_settings_can_fail():
    with pytest.raises(B.BridgeError):
        FakeBridge(fail={"config_settings"}).config_settings(["x."])
