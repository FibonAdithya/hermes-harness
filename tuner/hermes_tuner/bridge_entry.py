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
import re
import sys

# Same pattern as bridge.CONFIG_JSON_KEY (this file cannot import the tuner).
CONFIG_JSON_KEY = re.compile(r"mcp_servers\.[A-Za-z0-9_-]+\.tools\.include")


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


# Same list as bridge.CREATE_JOB_EXTRA_KEYS (this file cannot import the tuner).
CREATE_JOB_EXTRA_KEYS = ("skills", "skill", "model", "provider", "base_url", "workdir",
                         "context_from", "script")


def create_job(prompt, schedule, name, deliver, enabled_toolsets, extra=None):
    """`extra`: live-job fields for a shadow; only CREATE_JOB_EXTRA_KEYS, never
    origin/deliver/repeat/no_agent/attach_to_session."""
    extra = extra or {}
    if not isinstance(extra, dict) or set(extra) - set(CREATE_JOB_EXTRA_KEYS):
        raise ValueError("create_job extra has keys off the allowlist")
    from cron.jobs import create_job as hermes_create_job
    return hermes_create_job(prompt, schedule, name=name, deliver=deliver,
                             enabled_toolsets=enabled_toolsets, **extra)


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


def config_default_is_str(key):
    """True when Hermes's default for `key` (a DEFAULT_CONFIG leaf) is a string, so
    `config set` stores strings for it unchanged. Read-only. An import failure
    propagates (exit 1): the caller refuses the change rather than guess."""
    from hermes_cli.config import _default_value_for_key
    return isinstance(_default_value_for_key(key), str)


