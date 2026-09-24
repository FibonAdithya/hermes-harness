# The TIG box: fleet, Talos, and self-extension — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision the rebuilt `tig-server` as a compute host that Hermes reaches only through the broker, with fleet and Talos as gated tools, timer-started nights, a read-only wiki mirror, and a pull-request loop through which the box grows its own verb set.

**Architecture:** The droplet keeps Hermes and gains the broker from the 08-05 plan. The box runs a forced-command dispatcher whose verbs are files in `box/verbs/`; long verbs detach as transient systemd units under `~/nights/<id>/`. The broker's new tools send a verb name over SSH with JSON on stdin. A pull timer on each host makes a merge to `master` a deploy, and branch protection on a public `hermes-harness` is the gate.

**Tech Stack:** Python 3.11+ stdlib for the dispatcher and verbs (pytest for tests); the existing `hermes_broker` package (mcp, httpx, pytest); bash for provisioning and unit wrappers; systemd user units; Docker; uv; herdr 0.7.5; the `claude`, `codex`, and `gh` CLIs; fleet and Talos as installed from their repos.

**Spec:** `docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md` (this repo). Read it first; every task below cites the section it implements.

## Global Constraints

- Nothing on the box runs as root except the one-time `provision.sh`. Everything else is user `adi` (spec §3).
- The broker's SSH key on the box lands on `command="/home/adi/.local/bin/dispatch"` with `no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding`. No other key for the broker (spec §4).
- A verb is a file in `box/verbs/`. The dispatcher lists that directory at call time and refuses anything else (spec §4).
- Every long verb runs under `systemd-run --user` with `RuntimeMaxSec` set; it writes `~/nights/<id>/status` as one of `running`, `done`, `failed`, `killed`, `skipped`, and `exit_code` on exit (spec §4).
- Secrets never go on a command line. Prompts and tokens travel on stdin or in files with mode `0600` (08-05 plan Task 4 Step 6).
- `claude -p` never runs with `--bare` and never with `ANTHROPIC_API_KEY` set (spec §13 item 12).
- The box never commits to `hermes-wiki` (spec §9).
- Timers are installed disabled and enabled only after the dry night (spec §8).
- Branch is `master` on `hermes-harness`, not `main`. The spec says `main`; the repository's default branch is `master` (MEASURED 2026-09-24: `gh api repos/FibonAdithya/hermes-harness -q .default_branch`). Every reference below uses `master`.
- herdr on the box is 0.7.5, the laptop's version (MEASURED 2026-09-24: `herdr --version`).
- Prices in `fleet.toml` are the first-party API rates as of 2026-09-24 (MEASURED from `https://platform.claude.com/docs/en/about-claude/pricing.md`): Claude Sonnet 5 input 2.00, output 10.00, cache read 0.20, 5-minute cache write 2.50 USD per million tokens.
- All new work is committed on a branch `feat/tig-box` cut from `feat/hermes-dispatch`. The four files with uncommitted edits already on that branch (`docs/superpowers/plans/2026-07-28-*.md`, `docs/superpowers/specs/2026-07-28-*.md`, `runbooks/dispatch.md`, `runbooks/recovery.md`) are not touched and not staged by any task here. Stage explicit paths only.

## Review Focus

Inputs the spec implies but no task's tests would otherwise exercise; each line is pinned to a test in the task named.

1. **A verb name with a path separator or a dot** (`../verbs/x`, `status.py`). Expected: refused before any lookup. Pinned in Task 2.
2. **A night id from the broker that is not one the box minted** (`../../etc`, an empty string). Expected: `log` and `status` refuse it with a JSON error, never read outside `~/nights`. Pinned in Task 3.
3. **Stdin larger than a prompt should ever be** (a 10 MB body). Expected: the dispatcher stops reading at 1 MiB and refuses. Pinned in Task 2.
4. **`add_repo` for a fork, an org repo, or a name with a slash.** Expected: refused; nothing cloned. Pinned in Task 4.
5. **An empty Talos queue on a timer night.** Expected: status `skipped`, no Talos process, no error. Pinned in Task 5.

---

### Task 1: Provision the box

Spec §3. Makes the bare ARM box able to run everything else. Ops task: its test is that the script runs twice with the second run changing nothing, plus the checks listed.

**Files:**
- Create: `box/provision.sh`
- Create: `box/units/herdr-server.service`

**Interfaces:**
- Produces: user `adi` on the box; `node`, `uv`, `docker`, `gh`, `herdr`, `claude`, `codex` on `adi`'s PATH; `~adi/nights`, `~adi/TIG`, `~adi/.local/bin`; a running `herdr-server` user service; laptop SSH alias `tig-adi`.

- [ ] **Step 1: Write the provisioning script**

`box/provision.sh`:

```bash
#!/usr/bin/env bash
# Provision the TIG box. Run once as root over SSH; safe to re-run.
# Everything after the user exists runs as `adi` via `run_as_adi`.
set -euo pipefail

ADI_HOME=/home/adi
HERDR_VERSION="${HERDR_VERSION:-0.7.5}"

log() { printf '== %s\n' "$*"; }

run_as_adi() { sudo -u adi -H bash -lc "$*"; }

# --- packages (root) --------------------------------------------------------
log "apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential ca-certificates curl git gh jq \
  docker.io ripgrep unzip unattended-upgrades tmux python3 python3-venv
systemctl enable --now docker >/dev/null

# --- node 24 from NodeSource (root) -----------------------------------------
if ! command -v node >/dev/null || ! node --version | grep -q '^v24\.'; then
  log "node 24"
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash - >/dev/null
  apt-get install -y -qq nodejs
fi

# --- user (root) -------------------------------------------------------------
if ! id adi >/dev/null 2>&1; then
  log "user adi"
  adduser --disabled-password --gecos "" adi
fi
usermod -aG docker,sudo adi
install -d -o adi -g adi -m 0700 "$ADI_HOME/.ssh"
# The owner's own key (from the laptop) for herdr --remote and ssh.
if [ -n "${OWNER_PUBKEY:-}" ]; then
  grep -qxF "$OWNER_PUBKEY" "$ADI_HOME/.ssh/authorized_keys" 2>/dev/null \
    || echo "$OWNER_PUBKEY" >> "$ADI_HOME/.ssh/authorized_keys"
  chown adi:adi "$ADI_HOME/.ssh/authorized_keys"; chmod 0600 "$ADI_HOME/.ssh/authorized_keys"
fi
loginctl enable-linger adi
install -d -o adi -g adi -m 0700 /etc/hermes-exec

# --- per-user tooling (adi) --------------------------------------------------
log "directories"
run_as_adi 'mkdir -p ~/nights ~/TIG ~/.local/bin ~/.config/systemd/user'

log "uv"
run_as_adi 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'
run_as_adi 'uv python install 3.10 3.11 >/dev/null'

log "herdr"
run_as_adi "command -v herdr >/dev/null && herdr --version | grep -q '$HERDR_VERSION' \
  || curl -fsSL https://herdr.dev/install.sh | sh >/dev/null"

log "claude and codex"
run_as_adi 'command -v claude >/dev/null || npm install -g @anthropic-ai/claude-code >/dev/null'
run_as_adi 'command -v codex  >/dev/null || npm install -g @openai/codex >/dev/null'

log "done"
cat <<'EOF'
Provisioning complete. Interactive steps still needed, each over `ssh -t tig-adi`:
  claude          # then /login
  codex login
  gh auth login
Then copy the Claude guards:  scp -r ~/.claude/{CLAUDE.md,settings.json,hooks} tig-adi:~/.claude/
EOF
```

`npm install -g` as a non-root user needs a user prefix. NodeSource's node installs to `/usr/lib`, so add this line in the "claude and codex" block before the installs, and leave it in the script:

```bash
run_as_adi 'npm config get prefix | grep -q "$HOME/.npm-global" || { mkdir -p ~/.npm-global && npm config set prefix ~/.npm-global; }'
```

and `provision.sh` must make sure `~/.npm-global/bin` is on `adi`'s PATH:

```bash
run_as_adi 'grep -q npm-global ~/.profile || echo "export PATH=\$HOME/.npm-global/bin:\$HOME/.local/bin:\$PATH" >> ~/.profile'
```

Put both lines directly above `log "claude and codex"`.

- [ ] **Step 2: Write the herdr server unit**

`box/units/herdr-server.service`:

```ini
[Unit]
Description=herdr server (fleet's executor and the daily workspace)
After=network-online.target

[Service]
Type=simple
Environment=PATH=%h/.npm-global/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/herdr server run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`herdr server run` is the foreground server subcommand on 0.7.5 only if `herdr --help` on the box lists it. Verify in Step 5; if the subcommand differs, use what `herdr --help` prints and record it in the runbook (Task 13).

- [ ] **Step 3: Add the laptop SSH alias and run the script**

On the laptop, append to `~/.ssh/config`:

```
Host tig-adi
    HostName 2.29.24.53
    User adi
    IdentityFile ~/.ssh/tig_id
    IdentitiesOnly yes
```

Run the script as root, passing the owner's public key by environment so it never sits in shell history as a file path argument:

```bash
scp box/provision.sh tig-server:/root/provision.sh   # allow-plain-transfer: 3 KB script
ssh tig-server "OWNER_PUBKEY='$(cat ~/.ssh/tig_id.pub)' bash /root/provision.sh"
```

Expected: ends with `Provisioning complete.` and the interactive-steps list.

- [ ] **Step 4: Run it a second time and diff**

```bash
ssh tig-server 'bash /root/provision.sh' | tee /tmp/provision-2.log
grep -c '^== ' /tmp/provision-2.log
ssh tig-adi 'id; for c in node uv docker gh herdr claude codex; do printf "%-8s " $c; command -v $c >/dev/null && $c --version 2>&1 | head -1 || echo MISSING; done; loginctl show-user adi | grep Linger'
```

Expected: second run prints the same section headers with no installs, `id` shows groups `docker` and `sudo`, every tool prints a version, `Linger=yes`.

- [ ] **Step 5: Interactive logins and the herdr service**

```bash
ssh -t tig-adi claude          # /login, follow the browser flow, then /exit
ssh -t tig-adi codex login
ssh -t tig-adi gh auth login   # GitHub.com, HTTPS, login with a browser
scp -r ~/.claude/CLAUDE.md ~/.claude/settings.json ~/.claude/hooks tig-adi:~/.claude/   # allow-plain-transfer: config only, under 1 MB
scp box/units/herdr-server.service tig-adi:~/.config/systemd/user/   # allow-plain-transfer
ssh tig-adi 'systemctl --user daemon-reload && systemctl --user enable --now herdr-server && sleep 2 && herdr status server'
```

Expected: `claude -p "say ok"` over `ssh tig-adi` prints `ok`; `codex exec "say ok"` prints ok; `gh auth status` reports the owner's login; `herdr status server` reports a running server. Then from the laptop `herdr --remote tig-adi` attaches.

- [ ] **Step 6: Commit**

```bash
git checkout -b feat/tig-box
git add box/provision.sh box/units/herdr-server.service
git commit -m "feat(box): provisioning script and herdr server unit for the TIG box"
```

---

### Task 2: The dispatcher

Spec §4. The only thing the broker's key can run. Pure Python, stdlib only, tested on the laptop.

**Files:**
- Create: `box/dispatch` (executable, `#!/usr/bin/env python3`)
- Create: `box/boxlib.py`
- Create: `box/tests/test_dispatch.py`
- Create: `box/tests/conftest.py`

**Interfaces:**
- Produces: `boxlib.dispatch(argv_string: str, stdin: BinaryIO, verbs_dir: Path, env: dict) -> int` which execs `verbs_dir/<verb>` with stdin forwarded and returns its exit code; module constants `VERB_RE`, `MAX_STDIN = 1 << 20`; `boxlib.refuse(msg: str) -> int` printing `{"error": msg}` to stdout and returning 2.
- Every verb (Tasks 3, 4) reads one JSON object from stdin and prints one JSON object to stdout.

