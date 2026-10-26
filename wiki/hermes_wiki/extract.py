"""Turn finished coding sessions into wiki inbox notes via a headless `claude -p`."""
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from importlib import resources
from pathlib import Path
from typing import Mapping

from . import transcript
from .config import WikiConfig

_SESSION_OK = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_TOOLS = ["mcp__wiki__search", "mcp__wiki__read", "mcp__wiki__list_tags", "mcp__wiki__add_note"]


def load_prompt() -> str:
    return resources.files("hermes_wiki").joinpath("prompts", "extract.md").read_text(encoding="utf-8")


def _is_tmp(cwd: str) -> bool:
    return cwd == "/tmp" or cwd.startswith("/tmp/") or "pytest-of-" in cwd


def _main_checkout(git_file: Path) -> Path | None:
    """For a worktree's `.git` file (`gitdir: <main>/.git/worktrees/<name>`), return <main>."""
    try:
        text = git_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            gitdir = Path(line[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = git_file.parent / gitdir
            gitdir = Path(os.path.normpath(gitdir))
            for d in (gitdir, *gitdir.parents):
                if d.name == ".git":
                    return d.parent
            return None
    return None


def _opted_out(cwd: str, home: str) -> bool:
    """Walk up from cwd; stop only at a `.git` directory, $HOME or /.

    A `.git` file marks a git worktree: its main checkout's opt-out applies too.
    """
    d = Path(cwd)
    while True:
        if (d / ".no-wiki-notes").exists():
            return True
        git = d / ".git"
        if git.is_file():
            main = _main_checkout(git)
            if main is not None and (main / ".no-wiki-notes").exists():
                return True
        if git.is_dir() or str(d) == home or d == d.parent:
            return False
        d = d.parent


def skip_reason(hook: dict, env: Mapping[str, str], owner_msgs: int) -> str | None:
    cwd = hook.get("cwd") or ""
    if env.get("WIKI_NOTE_CHILD") == "1":
        return "child"
    if _is_tmp(cwd):
        return "tmp"
    if cwd and _opted_out(cwd, env.get("HOME") or str(Path.home())):
        return "opted-out"
    tp = hook.get("transcript_path")
    if not tp or not Path(tp).is_file():
        return "no-transcript"
    if owner_msgs < 4:
        return "short"
    return None


def claude_argv(mcp_config: Path, model: str, claude: str = "claude") -> list[str]:
    return [
        claude, "-p", "--model", model,
        "--settings", '{"disableAllHooks": true}',
        "--strict-mcp-config", "--mcp-config", str(mcp_config),
        "--tools", "",
        "--allowedTools", *_TOOLS,
    ]


def write_mcp_config(cfg: WikiConfig, session: str, path: Path) -> None:
    conf = {"mcpServers": {"wiki": {
        "command": cfg.bin,
        "args": ["mcp", "--config", str(cfg.path)],
        "env": {"HERMES_WIKI_SESSION": session},
    }}}
    path.write_text(json.dumps(conf), encoding="utf-8")


def _log_error(cfg: WikiConfig, what: str) -> None:
    try:
        d = cfg.state_dir / "log"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hook-errors.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {what}\n")
    except Exception:
        pass


def _spawn_detached(argv: list[str], cfg: WikiConfig | None = None, session: str = "x") -> None:
    state = cfg.state_dir if cfg else Path(tempfile.gettempdir())
    d = state / "log"
    d.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    with open(d / f"drain-{session}.log", "ab") as log:
        subprocess.Popen(argv, cwd=state, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT)


def _write_pending(cfg: WikiConfig, session: str, cwd: str, text: str) -> Path:
    d = cfg.state_dir / "pending"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.txt"
    tmp = d / f".{session}.tmp"
    tmp.write_text(json.dumps({"session": session, "cwd": cwd}) + "\n" + text, encoding="utf-8")
    os.replace(tmp, p)
    return p


def session_end(stdin_text: str, env: Mapping, cfg: WikiConfig, spawn=None) -> str:
    try:
        hook = json.loads(stdin_text)
        if not isinstance(hook, dict):
            raise ValueError("hook input is not a JSON object")
        session = str(hook.get("session_id", ""))
        if not _SESSION_OK.fullmatch(session):
            raise ValueError("bad session_id")
        tp = hook.get("transcript_path")
        n = transcript.owner_messages(Path(tp)) if tp and Path(tp).is_file() else 0
        reason = skip_reason(hook, env, n)
        if reason:
            return reason
        _write_pending(cfg, session, hook.get("cwd") or "", transcript.condense(Path(tp)))
        argv = [cfg.bin, "drain", "--limit", "4", "--config", str(cfg.path)]
        if spawn is None:
            _spawn_detached(argv, cfg, session)
        else:
            spawn(argv)
        return "queued"
    except Exception as e:
        _log_error(cfg, f"session-end: {type(e).__name__}: {e}")
        return "error"


_STALE_SECONDS = 7 * 86400
_LOG_TAIL = 2000


def _drain_log(cfg: WikiConfig, text: str) -> None:
    try:
        d = cfg.state_dir / "log"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "drain.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n")
    except Exception:
        pass


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env["WIKI_NOTE_CHILD"] = "1"
    return env


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_ino, st.st_mtime_ns


def _run_one(cfg: WikiConfig, pending: Path, run) -> tuple[str, bool]:
    with open(pending, encoding="utf-8") as fh:
        # The hook may replace this file while the extractor runs; only the copy read here is done.
        seen = _identity(os.fstat(fh.fileno()))
        head, _, body = fh.read().partition("\n")
    cwd = ""
    try:
        meta = json.loads(head)
        session, cwd = str(meta["session"]), str(meta.get("cwd") or "")
    except (ValueError, KeyError, TypeError, AttributeError):
        session = pending.stem
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = cfg.state_dir / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".json", prefix="mcp-", dir=tmpdir)
    os.close(fd)
    conf = Path(name)
    try:
        write_mcp_config(cfg, session, conf)
        try:
            proc = run(
                claude_argv(conf, cfg.model, cfg.claude),
                input=load_prompt() + "\n\n" + f"Session directory: {cwd}\n\n" + body,
                env=_child_env(), cwd=cfg.state_dir, encoding="utf-8",
                capture_output=True, text=True, timeout=900,
            )
            ok = proc.returncode == 0
            _drain_log(cfg, f"session={session} rc={proc.returncode} "
                            f"stdout={(proc.stdout or '')[-_LOG_TAIL:]!r} stderr={(proc.stderr or '')[-_LOG_TAIL:]!r}")
        except (subprocess.TimeoutExpired, OSError) as e:
            _log_error(cfg, f"drain {session}: {type(e).__name__}: {e}")
            _drain_log(cfg, f"session={session} rc=none error={type(e).__name__}")
            ok = False
    finally:
        conf.unlink(missing_ok=True)
    if ok:
        try:
            if _identity(pending.stat()) == seen:
                pending.unlink()
        except FileNotFoundError:
            pass
    return session, ok


