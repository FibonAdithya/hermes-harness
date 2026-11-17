"""deploy target chain: exactly the approved main commit, setup + check, rollback on failure, then restart chaind."""
import json
import os
import subprocess
from pathlib import Path

VERB = Path(__file__).resolve().parents[1] / "verbs" / "deploy"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(up, msg="c", setup_rc=0, check_rc=0):
    (up / "Makefile").write_text(
        f"setup:\n\t@echo setup {msg} >> \"$$HOME/make.log\"; exit {setup_rc}\n"
        f"check:\n\t@echo check {msg} >> \"$$HOME/make.log\"; exit {check_rc}\n"
    )
    git(up, "add", "Makefile")
    git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    git(up, "push", "-q", "origin", "HEAD:main")
    return git(up, "rev-parse", "HEAD")


def make_repos(tmp_path):
    origin, up, home = tmp_path / "origin.git", tmp_path / "up", tmp_path / "home"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(up)], check=True, capture_output=True)
    git(up, "checkout", "-q", "-b", "main")
    first = commit(up, msg="first")
    box = home / "chain"
    subprocess.run(["git", "clone", "-q", str(origin), str(box)], check=True)
    stamp = home / ".local" / "share" / "chain-deployed.sha"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(first)
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "systemctl").write_text(
        "#!/bin/sh\necho \"$*\" >> \"$HOME/systemctl.log\"\n[ -n \"$SYSTEMCTL_FAIL\" ] && { echo 'unit not found' >&2; exit 1; }\nexit 0\n")
    (shims / "systemctl").chmod(0o755)
    return up, home, box, first, shims


def run(home, shims, args, **env):
    e = {"HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}", **env}
    p = subprocess.run([str(VERB)], input=json.dumps(args).encode(), env=e, capture_output=True, check=False)
    return p.returncode, json.loads(p.stdout)


def restarts(home):
    log = home / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


def test_deploy_chain_writes_stamp_and_restarts(tmp_path):
    up, home, box, _, shims = make_repos(tmp_path)
    want = commit(up, msg="second")
    rc, out = run(home, shims, {"sha": want, "target": "chain"})
    assert (rc, out) == (0, {"deployed": want, "target": "chain"})
    assert git(box, "rev-parse", "HEAD") == want
    assert (home / ".local/share/chain-deployed.sha").read_text() == want
    assert restarts(home) == ["--user restart chaind"]
    log = (home / "make.log").read_text().splitlines()
    assert log == ["setup second", "check second"]


def test_deploy_chain_refuses_non_main_sha(tmp_path):
    up, home, box, first, shims = make_repos(tmp_path)
    approved = commit(up, msg="approved")
    commit(up, msg="later")
    rc, out = run(home, shims, {"sha": approved, "target": "chain"})
    assert rc == 2 and "main is at" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first
    assert restarts(home) == []


def test_deploy_chain_keeps_previous_on_failed_check(tmp_path):
    up, home, box, first, shims = make_repos(tmp_path)
    red = commit(up, check_rc=1, msg="red")
    rc, out = run(home, shims, {"sha": red, "target": "chain"})
    assert rc == 2 and f"make check failed at {red[:12]}" in out["error"] and f"back on {first[:12]}" in out["error"]
    assert git(box, "rev-parse", "HEAD") == first
    assert (home / ".local/share/chain-deployed.sha").read_text() == first
    assert restarts(home) == []


def test_deploy_chain_rolls_back_on_failed_setup(tmp_path):
    up, home, box, first, shims = make_repos(tmp_path)
    bad = commit(up, setup_rc=1, msg="badsetup")
    rc, out = run(home, shims, {"sha": bad, "target": "chain"})
    assert rc == 2 and "make setup failed" in out["error"] and "make check failed" not in out["error"]
    assert git(box, "rev-parse", "HEAD") == first
    assert (home / ".local/share/chain-deployed.sha").read_text() == first
    assert restarts(home) == []


def test_deploy_chain_reports_failed_rollback(tmp_path):
    up, home, box, first, shims = make_repos(tmp_path)
    red = commit(up, check_rc=1, msg="red")
    # make the rollback target unreachable: the stamp names a commit that does not exist
    (home / ".local/share/chain-deployed.sha").write_text("0" * 40)
    rc, out = run(home, shims, {"sha": red, "target": "chain"})
    assert rc == 2 and f"rollback to {'0' * 12} FAILED" in out["error"]
    assert restarts(home) == []


def test_deploy_chain_failed_restart_is_refused_but_stamped(tmp_path):
    up, home, box, _, shims = make_repos(tmp_path)
    want = commit(up, msg="second")
    rc, out = run(home, shims, {"sha": want, "target": "chain"}, SYSTEMCTL_FAIL="1")
    assert rc == 2 and "unit not found" in out["error"] and "not restarted" in out["error"]
    assert (home / ".local/share/chain-deployed.sha").read_text() == want


def test_deploy_chain_does_not_check_fleet_nights(tmp_path):
    up, home, box, _, shims = make_repos(tmp_path)
    want = commit(up, msg="second")
    d = home / "nights" / "fleet-20260926-2300-ab12"
    d.mkdir(parents=True)
    (d / "status").write_text("running\n")
    rc, out = run(home, shims, {"sha": want, "target": "chain"})
    assert rc == 0 and out["target"] == "chain"
