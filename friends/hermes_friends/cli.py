"""friends command line. All side effects go through the System object."""
import argparse
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import render, validate
from .model import Friend
from .owner import owner_google_emails, read_owner_env
from .policy import (CADDY_FILE_MODE, DEFAULT_CADDY_DIR, DEFAULT_CAP_USD, MEM_FLOOR_MB,
                     MEM_PER_FRIEND_MB, PUBLIC_HOST, gateway_unit)
from .registry import allocate_port, bot_token_in_use, list_friends, profile_dir
from .system import OWNER_ROOT, System


class Refused(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _write_profile_files(system, pdir: Path, f: Friend, token: str | None, owner) -> None:
    system.write_file(pdir / "config.yaml", render.render_config(f), 0o600)
    if token is not None:
        system.write_file(pdir / ".env", render.render_env(f, token, owner), 0o600)
        system.write_file(pdir / "SOUL.md", render.render_soul(f), 0o600)
        for rel, data in render.skill_files().items():
            system.write_file(pdir / "skills" / rel, data, 0o600)
    system.write_file(pdir / "memories" / "MEMORY.md", render.render_memory(f), 0o600)
    system.write_file(pdir / "friend.json", json.dumps(asdict(f), indent=2) + "\n", 0o600)


def _undo_caddy(system, caddy_file: Path):
    def undo():
        if caddy_file.exists():
            system.remove_tree(caddy_file)
        system.caddy_reload()
    return undo


def cmd_add(a, system, root: Path, caddy_dir: Path) -> int:
    try:
        name = validate.validate_name(a.name)
        tid = validate.validate_telegram_id(a.telegram_id)
        token = validate.validate_bot_token(a.bot_token)
        tz = validate.validate_tz(a.tz)
        email = validate.validate_email(a.email).lower()
        if not math.isfinite(a.cap) or a.cap <= 0:
            raise ValueError("cap must be a positive finite number")
        owner = read_owner_env(root)
    except ValueError as e:
        _err(str(e))
        return 2
    if email.lower() in owner_google_emails(root, root.parent):
        _err("that email is one of the owner's Google accounts; use the friend's own")
        return 1
    pdir = profile_dir(root, name)
    if pdir.exists():
        _err(f"profile {name} already exists")
        return 1
    if bot_token_in_use(root, token):
        _err("that bot token is already used by another bot")
        return 1
    if system.mem_available_mb() - MEM_PER_FRIEND_MB < MEM_FLOOR_MB:
        _err("not enough free memory for another friend")
        return 1

    f = Friend(name=name, telegram_id=tid, email=email, tz=tz,
               port=allocate_port(list_friends(root)), cap_usd=a.cap, activated=False)
    caddy_file = Path(caddy_dir) / f"{name}.caddy"
    undo: list = []  # run in reverse on failure
    try:
        undo.append(lambda: pdir.exists() and system.remove_tree(pdir))
        system.hermes(name, "profile", "create", name, "--no-alias", "--no-skills")
        undo.append(lambda: system.hermes(name, "profile", "delete", "-y", name))
        system.chmod(pdir, 0o700)
        _write_profile_files(system, pdir, f, token, owner)
        undo.append(_undo_caddy(system, caddy_file))
        system.write_file(caddy_file, render.render_caddy(f), CADDY_FILE_MODE)
        system.caddy_reload()
        undo.append(lambda: system.hermes(name, "gateway", "uninstall"))
        system.hermes(name, "gateway", "install", "--start-now", "--start-on-login")
    except Exception as e:
        _err(f"add failed: {e}; rolling back")
        for step in reversed(undo):
            try:
                step()
            except Exception as e2:
                _err(f"rollback step failed: {e2}")
        return 1

    print(f"Created friend {name} on port {f.port}; gateway {gateway_unit(name)} started.")
    print(f"Add this redirect URI to the Google Cloud OAuth Web client:\n"
          f"  https://{PUBLIC_HOST}/{name}/oauth2callback")
    print(f"Tell {name} to send /start to their bot.")
    return 0


def _friend_dir(root: Path, name: str) -> Path:
    """Validated profile dir of an existing friend; raises Refused otherwise."""
    try:
        validate.validate_name(name)
    except ValueError as e:
        raise Refused(str(e), 2) from None
    pdir = profile_dir(root, name)
    if not (pdir / "friend.json").is_file():
        raise Refused(f"no such friend: {name}", 1)
    return pdir


def cmd_activate(a, system, root: Path) -> int:
    pdir = _friend_dir(root, a.name)
    f = Friend.load(pdir)
    creds = pdir / "google-credentials" / f"{f.email}.json"
    if not creds.is_file():
        _err(f"{f.name} has not signed in to Google yet ({creds} missing)")
        return 1
    f.activated = True
    _write_profile_files(system, pdir, f, None, None)
    system.systemctl("restart", gateway_unit(f.name))
    print(f"Activated {f.name}.")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="friends")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--telegram-id", required=True)
    add.add_argument("--bot-token", required=True)
    add.add_argument("--tz", required=True)
    add.add_argument("--email", required=True)
    add.add_argument("--cap", type=float, default=DEFAULT_CAP_USD)
    act = sub.add_parser("activate")
    act.add_argument("name")
    return p


def main(argv: list[str] | None = None, system: System | None = None,
         root: Path | None = None, caddy_dir: Path | None = None,
         now: datetime | None = None) -> int:
    now = now or datetime.now(ZoneInfo("Europe/London"))  # used by later subcommands
    root = Path(root) if root is not None else OWNER_ROOT
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    system = system or System(root)
    try:
        if a.cmd == "add":
            return cmd_add(a, system, root, Path(caddy_dir) if caddy_dir else DEFAULT_CADDY_DIR)
        if a.cmd == "activate":
            return cmd_activate(a, system, root)
    except Refused as e:
        _err(str(e))
        return e.code
    except Exception as e:
        _err(" ".join(str(e).split()) or type(e).__name__)
        return 1
    return 2
