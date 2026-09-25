"""The deploy verb: start the oneshot harness-pull unit, report failure as a refusal."""
import json
import stat
import subprocess
from pathlib import Path

VERB = Path(__file__).resolve().parents[1] / "verbs" / "deploy"


def _run(tmp_path, rc=0, stdin=b"{}"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ctl = bin_dir / "systemctl"
    ctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'systemctl.argv'}\n"
                   f"[ {rc} -eq 0 ] || echo 'Job for harness-pull.service failed.' >&2\nexit {rc}\n")
    ctl.chmod(ctl.stat().st_mode | stat.S_IEXEC)
    env = {"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    proc = subprocess.run([str(VERB)], input=stdin, env=env, capture_output=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


def test_deploy_starts_harness_pull(tmp_path):
    rc, out = _run(tmp_path)
    assert rc == 0 and out == {"deployed": True}
    assert (tmp_path / "systemctl.argv").read_text().splitlines() == ["--user", "start", "harness-pull.service"]


def test_deploy_ignores_its_arguments(tmp_path):
    for stdin in (b"", b'{"ref": "some-branch"}'):
        rc, out = _run(tmp_path, stdin=stdin)
        assert rc == 0 and out == {"deployed": True}
        assert (tmp_path / "systemctl.argv").read_text().splitlines() == ["--user", "start", "harness-pull.service"]


def test_deploy_refuses_when_the_unit_fails(tmp_path):
    rc, out = _run(tmp_path, rc=1)
    assert rc == 2
    assert out["error"] == "deploy failed: Job for harness-pull.service failed."