- [ ] **Step 1: Write the failing tests**

`box/tests/conftest.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```

`box/tests/test_dispatch.py`:

```python
import io
import json
import os
import stat
from pathlib import Path

import boxlib


def _verbs(tmp_path: Path) -> Path:
    d = tmp_path / "verbs"
    d.mkdir()
    echo = d / "echo_args"
    echo.write_text("#!/usr/bin/env python3\nimport sys,json\nprint(json.dumps({'got': json.load(sys.stdin)}))\n")
    echo.chmod(echo.stat().st_mode | stat.S_IEXEC)
    return d


def _run(verb_string, body, tmp_path, capsys):
    verbs = _verbs(tmp_path)
    rc = boxlib.dispatch(verb_string, io.BytesIO(body), verbs, {"HOME": str(tmp_path)})
    return rc, capsys.readouterr().out


def test_known_verb_runs_with_stdin(tmp_path, capsys):
    rc, out = _run("echo_args", b'{"x": 1}', tmp_path, capsys)
    assert rc == 0
    assert json.loads(out) == {"got": {"x": 1}}


def test_unknown_verb_is_refused(tmp_path, capsys):
    rc, out = _run("nope", b"{}", tmp_path, capsys)
    assert rc == 2
    assert "unknown verb" in json.loads(out)["error"]


def test_empty_command_is_refused(tmp_path, capsys):
    rc, out = _run("", b"{}", tmp_path, capsys)
    assert rc == 2


def test_shell_like_command_is_refused(tmp_path, capsys):
    for bad in ["bash -c id", "echo_args; id", "echo_args extra", "../echo_args", "echo_args.py", "ECHO_ARGS"]:
        rc, out = _run(bad, b"{}", tmp_path, capsys)
        assert rc == 2, bad
        assert "error" in json.loads(out), bad


def test_oversized_stdin_is_refused(tmp_path, capsys):
    rc, out = _run("echo_args", b"{" + b" " * (boxlib.MAX_STDIN + 10) + b"}", tmp_path, capsys)
    assert rc == 2
    assert "too large" in json.loads(out)["error"]


def test_invalid_json_is_refused(tmp_path, capsys):
    rc, out = _run("echo_args", b"not json", tmp_path, capsys)
    assert rc == 2
    assert "json" in json.loads(out)["error"].lower()


def test_non_executable_file_is_not_a_verb(tmp_path, capsys):
    verbs = _verbs(tmp_path)
    (verbs / "plain").write_text("x")
    rc = boxlib.dispatch("plain", io.BytesIO(b"{}"), verbs, {})
    assert rc == 2
```

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
cd box && python3 -m pytest tests/test_dispatch.py -q
```

Expected: `ModuleNotFoundError: No module named 'boxlib'`.

- [ ] **Step 3: Implement boxlib.dispatch and the dispatch entry point**

`box/boxlib.py`:

```python
"""The forced-command dispatcher and the helpers every verb shares.

The broker's SSH key can run exactly one program: `dispatch`. What it asked
for arrives in SSH_ORIGINAL_COMMAND and must be a bare verb name; the verb's
arguments are one JSON object on stdin. A verb is an executable file in the
verbs directory. Anything else is refused before any lookup happens.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_STDIN = 1 << 20  # 1 MiB: a prompt, never a file upload
NIGHT_ID_RE = re.compile(r"^[a-z]{1,8}-[0-9]{8}-[0-9]{4}-[a-f0-9]{4}$")


def refuse(msg: str) -> int:
    print(json.dumps({"error": msg}))
    sys.stdout.flush()
    return 2


def read_json_stdin(stdin: BinaryIO, limit: int = MAX_STDIN) -> dict | None:
    """One JSON object, or None if the body is oversized or not an object."""
    data = stdin.read(limit + 1)
    if len(data) > limit:
        return None
    try:
        obj = json.loads(data.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def dispatch(command: str, stdin: BinaryIO, verbs_dir: Path, env: dict) -> int:
    verb = command.strip()
    if not VERB_RE.match(verb):
        return refuse("invalid verb: a bare lowercase name is required")
    path = Path(verbs_dir) / verb
    if not path.is_file() or not os.access(path, os.X_OK):
        return refuse(f"unknown verb: {verb}")
    body = stdin.read(MAX_STDIN + 1)
    if len(body) > MAX_STDIN:
        return refuse("stdin too large")
    try:
        obj = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return refuse("stdin is not valid json")
    if not isinstance(obj, dict):
        return refuse("stdin json must be an object")
    # capture_output rather than inheriting stdout: pytest's captured stdout has
    # no real file descriptor, and production and tests must share one path.
    proc = subprocess.run(
        [str(path)],
        input=json.dumps(obj).encode("utf-8"),
        env={**env, "PATH": env.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        check=False,
    )
    sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    sys.stdout.flush()
    return proc.returncode
```

`box/dispatch`:

```python
#!/usr/bin/env python3
"""Forced-command entry point. See boxlib.dispatch."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boxlib  # noqa: E402

HOME = os.environ.get("HOME", "/home/adi")
ENV = {
    "HOME": HOME,
    "PATH": f"{HOME}/.npm-global/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    "LANG": "C.UTF-8",
}
sys.exit(boxlib.dispatch(
    os.environ.get("SSH_ORIGINAL_COMMAND", ""),
    sys.stdin.buffer,
    Path(__file__).resolve().parent / "verbs",
    ENV,
))
```

`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are what let `systemd-run --user` find the user manager from a non-login SSH session; without them every long verb fails with "Failed to connect to bus".

```bash
chmod +x box/dispatch
```

- [ ] **Step 4: Run the tests and confirm they pass**

```bash
cd box && python3 -m pytest tests/test_dispatch.py -q
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add box/dispatch box/boxlib.py box/tests/conftest.py box/tests/test_dispatch.py
git commit -m "feat(box): forced-command dispatcher with a verb directory"
```

---

### Task 3: Night bookkeeping and the read verbs

Spec §4 "long verbs detach" and "read verbs answer from files". The helpers that mint ids, start transient units, and read status back; then the three ungated verbs.

**Files:**
- Modify: `box/boxlib.py`
- Create: `box/run-night.sh`
- Create: `box/verbs/status`, `box/verbs/log`, `box/verbs/list_repos`
- Create: `box/tests/test_nights.py`

**Interfaces:**
- Produces in `boxlib`: `nights_root(env) -> Path` (`$HOME/nights`); `new_night_id(kind: str, now: float | None = None) -> str`; `check_night_id(s) -> str` (raises `ValueError`); `night_dir(root, id) -> Path`; `write_status(dir, status: str, exit_code: int | None = None)`; `read_status(root) -> list[dict]` (one dict per night: `id, status, started, exit_code`); `systemd_run_argv(unit, runtime_sec, workdir, env: dict, argv) -> list[str]`.
- Produces on the box: `~/.local/bin/run-night.sh <night_dir> <cmd...>` wrapper that writes `status`/`exit_code`/`log`.
- Verb contract: `status` takes `{}` and returns `{"nights": [...]}`; `log` takes `{"id": str, "lines": int}` and returns `{"id": ..., "lines": [...]}`; `list_repos` takes `{}` and returns `{"repos": [names]}`.

- [ ] **Step 1: Write the failing tests**

`box/tests/test_nights.py`:

```python
import json
import subprocess
from pathlib import Path

import pytest

import boxlib

VERBS = Path(__file__).resolve().parents[1] / "verbs"


def test_night_id_shape_and_validation():
    nid = boxlib.new_night_id("fleet", now=1758700000.0)
    assert boxlib.NIGHT_ID_RE.match(nid), nid
    assert nid.startswith("fleet-20260924-")
    assert boxlib.check_night_id(nid) == nid
    for bad in ["", "../x", "fleet-2026-1", "FLEET-20260924-0800-abcd", "a/b", nid + "\n"]:
        with pytest.raises(ValueError):
            boxlib.check_night_id(bad)


def test_two_ids_differ():
    assert boxlib.new_night_id("talos", now=1.0) != boxlib.new_night_id("talos", now=1.0)


def test_status_reads_every_night(tmp_path):
    root = tmp_path / "nights"
    a = boxlib.night_dir(root, "fleet-20260924-2300-aaaa")
    b = boxlib.night_dir(root, "talos-20260924-2301-bbbb")
    a.mkdir(parents=True); b.mkdir(parents=True)
    boxlib.write_status(a, "running")
    boxlib.write_status(b, "failed", exit_code=3)
    (root / "not-a-night").mkdir()
    rows = boxlib.read_status(root)
    assert {r["id"] for r in rows} == {"fleet-20260924-2300-aaaa", "talos-20260924-2301-bbbb"}
    by = {r["id"]: r for r in rows}
    assert by["talos-20260924-2301-bbbb"]["status"] == "failed"
    assert by["talos-20260924-2301-bbbb"]["exit_code"] == 3
    assert by["fleet-20260924-2300-aaaa"]["exit_code"] is None


def test_systemd_run_argv_carries_the_limit_and_env():
    argv = boxlib.systemd_run_argv("night-x", 3600, Path("/tmp/w"), {"PATH": "/p"}, ["/bin/true"])
    assert argv[:3] == ["systemd-run", "--user", "--unit=night-x"]
    assert "--property=RuntimeMaxSec=3600" in argv
    assert "--setenv=PATH=/p" in argv
    assert argv[-1] == "/bin/true"


def _verb(name, payload, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run([str(VERBS / name)], input=json.dumps(payload).encode(), capture_output=True, env=env)


def test_status_verb_lists_nights(tmp_path):
    d = boxlib.night_dir(tmp_path / "nights", "task-20260924-0100-cafe")
    d.mkdir(parents=True)
    boxlib.write_status(d, "done", exit_code=0)
    p = _verb("status", {}, tmp_path)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["nights"][0]["id"] == "task-20260924-0100-cafe"
    assert out["nights"][0]["status"] == "done"


def test_log_verb_tails_and_refuses_bad_ids(tmp_path):
    d = boxlib.night_dir(tmp_path / "nights", "task-20260924-0100-cafe")
    d.mkdir(parents=True)
    (d / "log").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    p = _verb("log", {"id": "task-20260924-0100-cafe", "lines": 3}, tmp_path)
    assert json.loads(p.stdout)["lines"] == ["line 97", "line 98", "line 99"]
    for bad in ["../../etc/passwd", "", "task-20260924-0100-cafe/../x"]:
        p = _verb("log", {"id": bad, "lines": 3}, tmp_path)
        assert p.returncode == 2, bad
        assert "error" in json.loads(p.stdout)


def test_log_verb_for_missing_night(tmp_path):
    p = _verb("log", {"id": "task-20260924-0100-dead", "lines": 3}, tmp_path)
    assert p.returncode == 2
    assert "no such night" in json.loads(p.stdout)["error"]


def test_list_repos_verb(tmp_path):
    (tmp_path / "TIG" / "fleet").mkdir(parents=True)
    (tmp_path / "TIG" / "a-file").write_text("x")
    p = _verb("list_repos", {}, tmp_path)
    assert json.loads(p.stdout)["repos"] == ["fleet"]
```

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
cd box && python3 -m pytest tests/test_nights.py -q
```

Expected: failures on `new_night_id` missing, then on missing verb files.

- [ ] **Step 3: Add the helpers to boxlib**

Append to `box/boxlib.py`:

```python
import secrets
import time

STATUSES = ("running", "done", "failed", "killed", "skipped")


def nights_root(env: dict | None = None) -> Path:
    home = (env or os.environ).get("HOME", "/home/adi")
    return Path(home) / "nights"


def new_night_id(kind: str, now: float | None = None) -> str:
    if not re.match(r"^[a-z]{1,8}$", kind):
        raise ValueError(f"bad kind: {kind!r}")
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(now if now is not None else time.time()))
    return f"{kind}-{stamp}-{secrets.token_hex(2)}"


