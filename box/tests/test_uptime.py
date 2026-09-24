import json
import subprocess
from pathlib import Path

VERBS = Path(__file__).resolve().parents[1] / "verbs"


def _verb(name, payload, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run([str(VERBS / name)], input=json.dumps(payload).encode(), capture_output=True, env=env)


def test_uptime_verb(tmp_path):
    p = _verb("uptime", {}, tmp_path)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["uptime"].startswith("up")
