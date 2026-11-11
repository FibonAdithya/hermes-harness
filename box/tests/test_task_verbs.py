"""The four ledger verbs, end to end through a gh shim."""
import json
import os
import subprocess
import sys
from pathlib import Path

BOX = Path(__file__).resolve().parents[1]
SHIM = r'''#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, os.environ["TESTS_DIR"])
from fakegh import FakeGh
state = os.environ["GH_STATE"]
gh = FakeGh()
if os.path.exists(state):
    s = json.load(open(state)); gh.labels = set(s["labels"]); gh.issues = {int(k): v for k, v in s["issues"].items()}
stdin = sys.stdin.read() if "--body-file" in sys.argv else None
rc, out, err = gh(sys.argv[1:], stdin)
json.dump({"labels": sorted(gh.labels), "issues": gh.issues}, open(state, "w"))
sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)
'''
UV_SHIM = '#!/bin/sh\necho \'["daemon", "docs"]\'\n'


SYSTEMD_RUN_SHIM = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemd-run.log"\n'


def home_with_repo(tmp_path, name="fleet-fixture", slug="FibonAdithya/fleet-fixture", role_prompts=True):
    """A box home with one fleet target. role_prompts=False is a repository like
    hermes-harness: a fleet.toml naming a roles directory that holds no prompt."""
    home = tmp_path / "home"
    d = home / "TIG" / name
    d.mkdir(parents=True)
    (d / "fleet.toml").write_text(f'[repo]\nslug = "{slug}"\n[policy]\nroles = ".agents/roles"\n')
    if role_prompts:
        (d / ".agents" / "roles").mkdir(parents=True)
        (d / ".agents" / "roles" / "implementer.md").write_text("prompt\n")
    (home / "TIG" / "notfleet").mkdir()
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "gh").write_text(SHIM)
    (shims / "uv").write_text(UV_SHIM)
    (shims / "systemd-run").write_text(SYSTEMD_RUN_SHIM)
    for s in ("gh", "uv", "systemd-run"):
        (shims / s).chmod(0o755)
    return home, shims


def run(verb, home, shims, args):
    env = {"HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}",
           "GH_STATE": str(home / "gh.json"), "TESTS_DIR": str(BOX / "tests")}
    p = subprocess.run([str(BOX / "verbs" / verb)], input=json.dumps(args).encode(), env=env,
                       capture_output=True, check=False)
    return p.returncode, json.loads(p.stdout)


def test_file_then_list_then_approve(tmp_path):
    home, shims = home_with_repo(tmp_path)
    rc, out = run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    assert rc == 0 and out["number"] == 1 and out["slug"] == "FibonAdithya/fleet-fixture"
    rc, out = run("list_triage", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and out["errors"] == []
    [issue] = out["issues"]
    assert (issue["repo"], issue["slug"], issue["number"]) == ("fleet-fixture", "FibonAdithya/fleet-fixture", 1)
    rc, out = run("approve_task", home, shims, {"repo": "fleet-fixture", "number": 1, "hash": issue["hash"], "mode": "fleet"})
    assert rc == 0 and out["title"] == "t"
    assert run("list_triage", home, shims, {})[1]["issues"] == []


def test_file_task_refuses_a_repo_without_fleet_toml_and_a_bad_area(tmp_path):
    home, shims = home_with_repo(tmp_path)
    rc, out = run("file_task", home, shims, {"repo": "notfleet", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    assert rc == 2 and "no fleet.toml" in out["error"]
    rc, out = run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "eval", "cls": "patch"})
    assert rc == 2 and "area 'eval'" in out["error"]


def test_file_task_always_files_as_hermes(tmp_path):
    """Catches a verb that forwards a caller-supplied source label."""
    home, shims = home_with_repo(tmp_path)
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch",
                                   "source": "source:doctor"})
    issue = run("list_triage", home, shims, {})[1]["issues"][0]
    assert "source:hermes" in issue["labels"] and "source:doctor" not in issue["labels"]