def check_night_id(s: str) -> str:
    if not isinstance(s, str) or not NIGHT_ID_RE.match(s):
        raise ValueError(f"invalid night id: {s!r}")
    return s


def night_dir(root: Path, night_id: str) -> Path:
    return Path(root) / check_night_id(night_id)


def write_status(d: Path, status: str, exit_code: int | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    (Path(d) / "status").write_text(status + "\n")
    if exit_code is not None:
        (Path(d) / "exit_code").write_text(f"{int(exit_code)}\n")


def read_status(root: Path) -> list[dict]:
    rows: list[dict] = []
    root = Path(root)
    if not root.is_dir():
        return rows
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not NIGHT_ID_RE.match(d.name):
            continue
        status_file = d / "status"
        exit_file = d / "exit_code"
        rows.append({
            "id": d.name,
            "status": status_file.read_text().strip() if status_file.is_file() else "unknown",
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d.stat().st_mtime)),
            "exit_code": int(exit_file.read_text()) if exit_file.is_file() else None,
        })
    return rows


def systemd_run_argv(unit: str, runtime_sec: int, workdir: Path, env: dict, argv: list[str]) -> list[str]:
    out = [
        "systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
        f"--property=RuntimeMaxSec={int(runtime_sec)}",
        f"--property=WorkingDirectory={workdir}",
    ]
    for k, v in env.items():
        out.append(f"--setenv={k}={v}")
    out.extend(argv)
    return out
```

- [ ] **Step 4: Write the night wrapper and the three verbs**

`box/run-night.sh` (the process every transient unit actually runs):

```bash
#!/usr/bin/env bash
# run-night.sh <night_dir> <command...>
# Writes status, log and exit_code around one command. SIGTERM (what
# RuntimeMaxSec sends) becomes status=killed, not a silent "running" forever.
set -uo pipefail
dir="$1"; shift
mkdir -p "$dir"
echo running > "$dir/status"
term() { echo killed > "$dir/status"; echo 143 > "$dir/exit_code"; kill -TERM "$child" 2>/dev/null; wait "$child"; exit 143; }
trap term TERM INT
"$@" >> "$dir/log" 2>&1 &
child=$!
wait "$child"; rc=$?
echo "$rc" > "$dir/exit_code"
if [ "$rc" -eq 0 ]; then echo done > "$dir/status"; else echo failed > "$dir/status"; fi
exit "$rc"
```

`box/verbs/status`:

```python
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

boxlib.read_json_stdin(sys.stdin.buffer)  # arguments are ignored; consume stdin
print(json.dumps({"nights": boxlib.read_status(boxlib.nights_root(os.environ))}))
```

`box/verbs/log`:

```python
#!/usr/bin/env python3
import json, os, sys
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

args = boxlib.read_json_stdin(sys.stdin.buffer) or {}
try:
    night_id = boxlib.check_night_id(args.get("id", ""))
except ValueError as exc:
    sys.exit(boxlib.refuse(str(exc)))
lines = max(1, min(int(args.get("lines", 80)), 2000))
d = boxlib.night_dir(boxlib.nights_root(os.environ), night_id)
if not d.is_dir():
    sys.exit(boxlib.refuse(f"no such night: {night_id}"))
log = d / "log"
tail = deque(log.read_text(errors="replace").splitlines(), maxlen=lines) if log.is_file() else deque()
print(json.dumps({"id": night_id, "lines": list(tail)}))
```

`box/verbs/list_repos`:

```python
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

boxlib.read_json_stdin(sys.stdin.buffer)
tig = Path(os.environ.get("HOME", "/home/adi")) / "TIG"
repos = sorted(p.name for p in tig.iterdir() if p.is_dir()) if tig.is_dir() else []
print(json.dumps({"repos": repos}))
```

```bash
chmod +x box/run-night.sh box/verbs/status box/verbs/log box/verbs/list_repos
```

- [ ] **Step 5: Run the tests and confirm they pass**

```bash
cd box && python3 -m pytest tests -q
```

Expected: all tests in both files pass.

- [ ] **Step 6: Commit**

```bash
git add box/boxlib.py box/run-night.sh box/verbs/status box/verbs/log box/verbs/list_repos box/tests/test_nights.py
git commit -m "feat(box): night bookkeeping, the run-night wrapper, and the read verbs"
```

---

### Task 4: The gated verbs: run_fleet, run_talos, add_repo, run_task

Spec §5 and §6, §7 for what each launches. Each verb validates its arguments with a pure function that is unit tested, then starts a transient unit through `run-night.sh`.

**Files:**
- Modify: `box/boxlib.py`
- Create: `box/verbs/run_fleet`, `box/verbs/run_talos`, `box/verbs/add_repo`, `box/verbs/run_task`
- Modify: `executor/run-task.sh`
- Create: `box/tests/test_gated_verbs.py`

**Interfaces:**
- Produces in `boxlib`: `fleet_command(home, repo, hours) -> tuple[Path, list[str], int]` returning `(workdir, argv, runtime_sec)`; `talos_command(home, challenge, direction, iterations, backend) -> tuple[Path, list[str], int]`; `add_repo_check(owner_login, repo_json: dict, name: str) -> str | None` returning a refusal reason or `None`; `task_command(home, night_dir, repo, minutes) -> tuple[Path, list[str], int]`; `start_night(kind, workdir, argv, runtime_sec, env) -> str` that mints the id, creates the dir, runs `systemd-run`, and returns the id.
- Verb payloads: `run_fleet {"repo": str, "hours": int}`; `run_talos {"challenge": str, "direction": str, "iterations": int, "backend": "local"|"modal"}`; `add_repo {"name": str}`; `run_task {"repo": "owner/name", "prompt": str, "minutes": int}`. Each returns `{"id": night_id}` or `{"error": ...}` with exit 2.
- `executor/run-task.sh <night_dir> <owner/repo> <minutes>` now runs in the foreground (the transient unit is what detaches it) and reads the prompt from `<night_dir>/prompt.txt`.

- [ ] **Step 1: Write the failing tests**

`box/tests/test_gated_verbs.py`:

```python
from pathlib import Path

import pytest

import boxlib

HOME = Path("/home/adi")


def test_fleet_command_shape():
    wd, argv, secs = boxlib.fleet_command(HOME, "fleet-fixture", hours=8)
    assert wd == HOME / "TIG" / "fleet-fixture"
    assert argv[:3] == ["uv", "run", "--project"]
    assert argv[3] == str(HOME / "TIG" / "fleet")
    assert argv[4:] == ["fleet", "run", "--new-run"]
    assert secs == 8 * 3600


def test_fleet_command_refuses_bad_repo_and_hours():
    for bad in ["../x", "a/b", "", "Fleet Fixture"]:
        with pytest.raises(ValueError):
            boxlib.fleet_command(HOME, bad, hours=1)
    for bad in [0, -1, 25]:
        with pytest.raises(ValueError):
            boxlib.fleet_command(HOME, "fleet-fixture", hours=bad)


def test_talos_command_shape():
    wd, argv, secs = boxlib.talos_command(HOME, "knapsack", "try a greedy warm start", 20, "local")
    assert wd == HOME / "talos-local"
    assert argv[0] == str(HOME / "talos-local" / ".venv" / "bin" / "talos")
    assert argv[1:] == ["run", "--challenge", "knapsack", "--direction", "try a greedy warm start",
                        "--budget-iterations", "20", "--yes"]
    assert secs == 12 * 3600
    wd2, _, _ = boxlib.talos_command(HOME, "knapsack", "x", 1, "modal")
    assert wd2 == HOME / "talos-modal"


def test_talos_command_refuses_bad_inputs():
    with pytest.raises(ValueError):
        boxlib.talos_command(HOME, "knapsack", "x", 1, "c3")
    with pytest.raises(ValueError):
        boxlib.talos_command(HOME, "not a challenge", "x", 1, "local")
    with pytest.raises(ValueError):
        boxlib.talos_command(HOME, "knapsack", "", 1, "local")
    for bad in [0, 501]:
        with pytest.raises(ValueError):
            boxlib.talos_command(HOME, "knapsack", "x", bad, "local")


def test_add_repo_check():
    ok = {"owner": {"login": "FibonAdithya"}, "fork": False, "name": "fleet"}
    assert boxlib.add_repo_check("FibonAdithya", ok, "fleet") is None
    assert "fork" in boxlib.add_repo_check("FibonAdithya", {**ok, "fork": True}, "fleet")
    assert "own" in boxlib.add_repo_check("FibonAdithya", {**ok, "owner": {"login": "someone"}}, "fleet")
    for bad in ["a/b", "", "..", "x" * 101, "has space"]:
        assert boxlib.add_repo_check("FibonAdithya", ok, bad) is not None, bad


def test_task_command_shape(tmp_path):
    wd, argv, secs = boxlib.task_command(HOME, tmp_path, "FibonAdithya/hermes-harness", minutes=30)
    assert argv[0] == str(HOME / ".local" / "bin" / "run-task.sh")
    assert argv[1:] == [str(tmp_path), "FibonAdithya/hermes-harness", "30"]
    assert secs == 30 * 60 + 300
    with pytest.raises(ValueError):
        boxlib.task_command(HOME, tmp_path, "no-slash", 30)
    with pytest.raises(ValueError):
        boxlib.task_command(HOME, tmp_path, "FibonAdithya/hermes-harness", 0)
```

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
cd box && python3 -m pytest tests/test_gated_verbs.py -q
```

Expected: `AttributeError: module 'boxlib' has no attribute 'fleet_command'`.

- [ ] **Step 3: Implement the command builders and start_night**

Append to `box/boxlib.py`:

```python
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# The challenges the pinned Talos knows. Extend by PR when Talos does.
TALOS_CHALLENGES = ("knapsack", "vehicle_routing", "satisfiability", "vector_search", "hypergraph", "neuralnet_optimizer")
TALOS_BACKENDS = ("local", "modal")
MAX_FLEET_HOURS = 24
MAX_TALOS_ITER = 500
MAX_TASK_MINUTES = 240


def _name(s: str) -> str:
    if not isinstance(s, str) or not NAME_RE.match(s) or s in (".", ".."):
        raise ValueError(f"invalid name: {s!r}")
    return s


def fleet_command(home: Path, repo: str, hours: int) -> tuple[Path, list[str], int]:
    repo = _name(repo)
    if not 1 <= int(hours) <= MAX_FLEET_HOURS:
        raise ValueError(f"hours must be 1..{MAX_FLEET_HOURS}")
    home = Path(home)
    workdir = home / "TIG" / repo
    argv = ["uv", "run", "--project", str(home / "TIG" / "fleet"), "fleet", "run", "--new-run"]
    return workdir, argv, int(hours) * 3600


def talos_command(home: Path, challenge: str, direction: str, iterations: int, backend: str) -> tuple[Path, list[str], int]:
    if challenge not in TALOS_CHALLENGES:
        raise ValueError(f"unknown challenge: {challenge!r}")
    if backend not in TALOS_BACKENDS:
        raise ValueError(f"unknown backend: {backend!r}")
    if not isinstance(direction, str) or not direction.strip():
        raise ValueError("direction is required")
    if not 1 <= int(iterations) <= MAX_TALOS_ITER:
        raise ValueError(f"iterations must be 1..{MAX_TALOS_ITER}")
    workdir = Path(home) / f"talos-{backend}"
    argv = [str(workdir / ".venv" / "bin" / "talos"), "run", "--challenge", challenge,
            "--direction", direction, "--budget-iterations", str(int(iterations)), "--yes"]
    return workdir, argv, 12 * 3600


def add_repo_check(owner_login: str, repo_json: dict, name: str) -> str | None:
    try:
        _name(name)
    except ValueError as exc:
        return str(exc)
    if repo_json.get("fork"):
        return f"{name} is a fork; only repositories you own outright are cloned"
    if (repo_json.get("owner") or {}).get("login") != owner_login:
        return f"{name} is not owned by {owner_login}"
    return None


def task_command(home: Path, night_dir: Path, repo: str, minutes: int) -> tuple[Path, list[str], int]:
    if not isinstance(repo, str) or not SLUG_RE.match(repo):
        raise ValueError(f"repo must be owner/name: {repo!r}")
    if not 1 <= int(minutes) <= MAX_TASK_MINUTES:
        raise ValueError(f"minutes must be 1..{MAX_TASK_MINUTES}")
    argv = [str(Path(home) / ".local" / "bin" / "run-task.sh"), str(night_dir), repo, str(int(minutes))]
    return Path(night_dir), argv, int(minutes) * 60 + 300


def start_night(kind: str, workdir: Path, argv: list[str], runtime_sec: int, env: dict, meta: dict | None = None) -> str:
    """Mint an id, create the directory, hand the command to systemd. Returns the id."""
    root = nights_root(env)
    night_id = new_night_id(kind)
    d = night_dir(root, night_id)
    d.mkdir(parents=True, exist_ok=False)
    (d / "meta.json").write_text(json.dumps({"kind": kind, "argv": argv, "workdir": str(workdir), **(meta or {})}))
    write_status(d, "running")
    home = env.get("HOME", "/home/adi")
    wrapper = str(Path(home) / ".local" / "bin" / "run-night.sh")
    full = systemd_run_argv(f"night-{night_id}", runtime_sec, workdir, env, [wrapper, str(d), *argv])
    proc = subprocess.run(full, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        write_status(d, "failed", exit_code=proc.returncode)
        (d / "log").write_text(f"systemd-run failed: {proc.stderr}\n")
        raise RuntimeError(f"systemd-run failed: {proc.stderr.strip()[:300]}")
    return night_id
```