def _locked_drain(cfg: WikiConfig, pick, run) -> list[tuple[str, bool]]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "drain.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        results = []
        for p in pick():
            try:
                results.append(_run_one(cfg, p, run))
            except Exception as e:
                _log_error(cfg, f"drain {p.name}: {type(e).__name__}: {e}")
                results.append((p.stem, False))
        return results


def drain(cfg: WikiConfig, limit: int = 4, run=subprocess.run) -> list[tuple[str, bool]]:
    def pick() -> list[Path]:
        now = time.time()
        files = []
        for p in (cfg.state_dir / "pending").glob("*.txt"):
            try:
                if now - p.stat().st_mtime > _STALE_SECONDS:
                    p.unlink()
                    _drain_log(cfg, f"session={p.stem} deleted stale pending file (older than 7 days)")
                else:
                    files.append(p)
            except OSError:
                continue
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]

    return _locked_drain(cfg, pick, run)


def extract_dir(cfg: WikiConfig, projects: Path, session: str, run=subprocess.run) -> bool:
    if not _SESSION_OK.fullmatch(session):
        return False
    files = [p for p in Path(projects).glob("*/*.jsonl") if p.is_file()]
    if not files:
        return False
    newest = max(files, key=lambda p: p.stat().st_mtime)
    _write_pending(cfg, session, "", transcript.condense(newest))
    return (session, True) in drain(cfg, limit=4, run=run)