def test_approve_task_refuses_a_stale_hash(tmp_path):
    home, shims = home_with_repo(tmp_path)
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    rc, out = run("approve_task", home, shims, {"repo": "fleet-fixture", "number": 1, "hash": "0" * 64, "mode": "fleet"})
    assert rc == 2 and "changed since it was announced" in out["error"]


def test_close_task(tmp_path):
    home, shims = home_with_repo(tmp_path)
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    rc, out = run("close_task", home, shims, {"repo": "fleet-fixture", "number": 1})
    assert (rc, out) == (0, {"repo": "fleet-fixture", "number": 1, "closed": True})


def test_list_triage_reports_a_failing_repo_without_dropping_the_others(tmp_path):
    home, shims = home_with_repo(tmp_path)
    bad = home / "TIG" / "broken"
    bad.mkdir()
    (bad / "fleet.toml").write_text("[repo]\n")  # no slug
    run("file_task", home, shims, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    rc, out = run("list_triage", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and len(out["issues"]) == 1
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("broken:")


def _no_path_env(home, only_dir):
    """A PATH containing only `only_dir`, with a python3 symlink so the verb's
    own #!/usr/bin/env python3 shebang still resolves, but nothing else -- no
    gh, no uv."""
    only_dir.mkdir()
    (only_dir / "python3").symlink_to(sys.executable)
    return {"HOME": str(home), "PATH": str(only_dir)}


def run_bare(verb, env, args):
    p = subprocess.run([str(BOX / "verbs" / verb)], input=json.dumps(args).encode(), env=env,
                       capture_output=True, check=False)
    return p.returncode, json.loads(p.stdout)


def test_close_task_refuses_cleanly_when_gh_is_missing(tmp_path):
    home, _ = home_with_repo(tmp_path)
    env = _no_path_env(home, tmp_path / "no_gh")
    rc, out = run_bare("close_task", env, {"repo": "fleet-fixture", "number": 1})
    assert rc == 2 and "cannot run gh" in out["error"]


def test_file_task_refuses_cleanly_when_uv_is_missing(tmp_path):
    home, _ = home_with_repo(tmp_path)
    env = _no_path_env(home, tmp_path / "no_uv")
    rc, out = run_bare("file_task", env, {"repo": "fleet-fixture", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    assert rc == 2 and "cannot run uv" in out["error"]


def _labels(home, number):
    return {x["name"] for x in json.load(open(home / "gh.json"))["issues"][str(number)]["labels"]}


def test_task_is_refused_where_fleet_cannot_run_and_solo_still_works(tmp_path):
    home, shims = home_with_repo(tmp_path, name="solo-only", slug="FibonAdithya/solo-only", role_prompts=False)
    rc, out = run("file_task", home, shims, {"repo": "solo-only", "title": "t", "body": "b", "area": "docs", "cls": "patch"})
    assert rc == 0
    [issue] = run("list_triage", home, shims, {})[1]["issues"]
    args = {"repo": "solo-only", "number": 1, "hash": issue["hash"]}
    rc, out = run("approve_task", home, shims, {**args, "mode": "fleet"})
    assert rc == 2 and "solo only" in out["error"]
    assert "fleet:triage" in _labels(home, 1) and "fleet:ready" not in _labels(home, 1)
    rc, out = run("approve_task", home, shims, {**args, "mode": "solo"})
    assert rc == 0 and out["title"] == "t"
    assert {"fleet:human", "fleet:solo"} <= _labels(home, 1) and "fleet:triage" not in _labels(home, 1)


def test_run_fleet_starts_a_night_only_where_fleet_can_run(tmp_path):
    home, shims = home_with_repo(tmp_path, name="solo-only", slug="FibonAdithya/solo-only", role_prompts=False)
    rc, out = run("run_fleet", home, shims, {"repo": "solo-only", "hours": 1})
    assert rc == 2 and "solo only" in out["error"]
    assert not (home / "systemd-run.log").exists() and not (home / "nights").exists()

    home, shims = home_with_repo(tmp_path / "second")
    rc, out = run("run_fleet", home, shims, {"repo": "fleet-fixture", "hours": 1})
    assert rc == 0 and out["id"].startswith("fleet-")
    assert f"--unit=night-{out['id']}" in (home / "systemd-run.log").read_text()