- [ ] **Step 4: Write the four verbs**

`box/verbs/run_fleet`:

```python
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
home = Path(os.environ.get("HOME", "/home/adi"))
try:
    workdir, argv, secs = boxlib.fleet_command(home, a.get("repo", ""), int(a.get("hours", 8)))
except (ValueError, TypeError) as exc:
    sys.exit(boxlib.refuse(str(exc)))
if not (workdir / "fleet.toml").is_file():
    sys.exit(boxlib.refuse(f"{workdir.name} has no fleet.toml"))
try:
    night_id = boxlib.start_night("fleet", workdir, argv, secs, dict(os.environ), {"repo": workdir.name})
except RuntimeError as exc:
    sys.exit(boxlib.refuse(str(exc)))
print(json.dumps({"id": night_id}))
```

`box/verbs/run_talos`:

```python
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
home = Path(os.environ.get("HOME", "/home/adi"))
try:
    workdir, argv, secs = boxlib.talos_command(
        home, a.get("challenge", ""), a.get("direction", ""), int(a.get("iterations", 10)), a.get("backend", "local"))
except (ValueError, TypeError) as exc:
    sys.exit(boxlib.refuse(str(exc)))
if not (workdir / "talos.config.json").is_file():
    sys.exit(boxlib.refuse(f"{workdir.name} is not set up: run talos setup there first"))
try:
    night_id = boxlib.start_night("talos", workdir, argv, secs, dict(os.environ),
                                  {"challenge": a["challenge"], "direction": a["direction"], "backend": a.get("backend", "local")})
except RuntimeError as exc:
    sys.exit(boxlib.refuse(str(exc)))
print(json.dumps({"id": night_id}))
```

`box/verbs/add_repo`:

```python
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
name = a.get("name", "")
home = Path(os.environ.get("HOME", "/home/adi"))


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


me = gh("api", "user", "-q", ".login")
if me.returncode != 0:
    sys.exit(boxlib.refuse("gh is not logged in on the box"))
owner = me.stdout.strip()
try:
    boxlib._name(name)
except ValueError as exc:
    sys.exit(boxlib.refuse(str(exc)))
info = gh("api", f"repos/{owner}/{name}")
if info.returncode != 0:
    sys.exit(boxlib.refuse(f"no repository {owner}/{name} visible to this login"))
reason = boxlib.add_repo_check(owner, json.loads(info.stdout), name)
if reason:
    sys.exit(boxlib.refuse(reason))
dest = home / "TIG" / name
if dest.exists():
    print(json.dumps({"repo": name, "path": str(dest), "already": True}))
    sys.exit(0)
clone = gh("repo", "clone", f"{owner}/{name}", str(dest))
if clone.returncode != 0:
    sys.exit(boxlib.refuse(f"clone failed: {clone.stderr.strip()[:300]}"))
print(json.dumps({"repo": name, "path": str(dest), "already": False}))
```

`box/verbs/run_task`:

```python
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boxlib  # noqa: E402

a = boxlib.read_json_stdin(sys.stdin.buffer) or {}
home = Path(os.environ.get("HOME", "/home/adi"))
prompt = a.get("prompt", "")
if not isinstance(prompt, str) or not prompt.strip():
    sys.exit(boxlib.refuse("prompt is required"))
root = boxlib.nights_root(os.environ)
night_id = boxlib.new_night_id("task")
d = boxlib.night_dir(root, night_id)
d.mkdir(parents=True)
(d / "prompt.txt").write_text(prompt)
try:
    workdir, argv, secs = boxlib.task_command(home, d, a.get("repo", ""), int(a.get("minutes", 30)))
except (ValueError, TypeError) as exc:
    sys.exit(boxlib.refuse(str(exc)))
boxlib.write_status(d, "running")
wrapper = str(home / ".local" / "bin" / "run-night.sh")
full = boxlib.systemd_run_argv(f"night-{night_id}", secs, workdir, dict(os.environ), [wrapper, str(d), *argv])
proc = subprocess.run(full, capture_output=True, text=True, check=False)
if proc.returncode != 0:
    boxlib.write_status(d, "failed", exit_code=proc.returncode)
    sys.exit(boxlib.refuse(f"systemd-run failed: {proc.stderr.strip()[:300]}"))
print(json.dumps({"id": night_id}))
```

`run_task` mints its own id before the prompt is written because the prompt must be on disk before the unit starts; `start_night` is not used here for that reason.

Replace `executor/run-task.sh` with the foreground version:

```bash
#!/usr/bin/env bash
# Run one Claude Code task in a throwaway container, in the foreground.
# The transient systemd unit that calls this is what detaches it.
#
# Usage: run-task.sh <night_dir> <owner/repo> <minutes>
set -euo pipefail
dir="$1"; repo="$2"; minutes="$3"

token_file="/etc/hermes-exec/github-token"
claude_creds="$HOME/.claude/.credentials.json"
[ -s "$token_file" ] || { echo "no github token at $token_file" >&2; exit 78; }
[ -s "$claude_creds" ] || { echo "no claude login at $claude_creds" >&2; exit 78; }

exec docker run --rm \
  --name "hermes-task-$(basename "$dir")" \
  --memory 6g --cpus 4 \
  -v "${dir}:/work" \
  -v "${claude_creds}:/home/runner/.claude/.credentials.json:ro" \
  -e "GH_TOKEN=$(cat "$token_file")" \
  -e "TASK_PROMPT_FILE=/work/prompt.txt" \
  -e "TASK_REPO=${repo}" \
  -e "TASK_ID=$(basename "$dir")" \
  hermes-exec:latest \
  bash -lc '
    set -euo pipefail
    git clone --depth 50 "https://github.com/${TASK_REPO}.git" /work/repo
    cd /work/repo
    git checkout -b "hermes/${TASK_ID}"
    claude -p "$(cat "${TASK_PROMPT_FILE}")" \
      --dangerously-skip-permissions \
      --output-format json \
      --append-system-prompt "You are working in a throwaway container on a scratch box. Stay inside /work/repo. Make the change, run the tests, commit, push the branch hermes/${TASK_ID}, and open a pull request with gh. Do not push to master or main and do not merge anything."
  '
```

```bash
chmod +x box/verbs/run_fleet box/verbs/run_talos box/verbs/add_repo box/verbs/run_task executor/run-task.sh
```

- [ ] **Step 5: Run the tests and confirm they pass**

```bash
cd box && python3 -m pytest tests -q
```

Expected: all pass, including the five new ones.

- [ ] **Step 6: Commit**

```bash
git add box/boxlib.py box/verbs/run_fleet box/verbs/run_talos box/verbs/add_repo box/verbs/run_task box/tests/test_gated_verbs.py executor/run-task.sh
git commit -m "feat(box): gated verbs for fleet, talos, add_repo and run_task"
```

---

### Task 5: Timers, the night config, and install.sh

Spec §8 (timers off by default, fixed inputs, empty queue skips) and §10 (install.sh). The timers call `box/night.py`, which reads `~/nights/config.toml`, pops the Talos queue, and calls the same builders the verbs use.

**Files:**
- Create: `box/night.py`
- Create: `box/night-config.example.toml`
- Create: `box/units/fleet-night.service`, `box/units/fleet-night.timer`, `box/units/talos-night.service`, `box/units/talos-night.timer`
- Create: `box/install.sh`
- Create: `box/tests/test_night_config.py`

**Interfaces:**
- Produces: `night.pop_talos(config: dict) -> tuple[dict | None, dict]` returning the next queue entry and the rewritten config; `night.main(argv)` with `argv[1] in ("fleet", "talos")`.
- `~/nights/config.toml` shape:

```toml
[fleet]
repo = "fleet-fixture"
hours = 8

[talos]
iterations = 30
backend = "local"

[[talos.queue]]
challenge = "knapsack"
direction = "try a randomized greedy warm start before local search"
```

- [ ] **Step 1: Write the failing tests**

`box/tests/test_night_config.py`:

