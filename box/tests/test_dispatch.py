import io
import json
import stat
from pathlib import Path

import boxlib


def _verbs(tmp_path: Path) -> Path:
    d = tmp_path / "verbs"
    d.mkdir(exist_ok=True)
    echo = d / "echo_args"
    echo.write_text("#!/usr/bin/env python3\nimport sys,json\nprint(json.dumps({'got': json.load(sys.stdin)}))\n")
    echo.chmod(echo.stat().st_mode | stat.S_IEXEC)
    return d


def _run(verb_string, body, tmp_path, capsys):
    verbs = _verbs(tmp_path)
    rc = boxlib.dispatch(verb_string, io.BytesIO(body), verbs, {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    return rc, capsys.readouterr().out


def test_known_verb_runs_with_stdin(tmp_path, capsys):
    rc, out = _run("echo_args", b'{"x": 1}', tmp_path, capsys)
    assert rc == 0
    assert json.loads(out) == {"got": {"x": 1}}


def test_unknown_verb_is_refused(tmp_path, capsys):
    rc, out = _run("nope", b"{}", tmp_path, capsys)
    assert rc == 2
    assert "unknown verb" in json.loads(out)["error"]


def test_empty_command_is_refused(tmp_path, capsys):
    rc, out = _run("", b"{}", tmp_path, capsys)
    assert rc == 2


def test_shell_like_command_is_refused(tmp_path, capsys):
    for bad in ["bash -c id", "echo_args; id", "echo_args extra", "../echo_args", "echo_args.py", "ECHO_ARGS"]:
        rc, out = _run(bad, b"{}", tmp_path, capsys)
        assert rc == 2, bad
        assert "error" in json.loads(out), bad


def test_oversized_stdin_is_refused(tmp_path, capsys):
    rc, out = _run("echo_args", b"{" + b" " * (boxlib.MAX_STDIN + 10) + b"}", tmp_path, capsys)
    assert rc == 2
    assert "too large" in json.loads(out)["error"]


def test_invalid_json_is_refused(tmp_path, capsys):
    rc, out = _run("echo_args", b"not json", tmp_path, capsys)
    assert rc == 2
    assert "json" in json.loads(out)["error"].lower()


def test_non_executable_file_is_not_a_verb(tmp_path, capsys):
    verbs = _verbs(tmp_path)
    (verbs / "plain").write_text("x")
    rc = boxlib.dispatch("plain", io.BytesIO(b"{}"), verbs, {})
    assert rc == 2
