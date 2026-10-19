"""The tuner's only door to Hermes (spec 4.5).

The tuner never imports Hermes. `HermesBridge` runs `bridge_entry.py` with
Hermes's own venv Python, one process per call, JSON on stdin and stdout.
`FakeBridge` is the in-memory stand-in for tests.
"""
from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

HERMES_PY = "/home/hermes/.hermes/hermes-agent/venv/bin/python"
ENTRY = Path(__file__).with_name("bridge_entry.py")
CALL_TIMEOUT_S = 120
RESTART_TIMEOUT_S = 60
STDERR_TAIL = 500


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


class Bridge(Protocol):
    def scan(self, text: str) -> list[str]: ...
    def get_job(self, job_id: str) -> dict | None: ...
    def list_jobs(self) -> list[dict]: ...
    def update_job(self, job_id: str, updates: dict) -> dict: ...
    def create_job(self, prompt: str, schedule: str, name: str, deliver: str,
                   enabled_toolsets: list[str] | None) -> dict: ...
    def remove_job(self, job_id: str) -> bool: ...
    def config_get(self, key: str) -> Any: ...
    def config_set(self, key: str, value: Any) -> None: ...
    def oneshot(self, instructions: str, user_input: str, max_tokens: int) -> dict: ...
    def restart_gateway(self) -> None: ...


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "").strip()[-STDERR_TAIL:]


class HermesBridge:
    def __init__(self, hermes_py: str = HERMES_PY, entry: Path = ENTRY,
                 timeout: float = CALL_TIMEOUT_S, runner: Callable = subprocess.run):
        self.hermes_py, self.entry, self.timeout, self._run = hermes_py, str(entry), timeout, runner

    def _call(self, op: str, **args: Any) -> Any:
        try:
            p = self._run([self.hermes_py, self.entry, op], input=json.dumps(args),
                          capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(f"{op} timed out after {self.timeout}s") from exc
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

    def create_job(self, prompt, schedule, name, deliver, enabled_toolsets):
        return self._call("create_job", prompt=prompt, schedule=schedule, name=name,
                          deliver=deliver, enabled_toolsets=enabled_toolsets)

    def remove_job(self, job_id):
        return self._call("remove_job", job_id=job_id)

    def config_get(self, key):
        return self._call("config_get", key=key)

    def config_set(self, key, value):
        self._call("config_set", key=key, value=encode_config_value(value))

    def oneshot(self, instructions, user_input, max_tokens):
        return self._call("oneshot", instructions=instructions, user_input=user_input,
                          max_tokens=max_tokens)

    def restart_gateway(self):
        try:
            p = self._run(["systemctl", "--user", "restart", "hermes-gateway"],
                          capture_output=True, text=True, timeout=RESTART_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise BridgeError(f"restart_gateway: {type(exc).__name__}: {exc}") from exc
        if p.returncode != 0:
            raise BridgeError(f"restart_gateway failed (exit {p.returncode}): {_tail(p.stderr)}")


class FakeBridge:
    """In-memory Bridge. `calls` records (method, args) for every call, including
    ones told to fail via `fail` (a set of method names)."""

    MUTATING = frozenset({"update_job", "create_job", "remove_job", "config_set",
                          "restart_gateway", "oneshot"})

    def __init__(self, jobs: dict[str, dict] | None = None, config: dict | None = None,
                 oneshot_script: list[dict] | None = None, fail: set[str] | None = None,
                 scan: Callable[[str], list[str]] | None = None):
        self.jobs = copy.deepcopy(jobs or {})
        self.config = copy.deepcopy(config or {})
        self.oneshot_script = list(oneshot_script or [])
        self.fail = set(fail or ())
        self._scan = scan or (lambda text: [])
        self.calls: list[tuple[str, tuple]] = []

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

    def create_job(self, prompt, schedule, name, deliver, enabled_toolsets):
        self._enter("create_job", prompt, schedule, name, deliver, enabled_toolsets)
        job_id = f"fake{len(self.jobs):08x}"
        self.jobs[job_id] = {"id": job_id, "prompt": prompt, "schedule": schedule,
                             "name": name, "deliver": deliver,
                             "enabled_toolsets": copy.deepcopy(enabled_toolsets)}
        return copy.deepcopy(self.jobs[job_id])

    def remove_job(self, job_id):
        self._enter("remove_job", job_id)
        return self.jobs.pop(job_id, None) is not None

    def config_get(self, key):
        self._enter("config_get", key)
        return copy.deepcopy(self.config.get(key))

    def config_set(self, key, value):
        self._enter("config_set", key, value)
        encode_config_value(value)  # same type limits as the real bridge
        self.config[key] = copy.deepcopy(value)

    def oneshot(self, instructions, user_input, max_tokens):
        self._enter("oneshot", instructions, user_input, max_tokens)
        if not self.oneshot_script:
            raise BridgeError("oneshot script exhausted")
        return self.oneshot_script.pop(0)

    def restart_gateway(self):
        self._enter("restart_gateway")