```python
import tomllib

import night


def test_pop_talos_takes_the_first_and_rewrites():
    cfg = {"talos": {"iterations": 3, "queue": [{"challenge": "knapsack", "direction": "a"},
                                                 {"challenge": "knapsack", "direction": "b"}]}}
    entry, rest = night.pop_talos(cfg)
    assert entry == {"challenge": "knapsack", "direction": "a"}
    assert rest["talos"]["queue"] == [{"challenge": "knapsack", "direction": "b"}]


def test_pop_talos_on_empty_queue_is_none():
    entry, rest = night.pop_talos({"talos": {"queue": []}})
    assert entry is None
    entry, rest = night.pop_talos({"talos": {}})
    assert entry is None


def test_dump_toml_round_trips_the_queue():
    cfg = {"fleet": {"repo": "fleet-fixture", "hours": 8},
           "talos": {"iterations": 30, "backend": "local",
                     "queue": [{"challenge": "knapsack", "direction": 'say "hi"'}]}}
    text = night.dump_toml(cfg)
    assert tomllib.loads(text) == cfg
```

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
cd box && python3 -m pytest tests/test_night_config.py -q
```

Expected: `ModuleNotFoundError: No module named 'night'`.

- [ ] **Step 3: Implement night.py**

`box/night.py`:

```python
#!/usr/bin/env python3
"""Timer entry point: start tonight's fleet or Talos run from ~/nights/config.toml.

Takes no input from anywhere but that file. An empty Talos queue records a
`skipped` night and exits 0, so a quiet night is not an alert.
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boxlib  # noqa: E402


def pop_talos(cfg: dict) -> tuple[dict | None, dict]:
    talos = dict(cfg.get("talos", {}))
    queue = list(talos.get("queue", []))
    if not queue:
        return None, cfg
    entry, rest = queue[0], queue[1:]
    out = {**cfg, "talos": {**talos, "queue": rest}}
    return entry, out


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v)  # a JSON string literal is a valid TOML basic string


def dump_toml(cfg: dict) -> str:
    """Enough TOML for this file: flat tables and one array of tables."""
    lines: list[str] = []
    for table, body in cfg.items():
        scalars = {k: v for k, v in body.items() if not isinstance(v, list)}
        lines.append(f"[{table}]")
        for k, v in scalars.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
        for k, v in body.items():
            if isinstance(v, list):
                for item in v:
                    lines.append(f"[[{table}.{k}]]")
                    for ik, iv in item.items():
                        lines.append(f"{ik} = {_toml_value(iv)}")
                    lines.append("")
    return "\n".join(lines)


def _skip(root: Path, kind: str, why: str) -> None:
    d = boxlib.night_dir(root, boxlib.new_night_id(kind))
    d.mkdir(parents=True)
    (d / "log").write_text(why + "\n")
    boxlib.write_status(d, "skipped", exit_code=0)


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    home = Path(env.get("HOME", "/home/adi"))
    root = boxlib.nights_root(env)
    path = root / "config.toml"
    if not path.is_file():
        _skip(root, kind or "night", f"no {path}")
        return 0
    cfg = tomllib.loads(path.read_text())
    if kind == "fleet":
        f = cfg.get("fleet", {})
        workdir, cmd, secs = boxlib.fleet_command(home, f.get("repo", ""), int(f.get("hours", 8)))
        night_id = boxlib.start_night("fleet", workdir, cmd, secs, env, {"repo": workdir.name, "timer": True})
    elif kind == "talos":
        entry, rest = pop_talos(cfg)
        if entry is None:
            _skip(root, "talos", "talos queue is empty")
            return 0
        t = cfg.get("talos", {})
        workdir, cmd, secs = boxlib.talos_command(home, entry["challenge"], entry["direction"],
                                                  int(t.get("iterations", 30)), t.get("backend", "local"))
        path.write_text(dump_toml(rest))
        night_id = boxlib.start_night("talos", workdir, cmd, secs, env, {**entry, "timer": True})
    else:
        print(json.dumps({"error": f"usage: night.py fleet|talos, got {kind!r}"}))
        return 2
    print(json.dumps({"id": night_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

`box/night-config.example.toml`: the shape shown in Interfaces, verbatim.

- [ ] **Step 4: Run the tests and confirm they pass**

```bash
cd box && python3 -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 5: Write the units and install.sh**

`box/units/fleet-night.service`:

```ini
[Unit]
Description=Start tonight's fleet run from ~/nights/config.toml

[Service]
Type=oneshot
Environment=PATH=%h/.npm-global/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 %h/TIG/hermes-harness/box/night.py fleet
```

`box/units/fleet-night.timer`:

```ini
[Unit]
Description=fleet night at 23:00 UTC

[Timer]
OnCalendar=*-*-* 23:00:00 UTC
Persistent=false

[Install]
WantedBy=timers.target
```

`box/units/talos-night.service`: same as fleet-night.service with `Description=Start tonight's Talos run from ~/nights/config.toml` and `ExecStart=/usr/bin/python3 %h/TIG/hermes-harness/box/night.py talos`.

`box/units/talos-night.timer`: same as fleet-night.timer with `Description=Talos night at 23:05 UTC` and `OnCalendar=*-*-* 23:05:00 UTC`.

`box/install.sh`:

```bash
#!/usr/bin/env bash
# Install or refresh the box side of hermes-harness. Idempotent. Runs as adi.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin"; units="$HOME/.config/systemd/user"
mkdir -p "$bin" "$units" "$HOME/nights"
ln -sfn "$here/dispatch" "$bin/dispatch"
ln -sfn "$here/run-night.sh" "$bin/run-night.sh"
ln -sfn "$here/../executor/run-task.sh" "$bin/run-task.sh"
chmod +x "$here/dispatch" "$here/run-night.sh" "$here"/verbs/* "$here/../executor/run-task.sh"
cp "$here"/units/*.service "$here"/units/*.timer "$units/"
[ -f "$HOME/nights/config.toml" ] || cp "$here/night-config.example.toml" "$HOME/nights/config.toml"
systemctl --user daemon-reload
# Timers are installed but never enabled here. Enabling is the owner's act after the dry night.
echo "installed: $(ls "$here/verbs" | tr '\n' ' ')"
```

```bash
chmod +x box/install.sh
```

- [ ] **Step 6: Deploy to the box and run the dispatcher by hand**

```bash
ssh tig-adi 'gh repo clone FibonAdithya/hermes-harness ~/TIG/hermes-harness && cd ~/TIG/hermes-harness && git checkout feat/tig-box'
```

That needs the branch pushed first:

```bash
git push -u origin feat/tig-box
```

Then:

```bash
ssh tig-adi '~/TIG/hermes-harness/box/install.sh && echo "{}" | SSH_ORIGINAL_COMMAND=status ~/.local/bin/dispatch && echo "{}" | SSH_ORIGINAL_COMMAND="bash -c id" ~/.local/bin/dispatch; echo "rc=$?"'
```

Expected: `{"nights": []}` then `{"error": "invalid verb: ..."}` and `rc=2`.

Prove a transient unit works from a non-login shell, which is how the broker will reach it:

```bash
ssh tig-adi 'echo "{\"repo\":\"nope\",\"hours\":1}" | SSH_ORIGINAL_COMMAND=run_fleet ~/.local/bin/dispatch'
```

Expected: `{"error": "nope has no fleet.toml"}`. And a real unit:

```bash
ssh tig-adi 'python3 - <<EOF
import os, sys; sys.path.insert(0, os.path.expanduser("~/TIG/hermes-harness/box")); import boxlib
print(boxlib.start_night("test", os.path.expanduser("~"), ["/bin/sh", "-c", "echo hello; sleep 2"], 60, dict(os.environ)))
EOF
sleep 4; echo "{}" | SSH_ORIGINAL_COMMAND=status ~/.local/bin/dispatch'
```

Expected: an id `test-...`, then a status list with that id as `done`. If `systemd-run` reports "Failed to connect to bus", the `XDG_RUNTIME_DIR` line in `dispatch` is wrong for this box: read `loginctl show-user adi -p RuntimePath` and fix.

- [ ] **Step 7: Commit**

```bash
git add box/night.py box/night-config.example.toml box/units/fleet-night.service box/units/fleet-night.timer box/units/talos-night.service box/units/talos-night.timer box/install.sh box/tests/test_night_config.py
git commit -m "feat(box): night timers, the night config, and install.sh"
git push
```

---

### Task 6: The executor image on arm64 and one task by hand

08-05 plan Task 4, moved to the new box. Spec §5 "run_task keeps its container".

**Files:**
- Modify: `executor/Dockerfile` (no content change expected; confirms arm64 build)

**Interfaces:**
- Produces: image `hermes-exec:latest` on the box; `/etc/hermes-exec/github-token` (0600 adi); a proven `run_task` verb.

- [ ] **Step 1: Build the image on the box**

```bash
ssh tig-adi 'cd ~/TIG/hermes-harness && docker build -t hermes-exec:latest -f executor/Dockerfile executor && docker run --rm hermes-exec:latest claude --version && docker run --rm hermes-exec:latest gh --version | head -1 && docker image inspect hermes-exec:latest --format "{{.Architecture}}"'
```

Expected: a Claude Code version, a gh version, and `arm64`.

- [ ] **Step 2: Install the GitHub token**

Create a fine-grained PAT for `hermes-harness` and `fleet-fixture` only, `Contents: read and write`, `Pull requests: read and write`. Then:

```bash
ssh tig-server 'install -m 0600 -o adi -g adi /dev/null /etc/hermes-exec/github-token'
ssh -t tig-server 'nano /etc/hermes-exec/github-token'
```

Never pass the token as an argument.

- [ ] **Step 3: Run one task end to end through the verb**

```bash
ssh tig-adi 'printf "%s" "{\"repo\":\"FibonAdithya/hermes-harness\",\"prompt\":\"Add one line to box/README.md saying the executor works on arm64. Create the file if it is missing. Commit, push, and open a PR.\",\"minutes\":15}" | SSH_ORIGINAL_COMMAND=run_task ~/.local/bin/dispatch'
```

Wait, then:

```bash
ssh tig-adi 'echo "{}" | SSH_ORIGINAL_COMMAND=status ~/.local/bin/dispatch; ID=$(ls -t ~/nights | grep ^task- | head -1); echo "{\"id\":\"$ID\",\"lines\":40}" | SSH_ORIGINAL_COMMAND=log ~/.local/bin/dispatch'
```

Expected: status `done`, a PR on hermes-harness authored by "Hermes Agent". Confirm the credential mount survived: the log must not contain `Not logged in` or `ANTHROPIC_API_KEY`. If Claude Code inside the container fails to refresh the token, copy the host login file to `/etc/hermes-exec/claude-credentials.json` (mode 0600 adi) and point `claude_creds` in `run-task.sh` at it; record which worked in the runbook (Task 13).

- [ ] **Step 4: Close the smoke PR and commit**

Close the smoke PR on GitHub without merging. Delete the branch. Commit nothing new unless `run-task.sh` changed in Step 3; if it did:

```bash
git add executor/run-task.sh
git commit -m "fix(executor): credentials path that survives token refresh on the box"
git push
```

---

### Task 7: Broker tools for the box

Spec §5. Replace the old `tasks.py` argv builders with a verb protocol, and add the seven tools.

**Files:**
- Create: `broker/hermes_broker/box.py`
- Delete: `broker/hermes_broker/tasks.py`, `broker/tests/test_tasks.py`
- Modify: `broker/hermes_broker/server.py`
- Create: `broker/tests/test_box.py`
- Modify: `broker/tests/test_gating.py`

**Interfaces:**
- Produces in `box.py`: `call(target: str, verb: str, args: dict, timeout: int = 60) -> dict` which runs `run_ssh(target, [verb], timeout, stdin_text=json.dumps(args))` and returns the parsed JSON object, or `{"error": ...}` when the reply is not JSON; `VERB_RE` shared with the box.
- Tools on the `compute` MCP server: `run_fleet(repo, hours=8)`, `run_talos(challenge, direction, iterations=30, backend="local")`, `run_task(repo, prompt, minutes=30)`, `add_repo(name)` (all gated on `tig-server`), `night_status()`, `night_log(night_id, lines=80)`, `list_repos()` (ungated). `task_status` and `task_log` are removed; `night_status` and `night_log` replace them.

- [ ] **Step 1: Write the failing tests**

`broker/tests/test_box.py`:

```python
import json

import pytest

from hermes_broker import box


def test_call_sends_verb_and_json_on_stdin(monkeypatch):
    seen = {}

    def fake_run_ssh(target, argv, timeout=60, stdin_text=None):
        seen.update(target=target, argv=argv, stdin=stdin_text, timeout=timeout)
        return 0, json.dumps({"id": "fleet-20260924-2300-abcd"}), ""

    monkeypatch.setattr(box, "run_ssh", fake_run_ssh)
    out = box.call("tig-server", "run_fleet", {"repo": "fleet-fixture", "hours": 8})
    assert seen["argv"] == ["run_fleet"]
    assert json.loads(seen["stdin"]) == {"repo": "fleet-fixture", "hours": 8}
    assert out == {"id": "fleet-20260924-2300-abcd"}


def test_call_refuses_a_bad_verb_locally():
    with pytest.raises(ValueError):
        box.call("tig-server", "bash -c id", {})


def test_call_reports_non_json_reply(monkeypatch):
    monkeypatch.setattr(box, "run_ssh", lambda *a, **k: (1, "garbage", "boom"))
    out = box.call("tig-server", "status", {})
    assert "error" in out
    assert "boom" in out["error"] or "garbage" in out["error"]


def test_call_reports_unreachable(monkeypatch):
    def dead(*a, **k):
        raise box.Unreachable("tig-server: timed out after 60s")

    monkeypatch.setattr(box, "run_ssh", dead)
    out = box.call("tig-server", "status", {})
    assert out["error"].startswith("tig-server unreachable")


def test_prompt_never_reaches_argv(monkeypatch):
    seen = {}
    monkeypatch.setattr(box, "run_ssh", lambda t, argv, timeout=60, stdin_text=None: (seen.update(argv=argv, stdin=stdin_text) or (0, "{}", "")))
    box.call("tig-server", "run_task", {"repo": "o/r", "prompt": "rm -rf / ; echo pwned", "minutes": 5})
    assert seen["argv"] == ["run_task"]
    assert "pwned" not in " ".join(seen["argv"])
    assert "pwned" in seen["stdin"]
```

Add to `broker/tests/test_gating.py`:

```python
def test_box_tools_are_gated_and_reads_are_not(store, monkeypatch):
    """Every box tool that starts something needs a grant; every read does not."""
    from hermes_broker import server

    monkeypatch.setattr(server, "_store", lambda: store)
    monkeypatch.setattr(server, "_target", lambda box: "tig-server")
    calls = []
    monkeypatch.setattr(server.box, "call", lambda t, v, a, timeout=60: calls.append(v) or {"id": "x", "nights": [], "lines": [], "repos": []})

    with pytest.raises(Locked):
        server.run_fleet.fn("fleet-fixture", 8)
    with pytest.raises(Locked):
        server.run_talos.fn("knapsack", "d", 3, "local")
    with pytest.raises(Locked):
        server.add_repo.fn("fleet")
    with pytest.raises(Locked):
        server.run_task.fn("o/r", "p", 5)
    assert calls == []

    server.night_status.fn()
    server.night_log.fn("fleet-20260924-2300-abcd", 10)
    server.list_repos.fn()
    assert calls == ["status", "log", "list_repos"]
```

`MCPServer` from `mcp` 2.x exposes the wrapped function as `.fn` on the tool object returned by `@mcp.tool()`. If the attribute is named differently in the installed version, read `python -c "from mcp.server import MCPServer; help(MCPServer.tool)"` and use that name in both the test and nowhere else.

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
cd broker && uv run pytest tests/test_box.py tests/test_gating.py -q
```

Expected: `ModuleNotFoundError: No module named 'hermes_broker.box'`.

- [ ] **Step 3: Implement box.py and the tools**

`broker/hermes_broker/box.py`:

```python
"""The verb protocol to the TIG box.

