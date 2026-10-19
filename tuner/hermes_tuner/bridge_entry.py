"""Runs inside Hermes's venv Python: `python bridge_entry.py <op>`.

Reads a JSON object of keyword arguments on stdin and writes
`{"result": ...}` on stdout. On any exception it writes `{"error": "..."}`
and exits 1. Stdlib only at import time: every Hermes import is inside an op
function, so this file compiles and imports without Hermes installed.
Exercised on the droplet (plan Task 11), not in CI.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys


def scan(text):
    from tools.threat_patterns import scan_for_threats
    return scan_for_threats(text, scope="context")


def get_job(job_id):
    from cron.jobs import get_job as hermes_get_job
    return hermes_get_job(job_id)


def list_jobs():
    from cron.jobs import list_jobs as hermes_list_jobs
    return hermes_list_jobs(include_disabled=True)


def update_job(job_id, updates):
    from cron.jobs import update_job as hermes_update_job
    job = hermes_update_job(job_id, updates)
    if job is None:
        raise LookupError(f"job {job_id} not found")
    return job


def create_job(prompt, schedule, name, deliver, enabled_toolsets):
    from cron.jobs import create_job as hermes_create_job
    return hermes_create_job(prompt, schedule, name=name, deliver=deliver,
                             enabled_toolsets=enabled_toolsets)


def remove_job(job_id):
    from cron.jobs import remove_job as hermes_remove_job
    return hermes_remove_job(job_id)


def config_get(key):
    """The resolved value as JSON-decoded data, or None when the key is unset.
    get_config_value prints (and sys.exit(1)s when unset), so capture stdout."""
    from hermes_cli.config import get_config_value
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            get_config_value(key, as_json=True)
    except SystemExit:
        return None
    return json.loads(buf.getvalue())


def config_set(key, value):
    """`value` is a string (see bridge.encode_config_value); Hermes coerces it."""
    from hermes_cli.config import set_config_value
    if not isinstance(value, str):
        raise TypeError("config_set value must be a string")
    set_config_value(key, value)


def config_set_json(key, value):
    """Set `key` to a JSON value (a list or dict). `config_set` only takes strings,
    so this repeats set_config_value's write path for config.yaml: read the raw user
    config, set the nested key, write it back atomically. Only the approved new-reach
    change (tuner/proposals.py) calls it."""
    from hermes_cli.config import (
        _set_nested, ensure_hermes_home, get_config_path, is_managed,
        require_readable_config_before_write)
    from utils import atomic_yaml_write, fast_safe_load
    if is_managed():
        raise RuntimeError("config is managed; cannot set values")
    config_path = get_config_path()
    require_readable_config_before_write(config_path)
    user_config = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            user_config = fast_safe_load(f) or {}
    _set_nested(user_config, key, value)
    ensure_hermes_home()
    atomic_yaml_write(config_path, user_config, sort_keys=False)


def oneshot(instructions, user_input, max_tokens):
    """One LLM call on the main provider/model from config. Subscription billing
    reports $0 ("included"), so the raw token counts are returned too."""
    from agent.auxiliary_client import call_llm
    from agent.usage_pricing import estimate_usage_cost, normalize_usage
    from hermes_cli.config import load_config
    cfg = load_config().get("model")
    cfg = cfg if isinstance(cfg, dict) else {"default": cfg}
    model = cfg.get("default") or cfg.get("model")
    provider, base_url = cfg.get("provider") or None, cfg.get("base_url") or None
    if not isinstance(model, str) or not model:
        raise RuntimeError("no main model configured")
    resp = call_llm(provider=provider, model=model, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": instructions},
                              {"role": "user", "content": user_input}])
    usage = normalize_usage(getattr(resp, "usage", None), provider=provider)
    cost = estimate_usage_cost(model, usage, provider=provider, base_url=base_url)
    return {"text": resp.choices[0].message.content or "",
            "input_tokens": usage.prompt_tokens, "output_tokens": usage.output_tokens,
            "estimated_cost_usd": None if cost.amount_usd is None else float(cost.amount_usd),
            "cost_status": cost.status}


OPS = {f.__name__: f for f in (scan, get_job, list_jobs, update_job, create_job,
                               remove_job, config_get, config_set, config_set_json, oneshot)}


def main(argv, stdin, stdout, stderr):
    try:
        op = argv[1] if len(argv) > 1 else ""
        if op not in OPS:
            raise ValueError(f"unknown op {op!r}")
        args = json.loads(stdin.read() or "{}")
        with contextlib.redirect_stdout(stderr):  # Hermes prints; keep stdout for JSON
            result = OPS[op](**args)
        stdout.write(json.dumps({"result": result}, default=str))
        return 0
    except BaseException as exc:  # noqa: BLE001 - SystemExit from Hermes included
        stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin, sys.stdout, sys.stderr))
