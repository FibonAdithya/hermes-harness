"""Tell Hermes that the owner approved something, so it continues unasked.

A grant is a file the agent's tools read when called; writing it wakes nobody.
The gateway does have a way in for other programs: its kanban notifier. When a
task completes, every chat subscribed to it gets a one-line notice, and if the
task carries a session id the gateway also puts a turn into that chat's session:

    [kanban] Task t_... completed.
    Title: <the task title>
    ...

So a prod is a task that exists only to complete: create it with a session id
and no assignee (the dispatcher skips unassigned tasks), subscribe the owner's
Telegram DM, complete it. The gateway delivers within its 5-second tick.

`hermes kanban create` cannot set a session id, so that one step calls
`hermes_cli.kanban_db.create_task` with Hermes's own interpreter. The other
steps are plain CLI calls. Checked against Hermes v0.19.0; `runbooks/dispatch.md`
has the check to repeat after a Hermes upgrade.

A prod grants nothing. Gated tools still read grants.json, so a forged or
repeated prod only makes Hermes take a turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping

logger = logging.getLogger(__name__)

Prod = Callable[[str], bool]

HERMES_DIR = Path("/home/hermes/.hermes/hermes-agent")
HERMES_BIN = Path("/home/hermes/.local/bin/hermes")
# Any non-empty value makes the gateway wake the subscribed chat; this one also
# marks the tasks as ours, so tidying up never touches anyone else's.
SESSION = "approvals-prod"
# The wake message shows this much of a title (gateway/kanban_watchers.py).
TITLE_CHARS = 120
STEP_TIMEOUT = 30

# argv: title, session id. Prints the new task id on the last line.
CREATE_SCRIPT = """\
import sys
from hermes_cli import kanban_db as kb
with kb.connect_closing() as conn:
    task_id = kb.create_task(conn, title=sys.argv[1], created_by="approvals", session_id=sys.argv[2])
print(task_id)
"""

_TASK_ID = re.compile(r"t_[0-9a-f]+")
# The listener runs under `uv run`, which points these at the broker's
# virtualenv. Hermes has its own.
_NOT_INHERITED = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")


def make_prod(
    chat_id: int,
    hermes_dir: Path = HERMES_DIR,
    hermes_bin: Path = HERMES_BIN,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
) -> Prod:
    """A function that prods Hermes in the owner's Telegram DM with one line of text.

    It returns True once the gateway has been handed the prod, False if any step
    failed. It never raises: a grant already written must not be lost to it.
    """
    python = str(Path(hermes_dir) / "venv" / "bin" / "python")
    hermes = str(hermes_bin)

    def call(step: str, cmd: list[str], cwd: Path | None = None) -> str | None:
        """stdout of a command that exited 0, else None. `step` names it in the log."""
        base = os.environ if environ is None else environ
        env = {k: v for k, v in base.items() if k not in _NOT_INHERITED}
        try:
            done = run(cmd, capture_output=True, text=True, timeout=STEP_TIMEOUT, cwd=cwd, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("prod: %s did not run: %s", step, exc)
            return None
        if done.returncode != 0:
            logger.warning("prod: %s exited %s: %s", step, done.returncode, (done.stderr or "")[-300:])
            return None
        return done.stdout or ""

    def tidy(keep: str) -> None:
        """Archive earlier prod tasks. `keep` is the one just completed: the
        gateway may not have delivered it yet."""
        listed = call("list", [hermes, "kanban", "list", "--session", SESSION, "--json"])
        if listed is None:
            return
        try:
            rows = json.loads(listed)
        except ValueError:
            return
        if not isinstance(rows, list):
            return
        old = [row["id"] for row in rows
               if isinstance(row, dict) and isinstance(row.get("id"), str)
               and _TASK_ID.fullmatch(row["id"]) and row["id"] != keep]
        if old:
            call("archive", [hermes, "kanban", "archive", *old])

    def prod(title: str) -> bool:
        created = call("create", [python, "-c", CREATE_SCRIPT, title, SESSION], cwd=Path(hermes_dir))
        lines = (created or "").strip().splitlines()
        task_id = lines[-1].strip() if lines else ""
        if not _TASK_ID.fullmatch(task_id):
            logger.warning("prod: no task was created")
            return False
        if call("subscribe", [hermes, "kanban", "notify-subscribe", task_id, "--platform", "telegram",
                              "--chat-id", str(chat_id), "--chat-type", "dm"]) is None:
            return False
        if call("complete", [hermes, "kanban", "complete", task_id, "--result", "approved"]) is None:
            return False
        logger.info("prodded Hermes with %s", task_id)
        tidy(keep=task_id)
        return True

    return prod