The broker's key on the box is bound to a forced command, so the only thing
that can be sent is a bare verb name; everything else travels as one JSON
object on stdin. Arguments composed by an agent that reads untrusted email
therefore never touch a command line or a shell.
"""

from __future__ import annotations

import json
import re

from .ssh import Unreachable, run_ssh

VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def call(target: str, verb: str, args: dict, timeout: int = 60) -> dict:
    if not VERB_RE.match(verb):
        raise ValueError(f"invalid verb: {verb!r}")
    try:
        code, out, err = run_ssh(target, [verb], timeout=timeout, stdin_text=json.dumps(args))
    except Unreachable as exc:
        return {"error": f"{target} unreachable: {exc}"}
    try:
        reply = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        return {"error": f"{verb} returned no json (exit {code}): {(err or out).strip()[:300]}"}
    if not isinstance(reply, dict):
        return {"error": f"{verb} returned {type(reply).__name__}, not an object"}
    if code != 0 and "error" not in reply:
        reply["error"] = f"{verb} exited {code}: {err.strip()[:300]}"
    return reply
```

In `server.py`: delete the import of `tasks` and the three functions `run_task`, `task_status`, `task_log`; add `from . import box` and these tools after `request_access`:

```python
@mcp.tool()
def run_fleet(repo: str, hours: int = 8) -> str:
    """Start a fleet night on the box against a checked-out repo. Requires a grant.

    The dollar budget comes from that repo's fleet.toml; hours is the hard stop.
    """
    require_grant(_store(), "tig-server", now=time.time())
    r = box.call(_target("tig-server"), "run_fleet", {"repo": repo, "hours": int(hours)})
    return r.get("error") or f"fleet night {r['id']} started on {repo}. Poll night_status()."


@mcp.tool()
def run_talos(challenge: str, direction: str, iterations: int = 30, backend: str = "local") -> str:
    """Start a Talos autoresearch run on the box. Requires a grant. backend is local or modal."""
    require_grant(_store(), "tig-server", now=time.time())
    r = box.call(_target("tig-server"), "run_talos",
                 {"challenge": challenge, "direction": direction, "iterations": int(iterations), "backend": backend})
    return r.get("error") or f"talos night {r['id']} started ({challenge}, {backend}). Poll night_status()."


@mcp.tool()
def run_task(repo: str, prompt: str, minutes: int = 30) -> str:
    """Run a coding task with Claude Code on the box, PR only. Requires a grant."""
    require_grant(_store(), "tig-server", now=time.time())
    _config().check_repo(repo)
    r = box.call(_target("tig-server"), "run_task", {"repo": repo, "prompt": prompt, "minutes": int(minutes)})
    return r.get("error") or f"task {r['id']} started on {repo}. Poll night_status(); read night_log('{r['id']}')."


@mcp.tool()
def add_repo(name: str) -> str:
    """Clone one of the owner's own GitHub repositories onto the box. Requires a grant."""
    require_grant(_store(), "tig-server", now=time.time())
    r = box.call(_target("tig-server"), "add_repo", {"name": name}, timeout=300)
    if "error" in r:
        return r["error"]
    return f"{r['repo']} is at {r['path']}" + (" (already there)" if r.get("already") else "")


@mcp.tool()
def night_status() -> str:
    """Every fleet, talos, and task run on the box: id, status, start time, exit code."""
    r = box.call(_target("tig-server"), "status", {}, timeout=30)
    if "error" in r:
        return r["error"]
    rows = r.get("nights", [])
    if not rows:
        return "no nights recorded"
    return "\n".join(f"{n['id']}  {n['status']:8} started {n['started']}  exit={n['exit_code']}" for n in rows)


@mcp.tool()
def night_log(night_id: str, lines: int = 80) -> str:
    """Tail of one night's log."""
    r = box.call(_target("tig-server"), "log", {"id": night_id, "lines": int(lines)}, timeout=30)
    return r.get("error") or "\n".join(r.get("lines", []))


@mcp.tool()
def list_repos() -> str:
    """Repositories checked out on the box."""
    r = box.call(_target("tig-server"), "list_repos", {}, timeout=30)
    return r.get("error") or ", ".join(r.get("repos", [])) or "none"
```

Then remove the old files:

```bash
git rm broker/hermes_broker/tasks.py broker/tests/test_tasks.py
```

- [ ] **Step 4: Run the whole broker suite**

```bash
cd broker && uv run pytest -q
```

Expected: all pass; nothing imports `tasks`.

- [ ] **Step 5: Commit**

```bash
git add broker/hermes_broker/box.py broker/hermes_broker/server.py broker/tests/test_box.py broker/tests/test_gating.py
git commit -m "feat(broker): box verbs — run_fleet, run_talos, add_repo, night_status, night_log, list_repos"
git push
```

---

### Task 8: fleet on the box

Spec §6. The fixture's policy is incomplete (MEASURED 2026-09-24: `fleet plan` in `fleet-fixture` raises `PolicyError: no role/agent table found`), so this task starts in the `fleet-fixture` repository.

**Files:**
- Modify (repo `fleet-fixture`): `docs/agent/task-classes.md`, `fleet.toml`

**Interfaces:**
- Produces: a `fleet-fixture` that passes `fleet plan`; fleet and fleet-fixture installed on the box; one supervised tick proven.

- [ ] **Step 1: Add the role table and the agent and price tables to fleet-fixture**

On the laptop, in `~/TIG/fleet-fixture`, on a branch `fleet/box-policy`, append to `docs/agent/task-classes.md`:

```markdown
## Who runs each role

| Role | Agent | Model |
|---|---|---|
| `implementer` | `claude` | `claude-sonnet-5` |
| `reviewer` | `claude` | `claude-sonnet-5` |
| `invariant-reviewer` | `claude` | `claude-sonnet-5` |
| `planner` | `claude` | `claude-sonnet-5` |
| `integrator` | `claude` | `claude-sonnet-5` |
| `scout` | `claude` | `claude-sonnet-5` |
```

Append to `fleet.toml`:

```toml
[agents.claude]
model_flag = "--model"
args = ["--permission-mode", "acceptEdits"]

# First-party API rates, USD per million tokens, read from
# platform.claude.com/docs/en/about-claude/pricing on 2026-09-24.
# cache_write is the 5-minute rate. Agents bill the subscription, not these
# rates; fleet still needs them to enforce fleet_usd.
[prices."claude-sonnet-5"]
input = 2.0
output = 10.0
cache_read = 0.2
cache_write = 2.5
```

Verify on the laptop:

```bash
cd ~/TIG/fleet-fixture && uv run --project ../fleet fleet plan 2>&1 | tail -5
```

Expected: a list of intents (or "idle backlog"), no traceback. Open a PR to `fleet-fixture` and merge it; fleet nights run from `master` there.

- [ ] **Step 2: Install fleet and the fixture on the box**

```bash
ssh tig-adi 'cd ~/TIG && gh repo clone FibonAdithya/fleet && gh repo clone FibonAdithya/fleet-fixture && cd fleet && uv sync --locked --dev && uv run fleet --help | head -3'
```

Expected: fleet's usage text.

- [ ] **Step 3: One supervised tick**

```bash
ssh tig-adi 'cd ~/TIG/fleet && ./fixtures/seed/seed_issues.sh'
ssh tig-adi 'cd ~/TIG/fleet-fixture && uv run --project ~/TIG/fleet fleet plan'
ssh tig-adi 'cd ~/TIG/fleet-fixture && timeout 900 uv run --project ~/TIG/fleet fleet run --once --new-run'
```

Expected: `fleet run: run_id=... (new run) spent=$0.00`, at least one dispatch, and a herdr pane appearing in `herdr --remote tig-adi`. If `HerdrExecutor._start` raises, the failure names the missing `[agents.<kind>]` or role; fix in the fixture PR, not on the box.

- [ ] **Step 4: Reset the fixture and record**

Follow "Resetting after a graded run" in `~/TIG/fleet/docs/runbook.md` to return the fixture backlog to idle. No commit in this repository for this task.

---

### Task 9: Talos on the box

Spec §7. Two directories, one live local run with a measured build time, Modal configured.

**Interfaces:**
- Produces: `~/talos-local` and `~/talos-modal` on the box, each with `talos.config.json`; a measured local build time recorded in the spec §7.

- [ ] **Step 1: Two checkouts at the laptop's commit**

```bash
LAPTOP_SHA=$(git -C ~/TIG/Talos rev-parse HEAD); echo $LAPTOP_SHA
ssh tig-adi "for d in talos-local talos-modal; do gh repo clone FibonAdithya/talos ~/\$d && git -C ~/\$d checkout -q $LAPTOP_SHA && cd ~/\$d && uv venv --python 3.10 .venv -q && uv pip install --python .venv/bin/python -e . -q && .venv/bin/talos --help | head -2; done"
```

Expected: Talos usage text twice.

- [ ] **Step 2: Fake run proves the install**

```bash
ssh tig-adi 'cd ~/talos-local && .venv/bin/talos run --challenge knapsack --direction demo --budget-iterations 3 --yes --fake | tail -3'
```

Expected: `Status: won ...`.

- [ ] **Step 3: Interactive setup, local first**

```bash
ssh -t tig-adi 'cd ~/talos-local && .venv/bin/talos setup'
```

Answers: backend `local`, provider `claude-cli`, model default, mode `single-shot`, CPUs and memory defaults. Setup pulls the dev image (13 GB) and does the warm-up build; leave the session attached or run it inside `herdr --remote tig-adi`.

```bash
ssh -t tig-adi 'cd ~/talos-modal && .venv/bin/talos setup'
```

Answers: backend `modal`, provider `claude-cli`, then the Modal token id and secret.

- [ ] **Step 4: One live local run, timed**

```bash
ssh tig-adi 'cd ~/talos-local && date -u +%FT%TZ && .venv/bin/talos run --challenge knapsack --direction "small safe edit: tune one constant" --budget-iterations 1 --yes 2>&1 | tail -15 && date -u +%FT%TZ'
```

Record the wall-clock from the two timestamps as MEASURED in the spec §7, replacing the ESTIMATE line, with the command above. Commit that edit:

```bash
git add docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md
git commit -m "docs(spec): measured local Talos build time on the ARM box"
git push
```

---

### Task 10: The wiki mirror and the morning briefing

Spec §9.

**Files:**
- Create: `box/units/wiki-mirror.service`, `box/units/wiki-mirror.timer`
- Modify: `box/install.sh` (no change needed: it copies every unit)

- [ ] **Step 1: Units**

`box/units/wiki-mirror.service`:

```ini
[Unit]
Description=Refresh the read-only hermes-wiki mirror

