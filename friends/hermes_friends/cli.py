"""friends command line. All side effects go through the System object."""
import argparse
import copy
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import render, validate
from . import spend as spendmod
from .model import Friend
from .owner import normalize_email, owner_google_emails, read_owner_env
from .policy import (CADDY_FILE_MODE, RUNAWAY_USD, WARN_FRACTION, DEFAULT_CADDY_DIR, DEFAULT_CAP_USD, MEM_FLOOR_MB,
                     MEM_PER_FRIEND_MB, PUBLIC_HOST, gateway_unit)
from .registry import allocate_port, bot_token_in_use, list_friends, profile_dir
from .system import OWNER_ROOT, System


class Refused(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _creds_path(pdir: Path, email: str) -> Path:
    """Google credentials file of a friend; refuses a path that escapes google-credentials/."""
    base = (pdir / "google-credentials").resolve()
    path = pdir / "google-credentials" / f"{email}.json"
    if not path.resolve().is_relative_to(base):
        raise ValueError(f"credentials path for {email!r} is outside {base}")
    return path


def _write_profile_files(system, pdir: Path, f: Friend, token: str | None, owner,
                         memory: bool = True) -> None:
    system.write_file(pdir / "config.yaml", render.render_config(f), 0o600)
    if token is not None:
        system.write_file(pdir / ".env", render.render_env(f, token, owner), 0o600)
        system.write_file(pdir / "SOUL.md", render.render_soul(f), 0o600)
        for rel, data in render.skill_files().items():
            system.write_file(pdir / "skills" / rel, data, 0o600)
    if memory:
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
    if normalize_email(email) in {normalize_email(e) for e in owner_google_emails(root, root.parent)}:
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
        # Profile commands run as the owner: with HERMES_HOME set to the new profile, Hermes
        # creates <profile>/logs on startup and `profile create` then finds it already exists.
        system.hermes(None, "profile", "create", name, "--no-alias", "--no-skills")
        undo.append(lambda: system.hermes(None, "profile", "delete", "-y", name))
        system.chmod(pdir, 0o700)
        _write_profile_files(system, pdir, f, token, owner)
        undo.append(_undo_caddy(system, caddy_file))
        system.write_file(caddy_file, render.render_caddy(f), CADDY_FILE_MODE)
        system.caddy_reload()
        undo.append(lambda: system.systemctl("reset-failed", gateway_unit(name)))  # runs after uninstall
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


def _activate_memory(system, pdir: Path, f: Friend) -> None:
    """Drop only the sign-in line from MEMORY.md; keep whatever else the friend's agent added."""
    path = pdir / "memories" / "MEMORY.md"
    if not path.is_file():
        system.write_file(path, render.render_memory(f), 0o600)
        return
    line = render.signin_line(f)
    kept = [ln for ln in path.read_text().splitlines(keepends=True) if ln.rstrip("\r\n") != line]
    system.write_file(path, "".join(kept), 0o600)


def cmd_activate(a, system, root: Path, now: datetime) -> int:
    pdir = _friend_dir(root, a.name)
    f = Friend.load(pdir)
    creds = _creds_path(pdir, f.email)
    if not creds.is_file():
        _err(f"{f.name} has not signed in to Google yet ({creds} missing)")
        return 1
    f.activated = True
    st = spendmod.SpendState.load(pdir, now)  # before any write: a corrupt file changes nothing
    _write_profile_files(system, pdir, f, None, None, memory=False)
    _activate_memory(system, pdir, f)
    if st.stopped_by_cap or st.stopped_by_runaway or st.notified_cap or st.notified_runaway:
        print(f"{f.name} activated; gateway stays stopped (cap/runaway hold) — "
              "use `friends cap` or `friends resume`.")
        return 0
    system.systemctl("restart", gateway_unit(f.name))
    print(f"Activated {f.name}.")
    return 0


def _apply_actions(system, f: Friend, chat_id: int, actions: list[str],
                   sp, st, old_st, now: datetime) -> None:
    """Apply actions, setting each state flag as soon as its own effect completes,
    so a failure part-way is retried next run without repeating finished effects."""
    unit = gateway_unit(f.name)
    total, cap = f"{sp.total_usd:.2f}", f"{f.cap_usd:.2f}"
    tail = f"— ${total} of ${cap} this month (ESTIMATE)."
    actions = list(actions)

    def owner(event: str) -> None:
        system.send(None, chat_id, f"friends: {f.name} {event} {tail}")

    if "new_month" in actions:
        carried = (old_st.stopped_by_cap or old_st.notified_cap) and "stop_cap" in actions
        if ((old_st.stopped_by_cap or old_st.notified_cap)
                and not old_st.stopped_by_runaway and not carried):
            system.systemctl("enable", unit)
            system.systemctl("start", unit)
            system.send(None, chat_id, f"friends: {f.name} new month; gateway re-enabled.")
        st.start_month(spendmod.month_key(now))
        if carried:  # still over the cap: stays stopped, nobody is told again
            st.stopped_by_cap = st.notified_cap = True
            st.warned = True  # no 80% message for a month that starts over the cap
            actions.remove("stop_cap")
    if "warn" in actions:
        pct = round(WARN_FRACTION * 100)
        system.send(f.name, f.telegram_id,
                    f"You've used {pct}% of this month's assistant allowance "
                    f"(${total} of ${cap}, estimate). It resets on the 1st.")
        owner(f"passed {pct}%")
        st.warned = True
    stops = [x for x in ("stop_cap", "stop_runaway") if x in actions]
    if stops:
        runaway = "stop_runaway" in stops
        unreachable = False
        if not (st.notified_runaway if runaway else st.notified_cap):
            if runaway:
                sent = system.send(f.name, f.telegram_id,
                                   "I've paused myself because I was using far more than usual. "
                                   "The owner has been told.")
            else:
                sent = system.send(f.name, f.telegram_id,
                                   f"You've reached this month's assistant allowance (${cap}, "
                                   "estimate). I'll be back on the 1st.")
            if sent:
                st.notified_runaway = st.notified_runaway or runaway
                st.notified_cap = st.notified_cap or "stop_cap" in stops
            else:
                unreachable = True
        system.systemctl("stop", unit)
        system.systemctl("disable", unit)
        if unreachable:
            system.send(None, chat_id, f"friends: {f.name} could not be messaged about the stop")
        if "stop_cap" in stops:
            owner("reached cap; gateway stopped and disabled")
        if runaway:
            owner(f"spent >${RUNAWAY_USD:.2f} in an hour; gateway stopped and disabled")
        st.stopped_by_cap = st.stopped_by_cap or "stop_cap" in stops
        if "stop_cap" in stops:
            st.warned = True  # no 80% message after the cap message
        st.stopped_by_runaway = st.stopped_by_runaway or runaway
    if "alert_unpriced" in actions:
        new = [i for i in sp.unpriced if i not in st.alerted_unpriced]
        owner(f"has unpriced sessions {', '.join(new)}; counted at fallback rate")
        st.alerted_unpriced = st.alerted_unpriced + new


def cmd_spend_check(system, root: Path, now: datetime) -> int:
    chat_id = read_owner_env(root).chat_id
    base = root / "profiles"
    dirs = sorted(d for d in base.iterdir() if (d / "friend.json").is_file()) \
        if base.is_dir() else []
    failed = 0

    def report(name: str, e: Exception) -> None:
        nonlocal failed
        failed = 1
        msg = " ".join(str(e).split()) or type(e).__name__
        _err(f"{name}: {msg}")
        system.send(None, chat_id, f"friends: {name} spend-check failed: {msg}")

    for pdir in dirs:
        st = sp = None
        try:
            f = Friend.load(pdir)
            sp = spendmod.month_spend(pdir / "state.db", spendmod.month_start(now))
            st = spendmod.SpendState.load(pdir, now)
            old_st = copy.deepcopy(st)
            actions = spendmod.decide(f, sp, old_st, now)
            _apply_actions(system, f, chat_id, actions, sp, st, old_st, now)
        except Exception as e:
            report(pdir.name, e)
        finally:
            if st is not None and sp is not None:
                try:
                    cutoff = now.timestamp() - spendmod.SAMPLE_KEEP_S
                    st.samples = [s for s in st.samples if s[0] >= cutoff]
                    st.samples.append((now.timestamp(), sp.total_usd))
                    st.save(pdir, system)
                except Exception as e:
                    report(pdir.name, e)
    return failed


def cmd_list(system, root: Path, now: datetime) -> int:
    base = Path(root) / "profiles"
    dirs = sorted(d for d in base.iterdir() if (d / "friend.json").is_file()) \
        if base.is_dir() else []
    if not dirs:
        print("no friends")
        return 0
    start = spendmod.month_start(now)
    failed = 0
    for pdir in dirs:
        try:
            f = Friend.load(pdir)
            total = spendmod.month_spend(pdir / "state.db", start).total_usd
            active = "active" if system.unit_active(gateway_unit(f.name)) else "inactive"
            signed = "yes" if _creds_path(pdir, f.email).is_file() else "no"
            print(f"{f.name}  {active}  signed-in {signed}  "
                  f"${total:.2f} of ${f.cap_usd:.2f} ESTIMATE")
        except Exception as e:
            failed = 1
            print(f"{pdir.name}  error: {' '.join(str(e).split()) or type(e).__name__}")
    return failed


def _save_friend(system, pdir: Path, f: Friend) -> None:
    system.write_file(pdir / "friend.json", json.dumps(asdict(f), indent=2) + "\n", 0o600)


def _start(system, name: str) -> None:
    unit = gateway_unit(name)
    system.systemctl("enable", unit)
    system.systemctl("start", unit)


def cmd_cap(a, system, root: Path, now: datetime) -> int:
    pdir = _friend_dir(root, a.name)
    if not math.isfinite(a.usd) or a.usd < 0:
        _err("cap must be a finite number, 0 or more")
        return 2
    f = Friend.load(pdir)
    f.cap_usd = a.usd
    _save_friend(system, pdir, f)
    st = spendmod.SpendState.load(pdir, now)
    if st.month != spendmod.month_key(now):
        # Stale state: the next spend-check starts the new month and decides the restart.
        print(f"{f.name} cap set to ${a.usd:.2f}.")
        return 0
    total = spendmod.month_spend(pdir / "state.db", spendmod.month_start(now)).total_usd
    restart = (st.stopped_by_cap or st.notified_cap) and round(a.usd, 6) > round(total, 6)
    if restart:
        st.clear_cap()
        if not st.stopped_by_runaway:
            st.samples = []  # old samples would re-trigger a runaway stop at once
        st.save(pdir, system)
        if not st.stopped_by_runaway:
            _start(system, f.name)
        else:
            print(f"{f.name} stays paused: runaway hold; use `resume`.")
    print(f"{f.name} cap set to ${a.usd:.2f}.")
    return 0


def cmd_resume(a, system, root: Path, now: datetime) -> int:
    pdir = _friend_dir(root, a.name)
    if not Friend.load(pdir).activated:
        _err(f"{a.name} has not been activated yet; run `activate` after Google sign-in")
        return 2
    st = spendmod.SpendState.load(pdir, now)
    st.clear_runaway()
    st.clear_cap()
    st.samples = []  # old samples would re-trigger a runaway stop at once
    st.save(pdir, system)
    _start(system, a.name)
    print(f"Resumed {a.name}.")
    return 0


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def cmd_remove(a, system, root: Path, caddy_dir: Path) -> int:
    """Deleting the friend's data is never blocked: every step after validation warns and continues."""
    pdir = _friend_dir(root, a.name)
    name = a.name
    warned = 0

    def step(what: str, fn) -> None:
        nonlocal warned
        try:
            fn()
        except Exception as e:
            warned = 1
            _warn(f"{what} failed: {' '.join(str(e).split()) or type(e).__name__}")

    unit = gateway_unit(name)
    credsdir = pdir / "google-credentials"
    creds = None
    try:
        creds = _creds_path(pdir, Friend.load(pdir).email)
    except Exception as e:
        warned = 1
        _warn(f"friend.json unreadable ({' '.join(str(e).split())}); continuing by name")
        found = [c for c in credsdir.glob("*.json") if c.name != "oauth_states.json"] \
            if credsdir.is_dir() else []
        creds = found[0] if len(found) == 1 else None
    if creds is not None and creds.is_file():  # revoke first: the delete destroys the token
        step("Google token revoke",
             lambda: system.revoke_google_token(json.loads(creds.read_text())["refresh_token"]))
    for verb in ("stop", "disable"):
        step(f"systemctl {verb} {unit}", lambda v=verb: system.systemctl(v, unit))
    step("gateway uninstall", lambda: system.hermes(name, "gateway", "uninstall"))
    # the unit lingers as "not-found failed" after uninstall
    step(f"systemctl reset-failed {unit}", lambda: system.systemctl("reset-failed", unit))
    # the sandbox container outlives the profile and keeps its network
    step("removing containers", lambda: system.remove_containers(pdir))
    # the root-run sandbox leaves root-owned files that block the delete
    step(f"reclaim {pdir}", lambda: system.reclaim(pdir))
    step("profile delete", lambda: system.hermes(None, "profile", "delete", "-y", name))
    if pdir.exists():
        step(f"removing {pdir}", lambda: system.remove_tree(pdir))
    caddy_file = Path(caddy_dir) / f"{name}.caddy"
    if caddy_file.exists():
        step("removing the Caddy file", lambda: system.remove_tree(caddy_file))
    step("caddy reload", system.caddy_reload)
    print(f"Removed {name}." if not pdir.exists() else f"{name}: profile still present.")
    print("delete the bot in BotFather (@BotFather → /deletebot)")
    if warned or pdir.exists():
        print("remove finished with warnings; check the lines above")
        return 1
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
    sub.add_parser("spend-check")
    sub.add_parser("list")
    cap = sub.add_parser("cap")
    cap.add_argument("name")
    cap.add_argument("usd", type=float)
    res = sub.add_parser("resume")
    res.add_argument("name")
    rem = sub.add_parser("remove")
    rem.add_argument("name")
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
            return cmd_activate(a, system, root, now)
        if a.cmd == "spend-check":
            return cmd_spend_check(system, root, now)
        if a.cmd == "list":
            return cmd_list(system, root, now)
        if a.cmd == "cap":
            return cmd_cap(a, system, root, now)
        if a.cmd == "resume":
            return cmd_resume(a, system, root, now)
        if a.cmd == "remove":
            return cmd_remove(a, system, root, Path(caddy_dir) if caddy_dir else DEFAULT_CADDY_DIR)
    except Refused as e:
        _err(str(e))
        return e.code
    except Exception as e:
        _err(" ".join(str(e).split()) or type(e).__name__)
        return 1
    return 2
