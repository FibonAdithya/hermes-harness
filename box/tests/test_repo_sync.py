"""droplet/repo-sync.sh (runs in the sandbox container) and droplet/hermes-repo-sync.sh
(the host wrapper), with stub `gh` / `docker` first on PATH and local bare repos as
remotes. Nothing here touches GitHub or Docker."""
import http.server
import os
import subprocess
import threading
from pathlib import Path

DROPLET = Path(__file__).resolve().parents[2] / "droplet"
INNER = DROPLET / "repo-sync.sh"
OUTER = DROPLET / "hermes-repo-sync.sh"

FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1" in
  api)
    if [ -n "${FAKE_GH_FAIL:-}" ]; then echo "HTTP 401: Bad credentials" >&2; exit 1; fi
    cat "$FAKE_GH_LIST" ;;
  auth)
    cat >/dev/null
    if [ "$3" = get ]; then printf 'username=x-access-token\\npassword=from-gh\\n'; fi ;;
esac
"""

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$DOCKER_LOG"
"""


def _git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout


def _remote(remotes, name, files):
    """A bare repo at remotes/FibonAdithya/<name>.git with one commit of `files`."""
    work = remotes / "work" / name
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    for rel, text in files.items():
        (work / rel).write_text(text)
    _git("add", ".", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=work)
    bare = remotes / "FibonAdithya" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--bare", str(work), str(bare))
    return bare


def _setup(tmp_path, names, url_base=None):
    home = tmp_path / "home"
    home.mkdir()
    remotes = tmp_path / "remotes"
    for n in names:
        _remote(remotes, n, {"README.md": f"{n}\n"})
    base = url_base or f"file://{remotes}/"
    # The sandbox's real /root/.gitconfig uses the store helper; keep it here so a
    # test can see whether the sync consults it.
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper = store\n"
        f'[url "{base}"]\n\tinsteadOf = https://github.com/\n'
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH)
    (bindir / "gh").chmod(0o755)
    listing = tmp_path / "listing.txt"
    listing.write_text("".join(f"FibonAdithya/{n}\n" for n in names))
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(home),
        "GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_LIST": str(listing),
        "GIT_TERMINAL_PROMPT": "0",
    }
    return home, remotes, env


def _sync(env, mirrors, **extra):
    return subprocess.run(["bash", str(INNER), str(mirrors)], env={**env, **extra},
                          capture_output=True, text=True)


def test_first_pass_clones_every_listed_repo(tmp_path):
    home, _, env = _setup(tmp_path, ["alpha", "beta"])
    r = _sync(env, home / "repos")
    assert r.returncode == 0, r.stderr
    assert (home / "repos" / "alpha" / "README.md").read_text() == "alpha\n"
    assert (home / "repos" / "beta" / "README.md").read_text() == "beta\n"
    assert "listed 2 repos, 0 failed" in r.stdout


def test_writes_under_a_mirror_are_discarded_on_the_next_pass(tmp_path):
    home, _, env = _setup(tmp_path, ["alpha"])
    assert _sync(env, home / "repos").returncode == 0
    mirror = home / "repos" / "alpha"
    (mirror / "README.md").write_text("edited by the agent\n")
    (mirror / "SCRATCH.md").write_text("mirror-test\n")
    r = _sync(env, home / "repos")
    assert r.returncode == 0, r.stderr
    assert (mirror / "README.md").read_text() == "alpha\n"
    assert not (mirror / "SCRATCH.md").exists()


