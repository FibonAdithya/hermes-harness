"""deploy target fleet: exactly the approved main commit, never under a running night, never a red one."""
import json
import os
import subprocess
from pathlib import Path

VERB = Path(__file__).resolve().parents[1] / "verbs" / "deploy"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(up, check_rc=0, msg="c"):
    (up / "Makefile").write_text(f"check:\n\t@echo checked {msg}; exit {check_rc}\n")
    git(up, "add", "Makefile")
    git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    git(up, "push", "-q", "origin", "HEAD:main")
    return git(up, "rev-parse", "HEAD")


def setup(tmp_path):
    origin, up, home = tmp_path / "origin.git", tmp_path / "up", tmp_path / "home"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(up)], check=True, capture_output=True)
    git(up, "checkout", "-q", "-b", "main")
    first = commit(up, msg="first")
    box = home / "TIG" / "fleet"
    subprocess.run(["git", "clone", "-q", str(origin), str(box)], check=True)
    stamp = home / ".local" / "share" / "fleet-deployed.sha"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(first)
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "uv").write_text("#!/bin/sh\necho \"uv $*\" >> \"$HOME/uv.log\"\n")
    (shims / "systemctl").write_text("#!/bin/sh\necho \"${SYSTEMCTL_STATE:-inactive}\"\n")
    for s in ("uv", "systemctl"):
        (shims / s).chmod(0o755)
    return up, home, box, first, shims


def run(home, shims, args, **env):
    e = {"HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}", **env}
    p = subprocess.run([str(VERB)], input=json.dumps(args).encode(), env=e, capture_output=True, check=False)
    return p.returncode, json.loads(p.stdout)


def running_night(home, name="fleet-20260926-2300-ab12"):
    d = home / "nights" / name
    d.mkdir(parents=True)
    (d / "status").write_text("running\n")


def test_deploys_a_green_main_commit(tmp_path):
    up, home, box, _, shims = setup(tmp_path)
    want = commit(up, msg="second")
    rc, out = run(home, shims, {"sha": want, "target": "fleet"})
    assert (rc, out) == (0, {"deployed": want, "target": "fleet"})
    assert git(box, "rev-parse", "HEAD") == want
    assert (home / ".local/share/fleet-deployed.sha").read_text() == want
    assert "uv sync --locked --dev" in (home / "uv.log").read_text()


def test_refuses_while_any_fleet_night_runs(tmp_path):
    """Any repo's fleet night: the daemon for fleet-fixture runs from ~/TIG/fleet too."""
    up, home, box, first, shims = setup(tmp_path)
    want = commit(up, msg="second")
    running_night(home)
    rc, out = run(home, shims, {"sha": want, "target": "fleet"}, SYSTEMCTL_STATE="active")
    assert rc == 2 and "fleet-20260926-2300-ab12 is running" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first


def test_refuses_when_main_moved(tmp_path):
    up, home, box, first, shims = setup(tmp_path)
    approved = commit(up, msg="approved")
    commit(up, msg="later")
    rc, out = run(home, shims, {"sha": approved, "target": "fleet"})
    assert rc == 2 and "main is at" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first


def test_a_red_commit_is_rolled_back_to_the_deployed_one(tmp_path):
    up, home, box, first, shims = setup(tmp_path)
    red = commit(up, check_rc=1, msg="red")
    rc, out = run(home, shims, {"sha": red, "target": "fleet"})
    assert rc == 2 and f"make check failed at {red[:12]}" in out["error"] and "checked red" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first
    assert (home / ".local/share/fleet-deployed.sha").read_text() == first


def test_unknown_target_refused(tmp_path):
    _, home, _, first, shims = setup(tmp_path)
    rc, out = run(home, shims, {"sha": first, "target": "droplet"})
    assert rc == 2 and out["error"] == "deploy target must be harness or fleet"
