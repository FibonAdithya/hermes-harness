"""The tuner's only door to Hermes (spec 4.5).

The tuner never imports Hermes. `HermesBridge` runs `bridge_entry.py` with
Hermes's own venv Python, one process per call, JSON on stdin and stdout.
`FakeBridge` is the in-memory stand-in for tests.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

HERMES_PY = "/home/hermes/.hermes/hermes-agent/venv/bin/python"
ENTRY = Path(__file__).with_name("bridge_entry.py")
CALL_TIMEOUT_S = 120
# The drafting call. Hermes's call_llm waits 30 s by default and deepseek-v4-pro needs
# longer (MEASURED 2026-10-21: 139.2 s and 64.7 s for two real drafts), so the tuner
# passes its own per-attempt timeout. The tuner waits for one full attempt plus a margin,
# not for Hermes's retries (up to 3 attempts): a cycle drafts up to 3 fixes in sequence
# and the service has 1800 s (droplet/units/hermes-tuner.service). A draft that times out
# once is charged and reported; the next cycle tries again.
ONESHOT_TIMEOUT_S = 300
ONESHOT_WAIT_S = 360
RESTART_TIMEOUT_S = 60
STDERR_TAIL = 500
# Live-job fields a shadow job copies so it runs like the live one (spec 4.5).
# Never origin, deliver, repeat, no_agent or attach_to_session.
CREATE_JOB_EXTRA_KEYS = ("skills", "skill", "model", "provider", "base_url", "workdir",
                         "context_from", "script")


class BridgeError(Exception):
    """A Hermes action failed (non-zero exit, bad output, timeout)."""


def encode_config_value(value: Any) -> str:
    """The string `hermes config set` takes for `value`.

    Hermes coerces the string itself: true/yes/on and false/no/off become
    booleans and digit strings become numbers, unless the key's default is a
    string. So bool -> "true"/"false", numbers -> str(n), str unchanged.
    None, lists and dicts cannot be passed this way and raise ValueError.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError(f"config value of type {type(value).__name__} cannot be set")


def hermes_coerce(value_str: str, default_is_str: bool) -> Any:
    """What Hermes's `set_config_value` (config.py) stores for the string `value_str`.

    The coercion applies only when the key's default in DEFAULT_CONFIG is not a
    string (unknown keys included); string-default keys store the string as is."""
    if default_is_str:
        return value_str
    low = value_str.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if value_str.isdigit():
        return int(value_str)
    if value_str.replace(".", "", 1).isdigit():
        return float(value_str)
    return value_str


# The only keys config_set_json may write: an MCP server's tools.include list (spec 4.6).
CONFIG_JSON_KEY = re.compile(r"mcp_servers\.[A-Za-z0-9_-]+\.tools\.include")


def is_setting_value(v: Any) -> bool:
    """A value `hermes config set` can write and the tuner can roll back: a string,
    number or boolean. None (unset), lists and dicts are not."""
    return isinstance(v, (str, int, float, bool))


def check_config_json_key(key: object) -> None:
    if not isinstance(key, str) or not CONFIG_JSON_KEY.fullmatch(key):
        raise ValueError(f"config_set_json refuses key {key!r}")


def check_create_job_extra(extra: dict | None) -> dict:
    """`extra` as a dict, or ValueError if it has a key off CREATE_JOB_EXTRA_KEYS."""
    if extra is None:
        return {}
    if not isinstance(extra, dict):
        raise ValueError("create_job extra must be an object")
    bad = sorted(set(extra) - set(CREATE_JOB_EXTRA_KEYS))
    if bad:
        raise ValueError(f"create_job extra keys not allowed: {bad}")
    return dict(extra)


class Bridge(Protocol):
    def scan(self, text: str) -> list[str]: ...
    def get_job(self, job_id: str) -> dict | None: ...
    def list_jobs(self) -> list[dict]: ...
    def update_job(self, job_id: str, updates: dict) -> dict: ...
    def create_job(self, prompt: str, schedule: str, name: str, deliver: str,
                   enabled_toolsets: list[str] | None, extra: dict | None = None) -> dict: ...
    def remove_job(self, job_id: str) -> bool: ...
    def config_get(self, key: str) -> Any: ...
    def config_default_is_str(self, key: str) -> bool: ...
    def config_set(self, key: str, value: Any) -> None: ...
    def config_set_json(self, key: str, value: Any) -> None: ...
    def config_settings(self, prefixes: list[str]) -> dict[str, Any]: ...
    def oneshot(self, instructions: str, user_input: str, max_tokens: int) -> dict: ...
    def pricing(self) -> dict: ...
    def restart_gateway(self) -> None: ...


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "").strip()[-STDERR_TAIL:]


