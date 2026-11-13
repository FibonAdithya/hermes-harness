import io
import json
import subprocess
from pathlib import Path

import boxlib

VERBS = Path(__file__).resolve().parents[1] / "verbs"
A, B = "a" * 40, "b" * 40


def _verb(name, payload, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run([str(VERBS / name)], input=json.dumps(payload).encode(), capture_output=True, env=env)


def test_missing_stamps_give_nulls(tmp_path):
    p = _verb("deployed", {}, tmp_path)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == {"harness": None, "fleet": None}


def test_valid_stamps_are_returned(tmp_path):
    share = tmp_path / ".local/share"
    share.mkdir(parents=True)
    (share / "harness-deployed.sha").write_text(A)  # harness-pull/deploy write no newline
    (share / "fleet-deployed.sha").write_text(B + "\n")  # a newline must not make it invalid
    assert json.loads(_verb("deployed", {}, tmp_path).stdout) == {"harness": A, "fleet": B}


def test_non_hex_stamp_gives_null(tmp_path):
    share = tmp_path / ".local/share"
    share.mkdir(parents=True)
    (share / "harness-deployed.sha").write_text("195a042")  # short
    (share / "fleet-deployed.sha").write_text("Z" * 40)  # not hex
    assert json.loads(_verb("deployed", {}, tmp_path).stdout) == {"harness": None, "fleet": None}
    (share / "harness-deployed.sha").write_text("c" * 41)  # too long
    assert json.loads(_verb("deployed", {}, tmp_path).stdout)["harness"] is None


def test_deployed_is_reachable_through_dispatch(tmp_path, capsys):
    rc = boxlib.dispatch("deployed", io.BytesIO(b"{}"), VERBS, {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"harness": None, "fleet": None}