def _leaves(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict) and v:
            yield from _leaves(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def config_settings(prefixes):
    """Read-only. {dotted key: value} for every leaf of the resolved config (defaults
    merged with config.yaml) under one of `prefixes`, keeping only strings, numbers and
    booleans: the values `config set` can write and the tuner can roll back."""
    from hermes_cli.config import load_config
    out = {}
    for key, value in _leaves(load_config()):
        if (any(key.startswith(p) for p in prefixes)
                and isinstance(value, (str, int, float, bool))):
            out[key] = value
    return out


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
    change (tuner/proposals.py) calls it, and only for an MCP tools.include key."""
    if not isinstance(key, str) or not CONFIG_JSON_KEY.fullmatch(key):
        raise ValueError(f"config_set_json refuses key {key!r}")
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


def _main_model():
    """(model, provider, base_url) of the main model from Hermes's config."""
    from hermes_cli.config import load_config
    cfg = load_config().get("model")
    cfg = cfg if isinstance(cfg, dict) else {"default": cfg}
    model = cfg.get("default") or cfg.get("model")
    provider, base_url = cfg.get("provider") or None, cfg.get("base_url") or None
    if not isinstance(model, str) or not model:
        raise RuntimeError("no main model configured")
    return model, provider, base_url


def oneshot(instructions, user_input, max_tokens, timeout):
    """One LLM call on the main provider/model from config, with a per-attempt
    `timeout` in seconds (call_llm's default is 30 s). The raw token counts are
    returned so the tuner prices the call itself, split as Hermes's CanonicalUsage
    splits them: input_tokens is FRESH input (not the prompt total), cache reads and
    writes are separate."""
    from agent.auxiliary_client import call_llm
    from agent.usage_pricing import estimate_usage_cost, normalize_usage
    model, provider, base_url = _main_model()
    resp = call_llm(provider=provider, model=model, max_tokens=max_tokens, timeout=timeout,
                    messages=[{"role": "system", "content": instructions},
                              {"role": "user", "content": user_input}])
    usage = normalize_usage(getattr(resp, "usage", None), provider=provider)
    cost = estimate_usage_cost(model, usage, provider=provider, base_url=base_url)
    return {"text": resp.choices[0].message.content or "",
            "input_tokens": usage.input_tokens, "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "estimated_cost_usd": None if cost.amount_usd is None else float(cost.amount_usd),
            "cost_status": cost.status}


def _num(v):
    return None if v is None else float(v)


def _usable(entry):
    """A real price list: input and output both known and not both zero. Hermes's
    `included-route` entry (all zero) and an all-None entry are not."""
    if entry is None:
        return False
    inp, out = entry.input_cost_per_million, entry.output_cost_per_million
    return inp is not None and out is not None and (inp > 0 or out > 0)


def lookup_pricing(model, provider, base_url, api_key, get_entry, vendors, detect_vendor=None):
    """The main model's price list. `get_entry` is Hermes's get_pricing_entry and
    `vendors` the providers Hermes has docs snapshots for, `detect_vendor` Hermes's
    model_normalize.detect_vendor (all injected so this runs without Hermes in tests).

    1. get_entry(model, provider=, base_url=, api_key=) - the billing route's list
       (provider /models API, else a docs snapshot);
    2. not usable (None, or no real rates) -> the model's vendor: the part of
       `model` before "/" (looked up with the part after); for an id without "/",
       `provider` if it is in `vendors`, else the maker `detect_vendor(model)` names
       if that is in `vendors` (never a search over vendors: the same model id has
       different prices at different vendors); get_entry(bare, provider=vendor);
    3. still nothing usable -> LookupError("no pricing for <model>")."""
    entry = get_entry(model, provider=provider, base_url=base_url, api_key=api_key)
    if not _usable(entry):
        entry = None
        if "/" in model:
            vendor, bare = model.split("/", 1)
        else:
            bare = model
            vendor = provider if provider in vendors else None
            if vendor is None and detect_vendor is not None:
                maker = detect_vendor(model)
                vendor = maker if maker in vendors else None
        if vendor:
            retry = get_entry(bare, provider=vendor)
            entry = retry if _usable(retry) else None
    if entry is None:
        raise LookupError(f"no pricing for {model}")
    return {"model": model,
            "input_per_m": _num(entry.input_cost_per_million),
            "output_per_m": _num(entry.output_cost_per_million),
            "cache_read_per_m": _num(entry.cache_read_cost_per_million),
            "cache_write_per_m": _num(entry.cache_write_cost_per_million),
            "source": entry.source, "source_url": entry.source_url,
            "pricing_version": entry.pricing_version}


def _main_api_key(provider, model):
    """The main provider's credential, resolved by Hermes's auxiliary client the way
    call_llm resolves it. Best effort: "" when it cannot be resolved (the /models
    lookup then fails or comes back empty and the vendor retry decides)."""
    try:
        from agent.auxiliary_client import resolve_provider_client
        client, _model = resolve_provider_client(provider or "auto", model)
        key = getattr(client, "api_key", "") if client is not None else ""
        return key if isinstance(key, str) else ""
    except Exception:  # noqa: BLE001 - best effort by design
        return ""


def pricing():
    """The configured main model's list prices in USD per million tokens, from
    Hermes's get_pricing_entry (lookup_pricing gives the order). Never scrapes a
    web page: Hermes's dated docs snapshot is the fallback."""
    from agent.usage_pricing import _OFFICIAL_DOCS_PRICING, get_pricing_entry
    from hermes_cli.model_normalize import detect_vendor
    model, provider, base_url = _main_model()
    vendors = {p for p, _m in _OFFICIAL_DOCS_PRICING}
    return lookup_pricing(model, provider, base_url, _main_api_key(provider, model),
                          get_pricing_entry, vendors, detect_vendor)


OPS = {f.__name__: f for f in (scan, get_job, list_jobs, update_job, create_job,
                               remove_job, config_get, config_default_is_str,
                               config_set, config_set_json, config_settings, oneshot,
                               pricing)}


def load_hermes_env():
    """Load Hermes's .env (provider keys, secret sources) the way its own entry points
    do (run_agent.py, cron/scheduler.py): load_hermes_dotenv for the Hermes home.
    Imports inside the function so this file compiles without Hermes."""
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_constants import get_hermes_home
    load_hermes_dotenv(hermes_home=get_hermes_home())


def main(argv, stdin, stdout, stderr):
    try:
        op = argv[1] if len(argv) > 1 else ""
        if op not in OPS:
            raise ValueError(f"unknown op {op!r}")
        args = json.loads(stdin.read() or "{}")
        with contextlib.redirect_stdout(stderr):  # Hermes prints; keep stdout for JSON
            load_hermes_env()
            result = OPS[op](**args)
        stdout.write(json.dumps({"result": result}, default=str))
        return 0
    except BaseException as exc:  # noqa: BLE001 - SystemExit from Hermes included
        stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin, sys.stdout, sys.stderr))
