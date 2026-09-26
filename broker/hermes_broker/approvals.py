"""Approval channel: a Telegram bot the broker owns and the agent cannot see.

Grants are issued only from a message that is (a) from the owner's numeric id,
(b) an exact `approve <4 digits>`, `deploy <7-40 hex>`, `revoke`, or
`task|solo|drop <4 digits>`, and (c) not forwarded. (c) is the
defence against As-built #20: text someone else wrote, forwarded in, arrives
through a channel already vetted as the owner.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

import httpx

from .grants import DEPLOY_GRANT_MINUTES, GrantStore
from .tasks import TaskStore

logger = logging.getLogger(__name__)

_APPROVE = re.compile(r"^approve\s+(\d{4})$", re.IGNORECASE)
_REVOKE = re.compile(r"^revoke$", re.IGNORECASE)
_DEPLOY = re.compile(r"^deploy\s+([0-9a-f]{7,40})$", re.IGNORECASE)
_TASK = re.compile(r"^(task|solo|drop)\s+(\d{4})$", re.IGNORECASE)

# Any of these on a message means it originated elsewhere.
_FORWARD_MARKERS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
)


def parse_command(update: dict, owner_id: int) -> tuple[str, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if any(marker in message for marker in _FORWARD_MARKERS):
        logger.warning("ignoring forwarded message in approvals channel")
        return None
    if (message.get("from") or {}).get("id") != owner_id:
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()

    approve = _APPROVE.match(text)
    if approve:
        return ("approve", approve.group(1))
    deploy = _DEPLOY.match(text)
    if deploy:
        return ("deploy", deploy.group(1).lower())
    task = _TASK.match(text)
    if task:
        return (task.group(1).lower(), task.group(2))
    if _REVOKE.match(text):
        return ("revoke", "")
    return None


def handle(store: GrantStore, verb: str, arg: str, now: float) -> str:
    """Apply one parsed owner command; return the reply to send."""
    if verb == "revoke":
        store.revoke(None)
        logger.info("all grants revoked by owner")
        return "Revoked. All boxes locked."
    if verb == "deploy":
        sha = store.approve_deploy(arg, now)
        if sha is None:
            logger.info("rejected deploy prefix")
            return f"No pending deploy of a commit starting {arg}."
        logger.info("deploy of %s approved", sha)
        return f"Deploy of {sha} to tig-server approved for {DEPLOY_GRANT_MINUTES} min."
    reason = store.pending_reason(arg)
    box = store.approve(arg, now)
    if box is None:
        logger.info("rejected approval code")
        return "No pending request with that code."
    minutes = int((store.expires_at(box) - now) / 60)
    logger.info("granted %s for %s minutes", box, minutes)
    return f"Granted {box} for {minutes} min.\nFor: {reason}"


TASK_VERBS = ("task", "solo", "drop")
ANNOUNCE_EVERY = 300
BODY_CHARS = 1500
SOLO_MINUTES = 60
Call = Callable[..., dict]


def format_announcement(issue: dict, code: str) -> str:
    source = next((x.split(":", 1)[1] for x in issue.get("labels", []) if x.startswith("source:")), "manual")
    head = f"{issue['repo']}#{issue['number']} [{source}] {issue['title'][:200]}"
    body = issue.get("body", "")
    shown = body[:BODY_CHARS] + ("\n[… trimmed]" if len(body) > BODY_CHARS else "")
    if "ops" in issue.get("labels", []):
        tail = f"code {code} — needs you on the box, not an agent.\ndrop {code}"
    else:
        tail = f"code {code}\ntask {code} | solo {code} | drop {code}"
    return f"{head}\n\n{shown}\n\n{tail}"


def _solo_prompt(slug: str, number: int, title: str, body: str) -> str:
    return (f"Fix issue #{number} in {slug}. Open a pull request whose description says `Closes #{number}`.\n\n"
            f"Title: {title}\n\n{body}")


def handle_task(tasks: TaskStore, verb: str, code: str, now: float, call: Call, repos: tuple[str, ...]) -> str:
    """Apply the owner's task/solo/drop reply. The code is spent whatever happens;
    if the issue is still in triage, the next announcer tick offers a new one."""
    entry = tasks.take(code, now)
    if entry is None:
        return "No announced task with that code."
    ref = f"{entry['repo']}#{entry['number']}"
    if verb == "drop":
        r = call("close_task", {"repo": entry["repo"], "number": entry["number"]}, 60)
        return f"{ref}: {r['error']}" if "error" in r else f"Dropped {ref}."
    if verb == "solo" and entry["slug"] not in repos:
        return f"{ref}: {entry['slug']} is not allowlisted for run_task; add it to broker.json repos."
    mode = "fleet" if verb == "task" else "solo"
    r = call("approve_task", {"repo": entry["repo"], "number": entry["number"], "hash": entry["hash"], "mode": mode}, 60)
    if "error" in r:
        return f"{ref}: {r['error']}"
    if mode == "fleet":
        return f"{ref} is ready for fleet. It runs on the next fleet night, or run_fleet('{entry['repo']}')."
    t = call("run_task", {"repo": entry["slug"], "prompt": _solo_prompt(entry["slug"], entry["number"], r["title"], r["body"]),
                          "minutes": SOLO_MINUTES}, 60)
    if "error" in t:
        return f"{ref} moved to fleet:human, but run_task failed: {t['error']}"
    return f"{ref}: run_task {t['id']} started; it opens a PR that closes #{entry['number']}."


class Announcer:
    """Posts each fleet:triage issue once, with a code. Lives in the listener
    because getUpdates is single-consumer and this bot is the owner's only channel."""

    def __init__(self, tasks: TaskStore, grants: GrantStore, call: Call, send: Callable[[str], None]) -> None:
        self.tasks, self.grants, self.call, self.send = tasks, grants, call, send
        self.box_down = False

    def tick(self, now: float) -> None:
        r = self.call("list_triage", {}, 120)
        if "error" in r:
            if not self.box_down:
                self.send(f"Task announcer: cannot list triage on tig-server: {r['error']}")
                self.box_down = True
            return
        if self.box_down:
            self.send("Task announcer: tig-server reachable again.")
            self.box_down = False
        for err in r.get("errors", []):
            logger.warning("list_triage: %s", err)
        issues = r.get("issues", [])
        self.tasks.prune(set(r.get("repos_ok", [])), {(i["repo"], i["number"]) for i in issues}, now)
        for issue in issues:
            code = self.tasks.announce(issue["repo"], issue["slug"], issue["number"], issue["hash"], now,
                                       avoid=self.grants.pending_codes(now))
            if code:
                self.send(format_announcement(issue, code))


