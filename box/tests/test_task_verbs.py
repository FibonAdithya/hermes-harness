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
failing = os.path.join(os.environ["HOME"], "gh.fail")
if os.path.exists(failing) and open(failing).read().strip() in sys.argv:
    sys.stderr.write("gh: simulated failure")
    sys.exit(1)
gh = FakeGh()
if os.path.exists(state):
    s = json.load(open(state)); gh.labels = set(s["labels"]); gh.issues = {int(k): v for k, v in s["issues"].items()}
stdin = sys.stdin.read() if "--body-file" in sys.argv else None
rc, out, err = gh(sys.argv[1:], stdin)
json.dump({"labels": sorted(gh.labels), "issues": gh.issues}, open(state, "w"))
sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)
'''
UV_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
if "escalation" not in " ".join(sys.argv[1:]):
    print('["daemon", "docs"]')
    sys.exit(0)
home = os.environ["HOME"]
if os.path.exists(home + "/escalations.fail"):
    sys.stderr.write("fleet checkout broken")
    sys.exit(1)
wanted = json.load(sys.stdin)
f = home + "/escalations.json"
scripted = json.load(open(f)) if os.path.exists(f) else {}
print(json.dumps({k: scripted.get(k) for k in wanted}))
'''


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


# --- fleet:human verbs ------------------------------------------------------------------

ESC = {"index": 2, "reason": "needs a decision", "text": "Which port should the daemon use?"}


def _add_human_issue(home, number=1, labels=("fleet:human", "area:docs"), esc=ESC, title="Pick a port"):
    """Puts a fleet:human issue in the gh shim's state and scripts its escalation."""
    f = home / "gh.json"
    st = json.load(open(f)) if f.exists() else {"labels": [], "issues": {}}
    st["labels"] = sorted(set(st["labels"]) | {"fleet:human", "fleet:solo", "area:docs"} | {x for x in labels})
    st["issues"][str(number)] = {"number": number, "title": title, "body": "b", "state": "OPEN",
                                 "labels": [{"name": x} for x in labels],
                                 "comments": [{"body": "escalation comment"}], "assignees": []}
    json.dump(st, open(f, "w"))
    if esc is not None:
        e = home / "escalations.json"
        cur = json.load(open(e)) if e.exists() else {}
        cur[str(number)] = esc
        json.dump(cur, open(e, "w"))


def _issue(home, number):
    return json.load(open(home / "gh.json"))["issues"][str(number)]


def test_human_verbs_are_executable():
    for v in ("list_human", "answer_human", "take_human"):
        p = BOX / "verbs" / v
        assert os.access(p, os.X_OK), v
        assert p.read_text().startswith("#!/usr/bin/env python3")