def test_next_pass_picks_up_new_upstream_commits(tmp_path):
    home, remotes, env = _setup(tmp_path, ["alpha"])
    assert _sync(env, home / "repos").returncode == 0
    work = remotes / "work" / "alpha"
    (work / "NEW.md").write_text("new\n")
    _git("add", ".", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two", cwd=work)
    _git("push", "-q", str(remotes / "FibonAdithya" / "alpha.git"), "main", cwd=work)
    assert _sync(env, home / "repos").returncode == 0
    assert (home / "repos" / "alpha" / "NEW.md").read_text() == "new\n"


def test_listing_failure_is_fatal(tmp_path):
    home, _, env = _setup(tmp_path, ["alpha"])
    r = _sync(env, home / "repos", FAKE_GH_FAIL="1")
    assert r.returncode != 0
    assert "FATAL: repo listing failed" in r.stderr
    assert not (home / "repos" / "alpha").exists()


def test_empty_listing_is_fatal(tmp_path):
    home, _, env = _setup(tmp_path, ["alpha"])
    Path(env["FAKE_GH_LIST"]).write_text("")
    r = _sync(env, home / "repos")
    assert r.returncode != 0
    assert "FATAL: repo listing came back empty" in r.stderr


def test_a_failed_clone_fails_the_run_but_the_rest_still_sync(tmp_path):
    home, _, env = _setup(tmp_path, ["alpha"])
    Path(env["FAKE_GH_LIST"]).write_text("FibonAdithya/missing\nFibonAdithya/alpha\n")
    r = _sync(env, home / "repos")
    assert r.returncode != 0
    assert "FAILED clone FibonAdithya/missing" in r.stderr
    assert (home / "repos" / "alpha" / "README.md").exists()
    assert "listed 2 repos, 1 failed" in r.stdout


def test_a_mirror_dropped_from_the_listing_is_reported_not_deleted(tmp_path):
    home, _, env = _setup(tmp_path, ["alpha", "beta"])
    assert _sync(env, home / "repos").returncode == 0
    Path(env["FAKE_GH_LIST"]).write_text("FibonAdithya/alpha\n")
    r = _sync(env, home / "repos")
    assert r.returncode == 0, r.stderr
    assert "STALE mirror: beta (not in listing)" in r.stdout
    assert (home / "repos" / "beta" / "README.md").exists()


class _AlwaysUnauthorized(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="t"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_clone_credentials_come_from_gh_and_never_touch_the_sandbox_store(tmp_path):
    # The sandbox's /root uses credential.helper=store. If the sync let git consult
    # it, git would hand the stored token to GitHub and, on rejection, erase it from
    # the agent's home (that is how the old dead token vanished on 2026-11-13).
    server = http.server.HTTPServer(("127.0.0.1", 0), _AlwaysUnauthorized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        home, _, env = _setup(tmp_path, ["alpha"], url_base=url)
        stored = f"http://agent:stored-token@127.0.0.1%3a{server.server_port}\n"
        store = home / ".git-credentials"
        store.write_text(stored)
        r = _sync(env, home / "repos")
    finally:
        server.shutdown()
    assert r.returncode != 0
    assert "auth git-credential get" in Path(env["GH_LOG"]).read_text()
    assert store.read_text() == stored


# --- host wrapper ------------------------------------------------------------

def _outer(tmp_path, hosts_yml=None):
    home = tmp_path / "home"
    (home / ".hermes" / "sandboxes" / "docker" / "default" / "home").mkdir(parents=True)
    if hosts_yml is not None:
        gh = home / ".hermes" / "gh-config"
        gh.mkdir()
        (gh / "hosts.yml").write_text(hosts_yml)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home), "DOCKER_LOG": str(log)}
    r = subprocess.run(["bash", str(OUTER)], env=env, capture_output=True, text=True)
    return r, home, log


def test_wrapper_without_a_gh_credential_fails_before_docker(tmp_path):
    r, _, log = _outer(tmp_path)
    assert r.returncode == 1
    assert "FATAL: no gh credential" in r.stderr
    assert not log.exists()


def test_wrapper_rejects_a_keyring_only_login(tmp_path):
    # `gh auth login` without --insecure-storage puts the token in a host keyring the
    # container cannot reach, leaving hosts.yml with a user but no oauth_token.
    r, _, log = _outer(tmp_path, "github.com:\n    user: FibonAdithya\n    git_protocol: https\n")
    assert r.returncode == 1
    assert "--insecure-storage" in r.stderr
    assert not log.exists()


def test_wrapper_runs_the_sync_with_the_token_mounted_outside_root(tmp_path):
    r, home, log = _outer(tmp_path, "github.com:\n    oauth_token: github_pat_x\n    user: FibonAdithya\n")
    assert r.returncode == 0, r.stderr
    args = log.read_text().splitlines()
    sbx = home / ".hermes" / "sandboxes" / "docker" / "default" / "home"
    assert f"{sbx}:/root" in args
    assert f"{home / '.hermes' / 'gh-config'}:/gh:ro" in args
    assert f"{INNER}:/opt/repo-sync.sh:ro" in args
    assert "GH_CONFIG_DIR=/gh" in args
    assert args[-3:] == ["bash", "/opt/repo-sync.sh", "/root/repos"]
