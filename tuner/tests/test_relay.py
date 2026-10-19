"""Relay between a composed-tool program (in the sandbox) and the host's tools."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "hermes-tuner"
STUB = PLUGIN_DIR / "stub.py"
RUNNER = [sys.executable, "-u", str(STUB)]

SEARCH = "mcp__google__search_gmail_messages"
GET = "mcp__google__get_gmail_message_content"
LABELS = "mcp__google__modify_gmail_message_labels"


def load_plugin():
    name = "hermes_tuner_plugin_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def relay():
    load_plugin()
    return importlib.import_module("hermes_tuner_plugin_under_test.relay")


class FakeCall:
    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}

    def __call__(self, name, args):
        self.calls.append((name, args))
        return self.results.get(name, json.dumps({"ok": name}))


def run(program, call, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("intents", [])
    return json.loads(relay().run_program(program, {"q": "x"}, call, RUNNER, **kw))


def test_program_calls_relay_and_returns_value():
    program = f"""
def main(args, call):
    found = call({SEARCH!r}, {{"query": args["q"]}})
    a = call({GET!r}, {{"message_id": "m1"}})
    b = call({GET!r}, {{"message_id": "m2"}})
    print("noise on the program's stdout")
    return {{"found": found, "bodies": [a, b], "raw": call({GET!r}, {{"message_id": "m3"}})}}