def test_list_human_verb_shape(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    rc, out = run("list_human", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and out["errors"] == []
    [row] = out["issues"]
    assert set(row) == {"repo", "slug", "number", "title", "labels", "escalation", "note", "hash"}
    assert row["repo"] == "fleet-fixture" and row["slug"] == "FibonAdithya/fleet-fixture"
    assert row["escalation"] == ESC and row["note"] is None and row["number"] == 1


def test_list_human_reports_repo_errors(tmp_path):
    home, shims = home_with_repo(tmp_path)
    bad = home / "TIG" / "broken"
    bad.mkdir()
    (bad / "fleet.toml").write_text("[repo]\n")  # no slug
    _add_human_issue(home)
    rc, out = run("list_human", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and len(out["issues"]) == 1
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("broken:")


def test_list_human_lists_issue_when_uv_fails(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    (home / "escalations.fail").write_text("")
    rc, out = run("list_human", home, shims, {})
    assert rc == 0
    [row] = out["issues"]
    assert row["escalation"] is None and "escalation text unavailable" in row["note"]


def test_answer_human_verb(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    [row] = run("list_human", home, shims, {})[1]["issues"]
    rc, out = run("answer_human", home, shims,
                  {"repo": "fleet-fixture", "number": 1, "hash": row["hash"], "text": "  use 8080  "})
    assert rc == 0 and out == {"repo": "fleet-fixture", "slug": "FibonAdithya/fleet-fixture", "number": 1, "answers": 1}
    f = home / ".local/state/fleet/answers/FibonAdithya__fleet-fixture/1.json"
    [entry] = json.load(open(f))
    assert entry["text"] == "use 8080" and isinstance(entry["at"], float) and entry["at"] > 0
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert _labels(home, 1) == {"fleet:answered-1", "fleet:ready", "area:docs"}
    assert any("use 8080" in c["body"] for c in _issue(home, 1)["comments"])


def test_answer_human_refuses_stale_hash(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    rc, out = run("answer_human", home, shims, {"repo": "fleet-fixture", "number": 1, "hash": "0" * 64, "text": "x"})
    assert rc == 2 and "error" in out
    assert not (home / ".local/state/fleet/answers").exists()
    assert "fleet:human" in _labels(home, 1)


BAD_NUMBERS = ['"abc"', "0", "-1", "1e999", "true", "1.5", "null"]


def _run_raw(verb, home, shims, raw):
    env = {"HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}",
           "GH_STATE": str(home / "gh.json"), "TESTS_DIR": str(BOX / "tests")}
    p = subprocess.run([str(BOX / "verbs" / verb)], input=raw.encode(), env=env, capture_output=True, check=False)
    return p


def test_human_verbs_refuse_bad_payloads(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    before = (home / "gh.json").read_text()
    h = '"' + run("list_human", home, shims, {})[1]["issues"][0]["hash"] + '"'  # valid, so only the number is wrong
    cases = []
    for n in BAD_NUMBERS:
        cases.append(("answer_human", f'{{"repo": "fleet-fixture", "number": {n}, "hash": {h}, "text": "x"}}'))
        cases.append(("take_human", f'{{"repo": "fleet-fixture", "number": {n}, "hash": {h}}}'))
    cases += [("answer_human", f'{{"repo": "fleet-fixture", "number": 1, "hash": 5, "text": "x"}}'),
              ("answer_human", f'{{"repo": "fleet-fixture", "number": 1, "hash": {h}, "text": 5}}'),
              ("answer_human", f'{{"repo": "fleet-fixture", "number": 1, "hash": {h}}}'),
              ("take_human", f'{{"repo": "fleet-fixture", "number": 1, "hash": 5}}'),
              ("take_human", f'{{"repo": "fleet-fixture", "number": 1}}')]
    for verb, raw in cases:
        p = _run_raw(verb, home, shims, raw)
        assert p.returncode == 2, (verb, raw, p.stderr[-200:])
        assert "error" in json.loads(p.stdout), (verb, raw)
        assert (home / "gh.json").read_text() == before, (verb, raw)
        assert not (home / ".local/state/fleet/answers").exists(), (verb, raw)


def test_take_human_verb(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    [row] = run("list_human", home, shims, {})[1]["issues"]
    rc, out = run("take_human", home, shims, {"repo": "fleet-fixture", "number": 1, "hash": row["hash"]})
    assert rc == 0 and out == {"repo": "fleet-fixture", "slug": "FibonAdithya/fleet-fixture",
                               "number": 1, "title": "Pick a port", "assignee": "fibon-box"}
    assert [a["login"] for a in _issue(home, 1)["assignees"]] == ["fibon-box"]
    assert "fleet:human" in _labels(home, 1)
    assert run("list_human", home, shims, {})[1]["issues"] == []


def test_list_human_reports_a_repo_whose_gh_calls_fail(tmp_path):
    home, shims = home_with_repo(tmp_path)
    _add_human_issue(home)
    bad = home / "TIG" / "ghfails"
    bad.mkdir()
    (bad / "fleet.toml").write_text('[repo]\nslug = "FibonAdithya/ghfails"\n')
    (home / "gh.fail").write_text("FibonAdithya/ghfails")
    rc, out = run("list_human", home, shims, {})
    assert rc == 0 and out["repos_ok"] == ["fleet-fixture"] and len(out["issues"]) == 1
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("ghfails:")