class HermesBridge:
    def __init__(self, hermes_py: str = HERMES_PY, entry: Path = ENTRY,
                 timeout: float = CALL_TIMEOUT_S, runner: Callable = subprocess.run):
        self.hermes_py, self.entry, self.timeout, self._run = hermes_py, str(entry), timeout, runner

    def _call(self, op: str, _wait: float | None = None, **args: Any) -> Any:
        wait = self.timeout if _wait is None else _wait
        try:
            p = self._run([self.hermes_py, self.entry, op], input=json.dumps(args),
                          capture_output=True, text=True, timeout=wait)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(f"{op} timed out after {wait}s") from exc
        except OSError as exc:
            raise BridgeError(f"{op}: cannot run {self.hermes_py}: {exc}") from exc
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = None
        if p.returncode != 0:
            detail = out.get("error") if isinstance(out, dict) else None
            raise BridgeError(f"{op} failed (exit {p.returncode}): "
                              f"{_tail(detail) or _tail(p.stderr)}")
        if not isinstance(out, dict) or "result" not in out:
            raise BridgeError(f"{op}: output is not a JSON result: {_tail(p.stdout)}")
        return out["result"]

    def scan(self, text):
        return self._call("scan", text=text)

    def get_job(self, job_id):
        return self._call("get_job", job_id=job_id)

    def list_jobs(self):
        return self._call("list_jobs")

    def update_job(self, job_id, updates):
        return self._call("update_job", job_id=job_id, updates=updates)

    def create_job(self, prompt, schedule, name, deliver, enabled_toolsets, extra=None):
        return self._call("create_job", prompt=prompt, schedule=schedule, name=name,
                          deliver=deliver, enabled_toolsets=enabled_toolsets,
                          extra=check_create_job_extra(extra))

    def remove_job(self, job_id):
        return self._call("remove_job", job_id=job_id)

    def config_get(self, key):
        return self._call("config_get", key=key)

    def config_default_is_str(self, key):
        """True when Hermes's default for `key` is a string, so `config set` stores
        strings for it unchanged. Raises BridgeError when Hermes cannot say."""
        return self._call("config_default_is_str", key=key)

    def config_set(self, key, value):
        self._call("config_set", key=key, value=encode_config_value(value))

    def config_set_json(self, key, value):
        """Set `key` to a JSON value (a list or dict that `config_set` cannot carry).
        Used only for owner-approved new-reach changes (spec 4.6); any key but
        mcp_servers.<server>.tools.include raises ValueError before Hermes runs."""
        check_config_json_key(key)
        self._call("config_set_json", key=key, value=value)

    def config_settings(self, prefixes):
        """Read-only: {key: value} for Hermes's resolved settings under `prefixes`
        (see bridge_entry.config_settings)."""
        return self._call("config_settings", prefixes=list(prefixes))

    def oneshot(self, instructions, user_input, max_tokens):
        return self._call("oneshot", _wait=ONESHOT_WAIT_S, instructions=instructions,
                          user_input=user_input, max_tokens=max_tokens,
                          timeout=ONESHOT_TIMEOUT_S)

    def pricing(self):
        """The configured main model's list prices (see bridge_entry.pricing)."""
        return self._call("pricing")

    def restart_gateway(self):
        try:
            p = self._run(["systemctl", "--user", "restart", "hermes-gateway"],
                          capture_output=True, text=True, timeout=RESTART_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise BridgeError(f"restart_gateway: {type(exc).__name__}: {exc}") from exc
        if p.returncode != 0:
            raise BridgeError(f"restart_gateway failed (exit {p.returncode}): {_tail(p.stderr)}")


# FakeBridge's default price list: one flat rate for every token kind, so test
# arithmetic stays simple. Tests that need a real list set `price`.
FAKE_PRICE = {"model": "fake-model", "input_per_m": 0.42, "output_per_m": 0.42,
              "cache_read_per_m": None, "cache_write_per_m": None, "source": "fake",
              "source_url": None, "pricing_version": "fake-flat"}


class FakeBridge:
    """In-memory Bridge. `calls` records (method, args) for every call, including
    ones told to fail via `fail` (a set of method names). `price` is what
    `pricing()` returns (a copy). `string_default_keys` are the config keys whose
    Hermes default is a string; `config_set` coerces like Hermes for the others."""

    MUTATING = frozenset({"update_job", "create_job", "remove_job", "config_set",
                          "config_set_json", "restart_gateway", "oneshot"})

    def __init__(self, jobs: dict[str, dict] | None = None, config: dict | None = None,
                 oneshot_script: list[dict] | None = None, fail: set[str] | None = None,
                 scan: Callable[[str], list[str]] | None = None,
                 price: dict | None = None,
                 string_default_keys: set[str] | None = None):
        self.string_default_keys = set(string_default_keys or ())
        self.price = copy.deepcopy(FAKE_PRICE if price is None else price)
        self.jobs = copy.deepcopy(jobs or {})
        self.config = copy.deepcopy(config or {})
        self.oneshot_script = list(oneshot_script or [])
        self.fail = set(fail or ())
        self._scan = scan or (lambda text: [])
        self.calls: list[tuple[str, tuple]] = []
        self._next_id = 0

    def _enter(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))
        if method in self.fail:
            raise BridgeError(f"{method} failed (fake)")

    def scan(self, text):
        self._enter("scan", text)
        return list(self._scan(text))

    def get_job(self, job_id):
        self._enter("get_job", job_id)
        return copy.deepcopy(self.jobs.get(job_id))

    def list_jobs(self):
        self._enter("list_jobs")
        return copy.deepcopy(list(self.jobs.values()))

    def update_job(self, job_id, updates):
        self._enter("update_job", job_id, updates)
        if job_id not in self.jobs:
            raise BridgeError(f"job {job_id} not found")
        self.jobs[job_id].update(copy.deepcopy(updates))
        return copy.deepcopy(self.jobs[job_id])

    def create_job(self, prompt, schedule, name, deliver, enabled_toolsets, extra=None):
        self._enter("create_job", prompt, schedule, name, deliver, enabled_toolsets,
                    copy.deepcopy(extra))
        extra = check_create_job_extra(extra)
        # Monotonic: len(self.jobs) would reuse an id after remove_job.
        self._next_id += 1
        job_id = f"{self._next_id:012x}"  # 12 lowercase hex, like Hermes job ids
        self.jobs[job_id] = {"id": job_id, "prompt": prompt, "schedule": schedule,
                             "next_run_at": schedule, "name": name, "deliver": deliver,
                             "enabled_toolsets": copy.deepcopy(enabled_toolsets),
                             **copy.deepcopy(extra)}
        return copy.deepcopy(self.jobs[job_id])

    def remove_job(self, job_id):
        self._enter("remove_job", job_id)
        return self.jobs.pop(job_id, None) is not None

    def config_get(self, key):
        self._enter("config_get", key)
        return copy.deepcopy(self.config.get(key))

    def config_default_is_str(self, key):
        self._enter("config_default_is_str", key)
        return key in self.string_default_keys

    def config_set(self, key, value):
        self._enter("config_set", key, value)
        # Same type limits and the same per-key coercion as Hermes.
        self.config[key] = hermes_coerce(encode_config_value(value),
                                         key in self.string_default_keys)

    def config_set_json(self, key, value):
        self._enter("config_set_json", key, value)
        check_config_json_key(key)
        self.config[key] = copy.deepcopy(value)

    def config_settings(self, prefixes):
        """Like bridge_entry.config_settings over the flat `config` dict."""
        self._enter("config_settings", list(prefixes))
        return {k: copy.deepcopy(v) for k, v in self.config.items()
                if any(k.startswith(p) for p in prefixes) and is_setting_value(v)}

    def oneshot(self, instructions, user_input, max_tokens):
        self._enter("oneshot", instructions, user_input, max_tokens)
        if not self.oneshot_script:
            raise BridgeError("oneshot script exhausted")
        return self.oneshot_script.pop(0)

    def pricing(self):
        self._enter("pricing")
        return copy.deepcopy(self.price)

    def restart_gateway(self):
        self._enter("restart_gateway")