def _send(client: httpx.Client, token: str, chat_id: int, text: str) -> None:
    try:
        client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except httpx.HTTPError:
        logger.exception("failed to send approval reply")


def run_listener(
    store: GrantStore,
    token: str,
    owner_id: int,
    poll_seconds: int = 30,
    tasks: TaskStore | None = None,
    call: Call | None = None,
    repos: tuple[str, ...] = (),
) -> None:
    offset = 0
    last_announce = 0.0
    with httpx.Client() as client:
        announcer = None
        if tasks is not None and call is not None:
            announcer = Announcer(tasks, store, call, lambda text: _send(client, token, owner_id, text))
        while True:
            try:
                response = client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": offset, "timeout": poll_seconds},
                    timeout=poll_seconds + 15,
                )
                response.raise_for_status()
                updates = response.json().get("result", [])
            except (httpx.HTTPError, ValueError):
                logger.exception("getUpdates failed; retrying")
                time.sleep(5)
                continue

            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                parsed = parse_command(update, owner_id)
                if parsed is None:
                    continue
                verb, arg = parsed
                if verb in TASK_VERBS:
                    if tasks is None or call is None:
                        continue
                    reply = handle_task(tasks, verb, arg, time.time(), call, repos)
                else:
                    reply = handle(store, verb, arg, time.time())
                _send(client, token, owner_id, reply)
            now = time.time()
            if announcer is not None and now - last_announce >= ANNOUNCE_EVERY:
                last_announce = now
                try:
                    announcer.tick(now)
                except Exception:
                    # The announcer must never take grant approval down with it.
                    logger.exception("announcer tick failed")
