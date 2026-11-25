"""Tests for droplet/todo-nightly.sh, using fake docker and python3 executables.

The fake docker runs the `sh -c` scripts for real, with /wiki replaced by the host
directory standing in for the sandbox checkout, so the read and the guarded delete are
both exercised.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "droplet" / "todo-nightly.sh"
SUGGESTIONS = '{"title": "x"}\n'

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
while [ "$#" -gt 0 ]; do
  a=$1; shift
  [ "$a" = hermes-sandbox:latest ] && break
done
if [ "$1" = sh ] && [ "$2" = -c ]; then
  script=$(printf '%s' "$3" | sed "s#/wiki#$SBX_WIKI#g")
  case "$script" in
    *"cat "*) [ "${FAKE_READ_RC:-0}" = 0 ] || exit "$FAKE_READ_RC" ;;
  esac
  shift 3
  exec sh -c "$script" "$@"
fi
exit 99
"""
FAKE_PYTHON = """#!/bin/sh
echo "python3 $*" >> "$FAKE_LOG"
prev=
for a in "$@"; do
  if [ "$prev" = --suggestions ]; then
    cat "$a" > "$FAKE_SEEN"
    echo "$a" > "$FAKE_TMP"
  fi
  prev=$a
done
echo "GH_CONFIG_DIR=$GH_CONFIG_DIR TREE=$HERMES_WIKI_TREE_GH_CONFIG_DIR PYTHONPATH=$PYTHONPATH" > "$FAKE_ENV"
[ -z "$FAKE_MUTATE" ] || printf '%s' "$FAKE_MUTATE" > "$SBX_WIKI/.todo-suggestions.jsonl"
echo '{"ok": true}'
exit "${FAKE_PY_RC:-0}"
"""


@pytest.fixture
def sandbox(tmp_path):
    for name, body in (("docker", FAKE_DOCKER), ("python3", FAKE_PYTHON)):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(0o755)
    (tmp_path / "home").mkdir()
    wiki_token = tmp_path / "home" / ".hermes" / "gh-wiki"
    wiki_token.mkdir(parents=True)
    (wiki_token / "hosts.yml").write_text("github.com:\n    oauth_token: x\n")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / ".todo-suggestions.jsonl").write_text(SUGGESTIONS)
    return tmp_path


def run(sandbox, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(sandbox / "home"),
        "DOCKER": str(sandbox / "docker"),
        "PYTHON3": str(sandbox / "python3"),
        "HARNESS": str(sandbox / "harness"),
        "SBX_WIKI": str(sandbox / "wiki"),
        "FAKE_LOG": str(sandbox / "log"),
        "FAKE_SEEN": str(sandbox / "seen"),
        "FAKE_TMP": str(sandbox / "tmpname"),
        "FAKE_ENV": str(sandbox / "envseen"),
    }
    env.update(extra)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    log = (sandbox / "log").read_text().splitlines() if (sandbox / "log").exists() else []
    return r, log


def sug(sandbox):
    return sandbox / "wiki" / ".todo-suggestions.jsonl"


def test_nightly_sh_passes_suggestions_to_python(sandbox):
    r, log = run(sandbox)
    assert r.returncode == 0, r.stderr
    py = [l for l in log if l.startswith("python3 ")]
    assert len(py) == 1
    assert "-m hermes_wiki todo nightly" in py[0]
    assert "--suggestions " in py[0]
    assert (sandbox / "seen").read_text() == SUGGESTIONS
    assert '{"ok": true}' in r.stdout
    assert "--config %s/home/.config/hermes-wiki/config.json" % sandbox in py[0]
    # catches: passing the wrong file, an empty temp file, or swallowing the summary


def test_nightly_sh_env_and_tempfile_cleanup(sandbox):
    r, _ = run(sandbox)
    assert r.returncode == 0, r.stderr
    seen = (sandbox / "envseen").read_text().strip()
    assert seen == (f"GH_CONFIG_DIR={sandbox}/home/.hermes/gh-config "
                    f"TREE={sandbox}/home/.hermes/gh-wiki PYTHONPATH={sandbox}/harness/wiki")
    assert not Path((sandbox / "tmpname").read_text().strip()).exists()
    # catches: missing gh config, wiki-write config or PYTHONPATH exports, the write token
    # replacing the read token, and a leaked temp file
    # (a failing run also cleans up, via the EXIT trap)


def test_nightly_sh_deletes_after_success_when_unchanged(sandbox):
    r, log = run(sandbox)
    assert r.returncode == 0, r.stderr
    assert not sug(sandbox).exists()
    rms = [l for l in log if "rm -f" in l]
    assert len(rms) == 1
    assert f"-v {sandbox}/wiki:/wiki " in rms[0] and ":ro" not in rms[0]
    assert log.index(rms[0]) > max(i for i, l in enumerate(log) if l.startswith("python3 "))
    # catches: never deleting (suggestions re-applied nightly), or deleting before python ran


def test_nightly_sh_keeps_file_changed_since_read(sandbox):
    r, _ = run(sandbox, FAKE_MUTATE='{"title": "new"}\n')
    assert r.returncode == 0, r.stderr
    assert sug(sandbox).read_text() == '{"title": "new"}\n'
    assert "changed since read" in r.stderr
    # catches: deleting suggestions the curator wrote after the read (no hash compare)


def test_nightly_sh_missing_file_gives_empty_suggestions(sandbox):
    sug(sandbox).unlink()
    r, _ = run(sandbox)
    assert r.returncode == 0, r.stderr
    assert (sandbox / "seen").read_text() == ""
    assert not sug(sandbox).exists()
    # catches: a missing file failing the read or the delete


def test_nightly_sh_keeps_suggestions_on_failure(sandbox):
    r, log = run(sandbox, FAKE_PY_RC="1")
    assert r.returncode != 0
    assert sug(sandbox).read_text() == SUGGESTIONS
    # catches: deleting suggestions after a failed nightly run, losing them


def test_nightly_sh_docker_read_failure_aborts(sandbox):
    r, log = run(sandbox, FAKE_READ_RC="125")
    assert r.returncode != 0
    assert not [l for l in log if l.startswith("python3 ")]
    assert len([l for l in log if l.startswith("docker ")]) == 1
    assert sug(sandbox).read_text() == SUGGESTIONS
    # catches: `|| true` masking a failed read, then deleting unread suggestions


def test_nightly_sh_read_is_readonly_and_networkless(sandbox):
    r, log = run(sandbox)
    read = [l for l in log if l.startswith("docker ") and "rm -f" not in l]
    assert len(read) == 1
    assert "--network=none" in read[0] and ":/wiki:ro" in read[0]
    assert "hermes-sandbox:latest" in read[0]
    # catches: mounting the checkout writable for the read, or allowing network


@pytest.mark.parametrize("hosts", [None, "github.com:\n    user: x\n"])
def test_nightly_sh_missing_wiki_token_aborts_before_anything(sandbox, hosts):
    f = sandbox / "home" / ".hermes" / "gh-wiki" / "hosts.yml"
    if hosts is None:
        f.unlink()
    else:
        f.write_text(hosts)
    r, log = run(sandbox)
    assert r.returncode != 0
    assert "FATAL" in r.stderr and "gh-wiki" in r.stderr
    assert log == []
    assert sug(sandbox).read_text() == SUGGESTIONS
    # catches: running with no wiki write credential (every close fails at the write), or a
    # token stored in a keyring the job cannot read (hosts.yml without oauth_token)