[Service]
Type=oneshot
ExecStart=/usr/bin/git -C %h/TIG/hermes-wiki fetch -q origin
ExecStart=/usr/bin/git -C %h/TIG/hermes-wiki reset -q --hard origin/main
```

`box/units/wiki-mirror.timer`:

```ini
[Unit]
Description=Wiki mirror every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
```

Check the wiki's default branch first; it is `main` (MEASURED 2026-09-24: `git push origin HEAD:main` in `hermes-wiki-sync.sh` on the droplet).

- [ ] **Step 2: Clone and enable on the box**

This timer is not a night, so it is enabled now:

```bash
ssh tig-adi 'gh repo clone FibonAdithya/hermes-wiki ~/TIG/hermes-wiki && cd ~/TIG/hermes-harness && git pull -q && box/install.sh && systemctl --user enable --now wiki-mirror.timer && systemctl --user start wiki-mirror.service && git -C ~/TIG/hermes-wiki log --oneline -1 && rg -c "" ~/TIG/hermes-wiki/index.md'
```

Expected: the wiki's latest commit and a line count for `index.md`.

- [ ] **Step 3: Extend the morning briefing**

On the droplet, the job `3bc4e11d29b1` gets a fourth section. `hermes cron edit --prompt` replaces the whole prompt, so pass the existing text plus the addition. Write the new prompt to a file first so nothing is quoted on a command line:

```bash
ssh hermes-vm 'cat > /tmp/morning.txt' <<'EOF'
Good morning briefing — deliver concisely.

1) CALENDAR: Use get_events to list today's calendar events. Summarize in a table.

2) INBOX TRIAGE: Use search_gmail_messages to get the 20 most recent inbox messages. For EACH email, read it with get_gmail_message_content, then ACT on it immediately:
   - DELETE (add TRASH): marketing, newsletters, promotions, social notifications, spam, storage warnings, onboarding drip emails, automated notifications that don't need action
   - ARCHIVE (remove INBOX): receipts, invoices, bills, past calendar invites, order confirmations, resolved conversations
   - FILE (add appropriate label OR remove INBOX): newsletters/promos that the user has explicitly labelled themselves (user-labelled as "old", "newsletter", "promo", etc.) — but if it's a standard unlabelled newsletter, DELETE it
   - KEEP (no action): emails from real people needing reply, active conversations, security alerts, travel bookings, legal docs, VC/recruiter outreach, time-sensitive applications

3) KEY EMAILS: After processing, report only the KEPT emails in a table (Sender | Subject | Why kept). Report totals: X deleted, Y archived, Z kept.

4) NIGHTS ON THE BOX: Call night_status(). For every night whose id is not yet on a wiki page under nights/, call night_log(id, 60), then write or extend the wiki page nights/<today's date YYYY-MM-DD>.md with one section per night: id, kind, status, exit code, and a three-line summary of what the log says happened (for fleet: tasks dispatched, PRs opened, spent; for talos: challenge, direction, best delta, won or not). Add the page to index.md if new. Then report the same in the briefing as a table. If night_status returns an error, say so verbatim; do not retry.

Be ruthless with deletions — anything automated and non-actionable goes. If unsure about an email, KEEP it and note it for review. Table format for all output.
EOF
ssh hermes-vm 'hermes cron edit 3bc4e11d29b1 --prompt "$(cat /tmp/morning.txt)" && rm /tmp/morning.txt && hermes cron list | grep -A3 morning-briefing'
```

`night_status` is an MCP tool, and MCP tools reach cron jobs only if the job's toolsets include them; if the first morning run reports the tool as unavailable, set `--workdir` and check `enabled_toolsets` on the job with `hermes cron edit --help`, and record what was needed in the runbook (Task 13). This step depends on the broker being deployed (Task 11); run it after that task.

- [ ] **Step 4: Commit**

```bash
git add box/units/wiki-mirror.service box/units/wiki-mirror.timer
git commit -m "feat(box): read-only wiki mirror timer"
git push
```

---

### Task 11: Deploy the broker to the droplet

08-05 plan Task 7, adjusted: the droplet gets a git checkout instead of an rsync, the SSH target is `adi` on the new box with the dispatch key, and the tool include list is the new one.

**Files:**
- Create (droplet): `~/.hermes/broker/broker.json`, `~/.ssh/config`, `~/.config/systemd/user/hermes-approvals.service`
- Modify (droplet): `~/.hermes/config.yaml`

- [ ] **Step 1: The dispatch key**

On the laptop:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tig_server_dispatch -N "" -C "hermes-broker-dispatch"
PUB=$(cat ~/.ssh/tig_server_dispatch.pub)
ssh tig-adi "printf '%s\n' 'command=\"/home/adi/.local/bin/dispatch\",no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding $PUB' >> ~/.ssh/authorized_keys"
ssh -i ~/.ssh/tig_server_dispatch -o IdentitiesOnly=yes adi@2.29.24.53 id; echo "rc=$?"
echo '{}' | ssh -i ~/.ssh/tig_server_dispatch -o IdentitiesOnly=yes adi@2.29.24.53 status
```

Expected: `id` prints `{"error": "invalid verb: ..."}` with `rc=2` (spec §13 item 2), and `status` prints the nights list.

- [ ] **Step 2: Make the repository public**

The droplet has no GitHub credential on the host, so it clones over plain HTTPS, which needs the repository public. That is also what branch protection (Task 12) needs on a Free plan. Scan history for secrets first; the repo has runbooks with hostnames but must hold no tokens:

```bash
git log -p --all | grep -n -i -E 'ghp_[A-Za-z0-9]{20,}|github_pat_|sk-ant-|sk-or-|xox[bp]-|[0-9]{9,10}:[A-Za-z0-9_-]{35}' | head
```

Expected: no output. If anything matches, stop: rotate that credential and rewrite history before continuing.

```bash
gh repo edit FibonAdithya/hermes-harness --visibility public --accept-visibility-change-consequences
gh repo view FibonAdithya/hermes-harness --json visibility -q .visibility
```

Expected: `PUBLIC`.

- [ ] **Step 3: Key, SSH config, checkout, and config on the droplet**

```bash
ssh hermes-vm 'install -d -m 0700 ~/.ssh'
cat ~/.ssh/tig_server_dispatch | ssh hermes-vm 'install -m 0600 /dev/stdin ~/.ssh/tig_server_dispatch'
ssh hermes-vm 'cat >> ~/.ssh/config <<EOF
Host tig-server
    HostName 2.29.24.53
    User adi
    IdentityFile ~/.ssh/tig_server_dispatch
    IdentitiesOnly yes
    ConnectTimeout 15
EOF
chmod 600 ~/.ssh/config
git clone -q https://github.com/FibonAdithya/hermes-harness.git ~/hermes-harness && cd ~/hermes-harness && git checkout -q feat/tig-box && cd broker && uv sync --no-dev -q && echo synced
install -d -m 0700 ~/.hermes/broker/state
echo "{}" | ssh -o BatchMode=yes tig-server status'
```

Expected: `synced` and a nights list from the box. The `tig-gpu` entry from the 08-05 plan is added only when that box is in use again.

Write `~/.hermes/broker/broker.json` over `ssh -t hermes-vm nano`, mode 0600, contents:

```json
{
  "owner_telegram_id": 8887384436,
  "approvals_bot_token": "PASTE_APPROVALS_BOT_TOKEN",
  "repos": ["FibonAdithya/hermes-harness", "FibonAdithya/fleet-fixture"],
  "ssh_targets": {"tig-gpu": "tig-gpu", "tig-server": "tig-server"},
  "state_dir": "/home/hermes/.hermes/broker/state"
}
```

