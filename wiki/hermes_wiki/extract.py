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


def _opted_out(cwd: str, home: str) -> bool:
    d = Path(cwd)
    while True:
        if (d / ".no-wiki-notes").exists():
            return True
        if (d / ".git").exists() or str(d) == home or d == d.parent:
            return False
        d = d.parent


def skip_reason(hook: dict, env: Mapping[str, str], owner_msgs: int) -> str | None:
    cwd = hook.get("cwd") or ""
    if env.get("WIKI_NOTE_CHILD") == "1":
        return "child"
    if cwd.startswith("/tmp/") or "pytest-of-" in cwd:
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
    with open(d / f"drain-{session}.log", "ab") as log:
        subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
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


def _run_one(cfg: WikiConfig, pending: Path, run) -> tuple[str, bool]:
    head, _, body = pending.read_text(encoding="utf-8").partition("\n")
    try:
        session = str(json.loads(head)["session"])
    except (ValueError, KeyError, TypeError):
        session = pending.stem
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
                input=load_prompt() + "\n\n" + body,
                env={**os.environ, "WIKI_NOTE_CHILD": "1"},
                capture_output=True, text=True, timeout=900,
            )
            ok = proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as e:
            _log_error(cfg, f"drain {session}: {type(e).__name__}: {e}")
            ok = False
    finally:
        conf.unlink(missing_ok=True)
    if ok:
        pending.unlink(missing_ok=True)
    return session, ok


def _locked_drain(cfg: WikiConfig, pick, run) -> list[tuple[str, bool]]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "drain.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        return [_run_one(cfg, p, run) for p in pick()]


def drain(cfg: WikiConfig, limit: int = 4, run=subprocess.run) -> list[tuple[str, bool]]:
    def pick() -> list[Path]:
        files = list((cfg.state_dir / "pending").glob("*.txt"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]

    return _locked_drain(cfg, pick, run)


def extract_dir(cfg: WikiConfig, projects: Path, session: str, run=subprocess.run) -> bool:
    if not _SESSION_OK.fullmatch(session):
        return False
    files = [p for p in Path(projects).rglob("*.jsonl") if p.is_file()]
    if not files:
        return False
    newest = max(files, key=lambda p: p.stat().st_mtime)
    pending = _write_pending(cfg, session, "", transcript.condense(newest))
    res = _locked_drain(cfg, lambda: [pending], run)
    return bool(res) and res[0][1]