"""
    call = FakeCall({SEARCH: json.dumps({"ids": ["m1", "m2"]})})
    call.results[GET] = json.dumps({"body": "b"})
    out = run(program, call)
    assert [n for n, _ in call.calls] == [SEARCH, GET, GET, GET]
    assert call.calls[0][1] == {"query": "x"}
    assert out == {"result": {"found": {"ids": ["m1", "m2"]},
                              "bodies": [{"body": "b"}, {"body": "b"}],
                              "raw": {"body": "b"}}}


def test_raw_string_result_returned_unparsed():
    program = f"def main(args, call):\n    return call({SEARCH!r}, {{}})\n"
    out = run(program, FakeCall({SEARCH: "plain text, not json"}))
    assert out == {"result": "plain text, not json"}


def test_call_outside_allowlist_refused():
    program = "def main(args, call):\n    return call('terminal', {'command': 'id'})\n"
    call = FakeCall()
    out = run(program, call)
    assert "not callable" in json.dumps(out)
    assert out == {"result": {"error": "terminal is not callable"}}
    assert call.calls == []


def test_explicit_allowed_set_narrows_and_never_allows_tuned():
    program = (f"def main(args, call):\n"
               f"    return [call({SEARCH!r}, {{}}), call('tuned_other', {{}})]\n")
    call = FakeCall()
    out = run(program, call, allowed=frozenset({GET, "tuned_other"}))
    assert out == {"result": [{"error": f"{SEARCH} is not callable"},
                              {"error": "tuned_other is not callable"}]}
    assert call.calls == []


def test_dry_run_records_mutations_without_calling():
    largs = {"message_id": "m1", "add_label_ids": ["L1"]}
    program = (f"def main(args, call):\n"
               f"    s = call({SEARCH!r}, {{}})\n"
               f"    r = call({LABELS!r}, {largs!r})\n"
               f"    return r\n")
    call = FakeCall()
    intents: list[dict] = []
    out = run(program, call, dry_run=True, intents=intents)
    assert intents == [{"tool": LABELS, "args": largs}]
    assert [n for n, _ in call.calls] == [SEARCH]
    assert out == {"result": {"dry_run": True}}


def test_mutation_forwarded_when_not_dry_run():
    program = f"def main(args, call):\n    return call({LABELS!r}, {{'message_id': 'm1'}})\n"
    call = FakeCall()
    intents: list[dict] = []
    run(program, call, dry_run=False, intents=intents)
    assert [n for n, _ in call.calls] == [LABELS]
    assert intents == []


def test_timeout_kills_runner():
    procs = []
    program = "def main(args, call):\n    while True:\n        pass\n"
    t0 = time.monotonic()
    out = run(program, FakeCall(), timeout_s=2, on_spawn=procs.append)
    elapsed = time.monotonic() - t0
    assert elapsed < 5
    assert "error" in out and "time" in out["error"]
    assert len(procs) == 1 and procs[0].poll() is not None


def test_timeout_holds_when_program_never_reads_result():
    # The program issues a call, then never reads the result: the host's write
    # of a large result must not block past the deadline.
    procs = []
    program = (f"import time\n"
               f"def main(args, call):\n"
               f"    import sys, json\n"
               f"    sys.__stdout__.write(json.dumps({{'call': {SEARCH!r}, 'args': {{}}}}) + '\\n')\n"
               f"    sys.__stdout__.flush()\n"
               f"    time.sleep(60)\n")
    big = json.dumps({"x": "y" * 2_000_000})
    t0 = time.monotonic()
    out = run(program, FakeCall({SEARCH: big}), timeout_s=2, on_spawn=procs.append)
    assert time.monotonic() - t0 < 5
    assert "error" in out
    assert procs[0].poll() is not None


def test_call_limit():
    program = (f"def main(args, call):\n"
               f"    for i in range(51):\n"
               f"        call({SEARCH!r}, {{'i': i}})\n"
               f"    return 'finished'\n")
    call = FakeCall()
    out = run(program, call, max_calls=50)
    assert "error" in out and "50" in out["error"] and "limit" in out["error"]
    assert len(call.calls) == 50


def test_call_limit_allows_exactly_max():
    program = (f"def main(args, call):\n"
               f"    for i in range(50):\n"
               f"        call({SEARCH!r}, {{'i': i}})\n"
               f"    return 'finished'\n")
    call = FakeCall()
    assert run(program, call, max_calls=50) == {"result": "finished"}
    assert len(call.calls) == 50


def test_output_cap():
    program = "def main(args, call):\n    return 'a' * 60000\n"
    raw = relay().run_program(program, {}, FakeCall(), RUNNER, dry_run=False, intents=[],
                              max_output=50_000)
    assert len(raw.encode()) <= 50_000
    out = json.loads(raw)
    assert "error" in out and "result" not in out


def test_output_just_under_cap_returned():
    program = "def main(args, call):\n    return 'a' * 49000\n"
    out = run(program, FakeCall(), max_output=50_000)
    assert out == {"result": "a" * 49000}


def test_program_exception_reported():
    program = "def main(args, call):\n    raise ValueError('bad input')\n"
    out = run(program, FakeCall())
    assert out == {"error": "ValueError: bad input"}


def test_unserialisable_result_reported():
    program = "def main(args, call):\n    return object()\n"
    out = run(program, FakeCall())
    assert "error" in out and "JSON" in out["error"]


def test_production_argv_shape():
    r = relay()
    from hermes_tuner import policy
    argv = r.production_argv("tuned-abc")
    assert argv[:len(policy.SANDBOX_ARGV)] == list(policy.SANDBOX_ARGV)
    rest = argv[len(policy.SANDBOX_ARGV):]
    assert rest == ["--name", "tuned-abc", policy.SANDBOX_IMAGE, "python3", "-u", "-c",
                    r.STUB_SOURCE]
    assert "--network=none" in argv
    assert r.STUB_SOURCE == STUB.read_text()


def test_web_refused_after_read_in_same_run():
    program = (f"def main(args, call):\n"
               f"    call({GET!r}, {{'message_id': 'm1'}})\n"
               f"    return [call('web_search', {{'query': 'q'}}), call('web_extract', {{'urls': ['u']}})]\n")
    call = FakeCall()
    out = run(program, call)
    assert out == {"result": [{"error": "web_search refused after a read in the same run"},
                              {"error": "web_extract refused after a read in the same run"}]}
    assert [n for n, _ in call.calls] == [GET]


def test_web_then_read_allowed():
    program = (f"def main(args, call):\n"
               f"    a = call('web_search', {{'query': 'q'}})\n"
               f"    b = call('web_extract', {{'urls': ['u']}})\n"
               f"    return [a, b, call({GET!r}, {{'message_id': 'm1'}})]\n")
    call = FakeCall()
    out = run(program, call)
    assert [n for n, _ in call.calls] == ["web_search", "web_extract", GET]
    assert out == {"result": [{"ok": "web_search"}, {"ok": "web_extract"}, {"ok": GET}]}


def test_container_removed_when_run_does_not_finish(monkeypatch):
    r = relay()
    removed = []
    monkeypatch.setattr(r, "_kill_container", removed.append)
    program = "def main(args, call):\n    while True:\n        pass\n"
    out = run(program, FakeCall(), timeout_s=1, container_name="tuned-test")
    assert "error" in out
    assert removed == ["tuned-test"]
    removed.clear()
    out = run("def main(args, call):\n    return 1\n", FakeCall(), container_name="tuned-test")
    assert out == {"result": 1}
    assert removed == []
    out = run("import sys\ndef main(args, call):\n    sys.__stdout__.close()\n    return 1\n",
              FakeCall(), container_name="tuned-test")
    assert "error" in out
    assert removed == ["tuned-test"]


def test_sandbox_argv_limits():
    from hermes_tuner import policy
    argv = list(policy.SANDBOX_ARGV)
    for flag, value in (("--memory", "512m"), ("--pids-limit", "64"), ("--cpus", "1"),
                        ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                        ("--user", "65534:65534")):
        assert argv[argv.index(flag) + 1] == value, flag
    assert "--network=none" in argv and "--read-only" in argv and "--rm" in argv
