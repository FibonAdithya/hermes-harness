"""The deploy verb: install exactly the approved commit, and only while it is master."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

VERB = Path(__file__).resolve().parents[1] / "verbs" / "deploy"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, install_rc=0, msg="c"):
    inst = repo / "box" / "install.sh"
    inst.parent.mkdir(parents=True, exist_ok=True)
    inst.write_text(f"#!/bin/sh\ngit -C \"$(dirname \"$0\")\" rev-parse HEAD > \"$HOME/installed\"\n"
                    f"[ {install_rc} -eq 0 ] || echo 'docker build of hermes-exec failed' >&2\nexit {install_rc}\n")
    inst.chmod(inst.stat().st_mode | stat.S_IEXEC)
    git(repo, "add", "box/install.sh")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)
    git(repo, "push", "-q", "origin", "HEAD:master")
    return git(repo, "rev-parse", "HEAD")


def setup(tmp_path):
    """origin (bare), an upstream working copy that pushes to it, and the box's clone at ~/TIG/hermes-harness."""
    origin, up, home = tmp_path / "origin.git", tmp_path / "up", tmp_path / "home"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(up)], check=True, capture_output=True)
    git(up, "checkout", "-q", "-b", "master")
    first = commit(up, msg="first")
    box = home / "TIG" / "hermes-harness"
    subprocess.run(["git", "clone", "-q", str(origin), str(box)], check=True)
    return up, home, box, first


def run(home, args, path=None):
    env = {"HOME": str(home), "PATH": path or os.environ["PATH"]}
    proc = subprocess.run([str(VERB)], input=json.dumps(args).encode(), env=env, capture_output=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


def test_deploys_the_approved_commit(tmp_path):
    up, home, box, _ = setup(tmp_path)
    want = commit(up, msg="second")
    rc, out = run(home, {"sha": want})
    assert (rc, out) == (0, {"deployed": want})
    assert git(box, "rev-parse", "HEAD") == want
    assert (home / "installed").read_text().strip() == want
    assert (home / ".local/share/harness-deployed.sha").read_text() == want


def test_refuses_when_master_has_moved_past_the_approved_commit(tmp_path):
    up, home, box, first = setup(tmp_path)
    approved = commit(up, msg="approved")
    moved = commit(up, msg="merged after approval")
    rc, out = run(home, {"sha": approved})
    assert rc == 2
    assert out["error"] == f"master is at {moved[:12]}, not the approved {approved[:12]}; request a new deploy"
    assert git(box, "rev-parse", "HEAD") == first
    assert not (home / "installed").exists()


def test_a_merge_after_the_master_check_is_not_deployed(tmp_path):
    """Master moves between the verb's check and its reset: the approved commit is still what lands."""
    up, home, box, _ = setup(tmp_path)
    approved = commit(up, msg="approved")
    later = up / "later"
    later.write_text("x")
    git(up, "add", "later")
    git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "merged mid-deploy")
    real = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" reset "*) "{real}" -C "{up}" push -q origin HEAD:master && "{real}" -C "{box}" fetch -q origin;; esac\n'
        f'exec "{real}" "$@"\n')
    (shim / "git").chmod(0o755)
    rc, out = run(home, {"sha": approved}, path=f"{shim}:{os.environ['PATH']}")
    assert git(box, "rev-parse", "origin/master") != approved  # the shim did move master
    assert (rc, out) == (0, {"deployed": approved})
    assert git(box, "rev-parse", "HEAD") == approved


def test_refuses_anything_but_a_full_sha(tmp_path):
    _, home, box, first = setup(tmp_path)
    for args in ({}, {"sha": first[:12]}, {"sha": "master"}, {"sha": first.upper()}, {"sha": ["x"]}):
        rc, out = run(home, args)
        assert rc == 2 and out["error"] == "deploy needs the approved 40-character commit sha", args
    assert not (home / "installed").exists()


def test_install_failure_is_a_refusal_and_leaves_no_stamp(tmp_path):
    up, home, _, _ = setup(tmp_path)
    want = commit(up, install_rc=1, msg="broken install")
    rc, out = run(home, {"sha": want})
    assert rc == 2
    assert out["error"] == "install.sh failed at " + want[:12] + ": docker build of hermes-exec failed"
    assert not (home / ".local/share/harness-deployed.sha").exists()
