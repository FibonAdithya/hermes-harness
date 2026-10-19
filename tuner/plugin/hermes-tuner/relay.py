"""Host side of a composed-tool run (spec 5.2).

The program is model-drafted, so it is untrusted. It runs in a throwaway
`--network=none` container (see `production_argv`); every tool call it makes
comes back over stdin/stdout and is checked here before `call` (in production
`ctx.dispatch_tool`) runs it. The wall-clock deadline covers the whole run:
reads and writes on the child's pipes are non-blocking and bounded by it.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

_TUNER_DIR = str(Path(__file__).resolve().parents[2])
if _TUNER_DIR not in sys.path:
    sys.path.insert(0, _TUNER_DIR)

from hermes_tuner import policy  # noqa: E402

STUB_SOURCE = (Path(__file__).resolve().parent / "stub.py").read_text()

LINE_CAP = 1_000_000  # longest protocol line accepted from the sandbox, bytes
ERROR_CAP = 2_000  # longest error message passed on, characters
NAME_CAP = 200
REAP_TIMEOUT_S = 5
# Tools that send data out. Refused once the run has forwarded any other call,
# so a program cannot read mail and then put it in a search query or URL.
WEB_TOOLS = frozenset({"web_search", "web_extract"})
DOCKER_RM_TIMEOUT_S = 15


def production_argv(container_name: str) -> list[str]:
    return [*policy.SANDBOX_ARGV, "--name", container_name, policy.SANDBOX_IMAGE,
            "python3", "-u", "-c", STUB_SOURCE]


class _Abort(Exception):
    pass


def _error(message: str, max_output: int) -> str:
    out = json.dumps({"error": message[:ERROR_CAP]})
    if len(out.encode()) > max_output:
        out = json.dumps({"error": "error"})
    return out


class _Channel:
    """Line-framed JSON over the child's pipes, bounded by a deadline."""

    def __init__(self, proc: subprocess.Popen, deadline: float, timeout_s: float):
        self.deadline = deadline
        self.timeout_s = timeout_s
        self.buf = bytearray()
        self.rfd = proc.stdout.fileno()
        self.wfd = proc.stdin.fileno()
        os.set_blocking(self.rfd, False)
        os.set_blocking(self.wfd, False)
        self.rsel = selectors.DefaultSelector()
        self.rsel.register(self.rfd, selectors.EVENT_READ)
        self.wsel = selectors.DefaultSelector()
        self.wsel.register(self.wfd, selectors.EVENT_WRITE)

    def close(self) -> None:
        self.rsel.close()
        self.wsel.close()

    def _remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _Abort(f"program timed out after {self.timeout_s}s")
        return left

    def send(self, obj: dict) -> None:
        view = memoryview((json.dumps(obj) + "\n").encode())
        while view:
            if not self.wsel.select(self._remaining()):
                continue
            try:
                n = os.write(self.wfd, view)
            except BlockingIOError:
                continue
            except (BrokenPipeError, ConnectionResetError):
                raise _Abort("sandbox closed the channel") from None
            view = view[n:]

    def recv(self) -> bytes | None:
        """Next line without its newline, or None at end of output."""
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line
            if len(self.buf) > LINE_CAP:
                raise _Abort(f"sandbox message exceeds {LINE_CAP} bytes")
            if not self.rsel.select(self._remaining()):
                continue
            try:
                chunk = os.read(self.rfd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                return None
            self.buf += chunk


class _Run:
    def __init__(self, allowed: frozenset[str], dry_run: bool, intents: list[dict]):
        self.allowed = allowed
        self.dry_run = dry_run
        self.intents = intents
        self.read = False  # a non-web call has been forwarded


def _dispatch(msg: dict, call: Callable[[str, dict], str], run: _Run) -> str:
    name, args = msg.get("call"), msg.get("args")
    if (not isinstance(name, str) or name.startswith(policy.TOOL_PREFIX)
            or name not in run.allowed):
        return json.dumps({"error": f"{str(name)[:NAME_CAP]} is not callable"})
    if not isinstance(args, dict):
        return json.dumps({"error": "args must be an object"})
    # In a dry run only tools known to be read-only are forwarded; anything
    # else (MUTATING_TOOLS, or an owner-approved callable) is recorded instead.
    if name in WEB_TOOLS and run.read:
        return json.dumps({"error": f"{name} refused after a read in the same run"})
    if run.dry_run and (name in policy.MUTATING_TOOLS or name not in policy.CALLABLE_TOOLS):
        run.intents.append({"tool": name, "args": args})
        return json.dumps({"dry_run": True})
    if name not in WEB_TOOLS:
        run.read = True
    try:
        result = call(name, args)
    except Exception as exc:  # noqa: BLE001 - the program gets an error, not a crash
        return json.dumps({"error": f"{name} failed: {type(exc).__name__}"})
    if not isinstance(result, str):
        result = json.dumps(result, default=str)
    return result


def _kill_container(name: str) -> None:
    try:
        subprocess.run(["docker", "rm", "-f", name], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=DOCKER_RM_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _shutdown(proc: subprocess.Popen, finished: bool, container_name: str | None) -> None:
    for pipe in (proc.stdin, proc.stdout):
        try:
            pipe.close()
        except OSError:
            pass
    if finished:
        try:
            proc.wait(timeout=REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
    killed = False
    if proc.poll() is None:
        proc.kill()
        killed = True
    # The docker CLI may have exited while the container lives on, so remove
    # it whenever the run did not end with a done/error message.
    if container_name and (killed or not finished):
        _kill_container(container_name)
    try:
        proc.wait(timeout=REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass


def run_program(program: str, args: dict, call: Callable[[str, dict], str],
                runner_argv: list[str], *, dry_run: bool, intents: list[dict],
                timeout_s: int = policy.RUN_TIMEOUT_S, max_calls: int = policy.MAX_CALLS,
                max_output: int = policy.MAX_OUTPUT_BYTES,
                allowed: frozenset[str] | None = None,
                container_name: str | None = None,
                on_spawn: Callable[[subprocess.Popen], None] | None = None) -> str:
    """Run `program` under `runner_argv`; return the JSON string for the model.

    `allowed` defaults to policy.CALLABLE_TOOLS; `tuned_*` names are always
    refused. `on_spawn` receives the child process (tests use it).
    """
    run = _Run(policy.CALLABLE_TOOLS if allowed is None else frozenset(allowed),
               dry_run, intents)
    deadline = time.monotonic() + timeout_s
    try:
        proc = subprocess.Popen(list(runner_argv), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return _error(f"could not start the sandbox: {type(exc).__name__}", max_output)
    if on_spawn is not None:
        on_spawn(proc)
    finished = False
    chan = None
    try:
        chan = _Channel(proc, deadline, timeout_s)
        chan.send({"program": program, "args": args})
        n_calls = 0
        while True:
            line = chan.recv()
            if line is None:
                return _error("program exited without a result", max_output)
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                return _error("sandbox sent a malformed message", max_output)
            if "done" in msg:
                finished = True
                out = json.dumps({"result": msg["done"]})
                if len(out.encode()) > max_output:
                    return _error(f"output exceeds {max_output} bytes", max_output)
                return out
            if "error" in msg:
                finished = True
                return _error(str(msg["error"]), max_output)
            if "call" not in msg:
                return _error("sandbox sent a malformed message", max_output)
            n_calls += 1
            if n_calls > max_calls:
                return _error(f"tool call limit ({max_calls}) exceeded", max_output)
            chan.send({"result": _dispatch(msg, call, run)})
    except _Abort as exc:
        return _error(str(exc), max_output)
    finally:
        if chan is not None:
            chan.close()
        _shutdown(proc, finished, container_name)