The approvals bot is created with BotFather as the 08-05 plan Task 7 Step 1 describes; the owner id above is the one `TELEGRAM_ALLOWED_USERS` already holds (MEASURED 2026-09-24 from the morning-briefing job's `deliver` field).

- [ ] **Step 4: Register the MCP server and the approvals service**

Back up, then add under `mcp_servers:` in `~/.hermes/config.yaml` as a sibling of `google:`:

```yaml
  compute:
    command: /usr/local/bin/uv
    args: [run, --directory, /home/hermes/hermes-harness/broker, python, -m, hermes_broker.server]
    enabled: true
    env:
      HERMES_BROKER_CONFIG: /home/hermes/.hermes/broker/broker.json
    tools:
      include:
        - request_access
        - run_fleet
        - run_talos
        - run_task
        - add_repo
        - night_status
        - night_log
        - list_repos
        - submit_job
        - job_status
        - job_logs
        - job_cancel
        - fetch_artifacts
```

Validate: `ssh hermes-vm 'python3 -c "import yaml;c=yaml.safe_load(open(\"/home/hermes/.hermes/config.yaml\"));print(sorted(c[\"mcp_servers\"].keys()))"'` prints `['compute', 'google']`.

`~/.config/systemd/user/hermes-approvals.service` on the droplet:

```ini
[Unit]
Description=Hermes approvals bot (issues time-boxed compute grants)
After=network-online.target

[Service]
Type=simple
Environment=HERMES_BROKER_CONFIG=/home/hermes/.hermes/broker/broker.json
ExecStart=/usr/local/bin/uv run --directory /home/hermes/hermes-harness/broker python -m hermes_broker.listen
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
ssh hermes-vm 'systemctl --user daemon-reload && systemctl --user enable --now hermes-approvals && systemctl --user restart hermes-gateway && sleep 20 && systemctl --user is-active hermes-approvals hermes-gateway && journalctl --user -u hermes-gateway -n 40 --no-pager | grep -i -E "mcp|compute|error" | tail -10'
```

Expected: both `active`. In Telegram: "list your tools" shows the thirteen `compute` tools. Verify through Telegram, not the CLI (As-built #13).

- [ ] **Step 5: Standing instructions**

Add to `~/.hermes/memories/MEMORY.md` on the droplet:

```
## The TIG box (compute MCP server)
- Ask first: request_access("tig-server", minutes, reason) returns a code; I approve it in the approvals chat. Never claim you already have access.
- run_fleet(repo, hours) and run_talos(challenge, direction, iterations, backend) start nights; they return an id at once. Poll night_status(); read night_log(id).
- run_task(repo, prompt, minutes) runs Claude Code in a container on the box and opens a PR; I merge. To add a new box verb or broker tool, run_task against FibonAdithya/hermes-harness — it deploys itself after I merge.
- add_repo(name) clones one of my own repos onto the box.
- Never schedule any of these; cron cannot get a grant. night_status and night_log are fine from cron.
- Results go on wiki pages under nights/.
```

Send `/new` in Telegram afterwards so the next session loads it (As-built #21).

No commit for this task: everything it changes lives on the droplet.

---

### Task 12: Self-extension: CI, public repo, branch protection, pull timers

Spec §10. The gate and the deploy.

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `box/units/harness-pull.service`, `box/units/harness-pull.timer`
- Create: `droplet/harness-pull.sh`, `droplet/units/harness-pull.service`, `droplet/units/harness-pull.timer`

- [ ] **Step 1: CI**

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [master]
permissions:
  contents: read
jobs:
  broker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: cd broker && uv sync --dev && uv run pytest -q
  box:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pytest && cd box && python -m pytest -q
      - run: sudo apt-get install -y -qq shellcheck && shellcheck box/*.sh executor/run-task.sh $(ls droplet/*.sh 2>/dev/null)
      - run: |
          for f in box/verbs/*; do
            [ -x "$f" ] || { echo "$f is not executable"; exit 1; }
            case "$(head -1 "$f")" in "#!/usr/bin/env python3") python -m py_compile "$f";; *) ;; esac
          done
```

Push it over SSH, not HTTPS: the gh token lacks the `workflow` scope and HTTPS will refuse a workflow file (standing instruction).

```bash
git add .github/workflows/ci.yml
git commit -m "ci: broker tests, box tests, shellcheck, verb checks"
git push
gh pr create --base master --head feat/tig-box --title "The TIG box: fleet, Talos, self-extension behind the broker" --body "$(cat <<'EOF'
Implements docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr checks --watch
```

Expected: both jobs green. Do not merge yet.

- [ ] **Step 2: Protect master**

The repository is public since Task 11 Step 2, so protection is available on the Free plan.

```bash
gh api -X PUT repos/FibonAdithya/hermes-harness/branches/master/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'EOF'
{"required_status_checks":{"strict":true,"contexts":["broker","box"]},
 "enforce_admins":true,
 "required_pull_request_reviews":{"required_approving_review_count":0},
 "restrictions":null,"allow_force_pushes":false,"allow_deletions":false}
EOF
gh api repos/FibonAdithya/hermes-harness -X PATCH -F allow_auto_merge=false
gh api repos/FibonAdithya/hermes-harness/branches/master/protection -q '.required_status_checks.contexts'
```

Expected: `["broker","box"]`.

Prove it holds from the executor (spec §13 item 10):

```bash
ssh tig-adi 'docker run --rm -e GH_TOKEN="$(cat /etc/hermes-exec/github-token)" hermes-exec:latest bash -lc "
  git clone --depth 1 https://github.com/FibonAdithya/hermes-harness.git r && cd r
  echo probe >> README.md && git commit -qam probe && git push origin HEAD:master"' 2>&1 | tail -3
```

Expected: `remote: error: GH006: Protected branch update failed`. If the push succeeds, protection is not real and this task is not done.

Do the same for `fleet-fixture` (`main` there), since fleet's integrator pushes to it.

- [ ] **Step 3: Pull timers on both hosts**

`box/units/harness-pull.service`:

```ini
[Unit]
Description=Deploy hermes-harness master to the box

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash -c 'cd %h/TIG/hermes-harness && git fetch -q origin && [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)" ] || { git checkout -q master && git reset -q --hard origin/master && box/install.sh; }'
```

`box/units/harness-pull.timer`:

```ini
[Unit]
Description=Deploy hermes-harness every 15 minutes

[Timer]
OnCalendar=*:2/15
Persistent=true

[Install]
WantedBy=timers.target
```

`droplet/harness-pull.sh`:

```bash
#!/usr/bin/env bash
# Deploy hermes-harness master on the droplet. A broker change takes effect
# for the approvals bot at once and for the MCP server at the next gateway
# restart (the 04:00 reset, or `systemctl --user restart hermes-gateway`).
set -euo pipefail
cd "$HOME/hermes-harness"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)" ] && exit 0
git checkout -q master && git reset -q --hard origin/master
(cd broker && /usr/local/bin/uv sync --no-dev -q)
systemctl --user restart hermes-approvals
echo "deployed $(git rev-parse --short HEAD)"
```

`droplet/units/harness-pull.service` and `.timer`: the same shape as the box units, with `ExecStart=%h/hermes-harness/droplet/harness-pull.sh` and `Description=Deploy hermes-harness to the droplet`.

```bash
chmod +x droplet/harness-pull.sh
git add box/units/harness-pull.service box/units/harness-pull.timer droplet/harness-pull.sh droplet/units/harness-pull.service droplet/units/harness-pull.timer
git commit -m "feat: harness-pull timers — merge to master is deploy on both hosts"
git push
gh pr checks --watch
```

- [ ] **Step 4: Merge, then install the timers from master**

```bash
gh pr merge --squash --delete-branch
ssh tig-adi 'cd ~/TIG/hermes-harness && git checkout -q master && git pull -q && box/install.sh && systemctl --user enable --now harness-pull.timer && systemctl --user list-timers --no-pager | grep -E "harness|wiki"'
ssh hermes-vm 'cd ~/hermes-harness && git checkout -q master && git pull -q && cp droplet/units/harness-pull.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now harness-pull.timer && systemctl --user restart hermes-approvals && systemctl --user is-active hermes-approvals'
```

Expected: both timers listed, `active`.

- [ ] **Step 5: The round trip (spec §13 item 9)**

In Telegram: "request access to tig-server for 20 minutes to add a `uptime` verb, then run_task on FibonAdithya/hermes-harness: add box/verbs/uptime that prints {"uptime": <output of uptime -p>} as JSON, executable, with a test in box/tests." Approve the code in the approvals chat. When the PR appears, check CI is green, merge it. Within 15 minutes:

```bash
echo '{}' | ssh -i ~/.ssh/tig_server_dispatch -o IdentitiesOnly=yes adi@2.29.24.53 uptime
```

Expected: `{"uptime": "up ..."}` without anyone touching the box. The broker needs no change for a box-only verb; Hermes can call it once a broker tool exists for it, which is the next PR through the same loop.

---

### Task 13: The dry night and the runbook

Spec §13 items 3, 4, 8, 11, 12, and the runbook update.

**Files:**
- Modify: `runbooks/dispatch.md` (this file has pre-existing uncommitted edits; add a section at the end and stage only after confirming with `git diff` that the pre-existing hunks are the owner's and are meant to go in, or ask)
- Modify: `docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md` (as-built corrections)

- [ ] **Step 1: Locked by default, and cron cannot start a night**

In Telegram: "start a fleet night on fleet-fixture". Expected: `LOCKED: no active grant for tig-server` and the assistant asks. Then:

```bash
ssh hermes-vm 'hermes cron create --name night-probe --schedule "every 1h" --prompt "Call request_access for tig-server for 5 minutes, reason probe, then immediately call run_fleet on fleet-fixture for 1 hour. Then call night_status and report it." --deliver telegram:8887384436 --model deepseek/deepseek-v4-pro --provider nous && hermes cron run night-probe'
```

Expected in the delivered output: `LOCKED`, followed by a night_status listing. Remove the probe: `hermes cron remove night-probe`.

- [ ] **Step 2: Dry night with tiny budgets**

On the box, set `~/nights/config.toml` to `fleet.hours = 1`, `talos.iterations = 2`, one queue entry. Then, instead of waiting for 23:00:

```bash
ssh tig-adi 'systemctl --user start fleet-night.service talos-night.service; sleep 5; echo "{}" | SSH_ORIGINAL_COMMAND=status ~/.local/bin/dispatch'
```

Expected: two nights `running`. After they finish (Talos: two builds, about 30 minutes; fleet: the hour limit or an idle backlog), status shows `done`, `failed`, or `killed`, never `running`, and `~/nights/config.toml` has one fewer queue entry. The next morning briefing must report both on a `nights/<date>.md` page (spec §13 item 8). Then start the second Talos night with an empty queue and confirm status `skipped`.

- [ ] **Step 3: Enable the timers**

Only now:

```bash
ssh tig-adi 'systemctl --user enable --now fleet-night.timer talos-night.timer && systemctl --user list-timers --no-pager | grep night'
```

And set real budgets in `~/nights/config.toml`.

- [ ] **Step 4: Subscription, not API (spec §13 item 12)**

```bash
ssh tig-adi 'grep -c -- "--bare" ~/.local/bin/run-task.sh; grep -c ANTHROPIC_API_KEY ~/.local/bin/run-task.sh; systemctl --user show-environment | grep -c ANTHROPIC || true'
```

Expected: `0`, `0`, `0`.

- [ ] **Step 5: Runbook and as-built notes**

Append to `runbooks/dispatch.md`:

````markdown
## The TIG box

The box is `tig-adi` from the laptop (user `adi`). Hermes reaches it only as
the dispatch key, which can run `~/.local/bin/dispatch` and nothing else.

```bash
ssh tig-adi 'echo "{}" | SSH_ORIGINAL_COMMAND=status ~/.local/bin/dispatch'   # what the broker sees
ssh tig-adi 'ls ~/nights; cat ~/nights/<id>/status; tail -50 ~/nights/<id>/log'
ssh tig-adi 'systemctl --user list-timers --no-pager'                          # nights, wiki mirror, harness pull
ssh tig-adi 'systemctl --user stop night-<id>.service'                         # kill a night; status becomes killed
```

- **A verb is missing.** It is a file in `box/verbs` on `master`; `harness-pull`
  installs `master` every 15 minutes. `journalctl --user -u harness-pull -n 20`.
- **Nothing ran last night.** `systemctl --user list-timers` shows whether the
  timers are enabled; they ship disabled. `~/nights/config.toml` holds the
  inputs; an empty Talos queue is a `skipped` night, not an error.
- **The dispatcher says "Failed to connect to bus".** `XDG_RUNTIME_DIR` for
  `adi` is not `/run/user/<uid>`; read `loginctl show-user adi -p RuntimePath`.
- **Rotating the dispatch key.** New keypair on the laptop; replace the
  `command=...` line in `~adi/.ssh/authorized_keys` and the key in
  `~/.ssh/tig_server_dispatch` on the droplet.
- **Rotating the executor's GitHub token.** `ssh -t tig-server 'nano /etc/hermes-exec/github-token'`.
````

Append an "As-built corrections (2026-09-…)" section to the spec listing every deviation met while executing, in the style of the 07-28 spec, with the measured Talos build time and whichever Claude credential path worked in Task 6.

```bash
git add runbooks/dispatch.md docs/ai/specs/2026-09-24-tig-box-fleet-talos-design.md
git commit -m "docs: TIG box runbook section and as-built corrections"
git push
```

Open a PR to master for this, since master is protected now, and merge after CI.

---

## Self-review

**Spec coverage.** §1 machines: Task 1. §2 planes: Tasks 4, 6, 11 (what lives where). §3 provisioning: Task 1. §4 dispatcher: Tasks 2, 3, 5 (install). §5 broker tools and allowlists: Tasks 4, 7. §6 fleet: Task 8. §7 Talos: Task 9. §8 nights: Tasks 5, 13. §9 wiki: Task 10. §10 self-extension: Task 12. §11 failure modes: `run-night.sh` (killed), `box.call` (unreachable), Task 13 runbook. §12 cost: fixture `fleet_usd` and Talos iterations in `config.toml`. §13 verification: items 1, 2, 5, 6, 7 in Tasks 1, 11, 4, 9, 8; items 3, 4, 8, 11, 12 in Task 13; items 9, 10 in Task 12.

**Placeholders.** `PASTE_APPROVALS_BOT_TOKEN` is a value the owner types into a 0600 file, not a plan gap. No other placeholders.

**Type consistency.** `boxlib.start_night(kind, workdir, argv, runtime_sec, env, meta)` is used the same way in `run_fleet`, `run_talos`, and `night.py`. `box.call(target, verb, args, timeout)` matches every tool. Night ids match `NIGHT_ID_RE` in the box and are passed through unchanged by the broker. The `status` verb is exposed as `night_status` and `log` as `night_log` on the broker; the runbook uses the verb names because it talks to the box.

**Review Focus.** Item 1 in `test_shell_like_command_is_refused`; item 2 in `test_log_verb_tails_and_refuses_bad_ids`; item 3 in `test_oversized_stdin_is_refused`; item 4 in `test_add_repo_check`; item 5 in `test_pop_talos_on_empty_queue_is_none` and Task 13 Step 2's empty-queue run.
