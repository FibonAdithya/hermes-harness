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
_DEPLOY = re.compile(r"^deploy\s+(?:(harness|fleet)\s+)?([0-9a-f]{7,40})$", re.IGNORECASE)
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
        target = (deploy.group(1) or "harness").lower()
        prefix = deploy.group(2).lower()
        return ("deploy", f"{target}:{prefix}")
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
        # arg is "target:prefix" from parse_command; a bare prefix (no colon), as a
        # caller invoking handle() directly may still pass, means target=harness.
        target, sep, prefix = arg.partition(":")
        if not sep:
            target, prefix = "harness", target
        sha = store.approve_deploy(prefix, now, target=target)
        if sha is None:
            logger.info("rejected deploy prefix")
            return f"No pending {target} deploy of a commit starting {prefix}."
        logger.info("deploy of %s approved", sha)
        # sha was granted for `target`: approve_deploy only grants on a target match,
        # so there is no need to re-read the grant to learn its target.
        what = sha if target == "harness" else f"{target} {sha}"
        return f"Deploy of {what} to tig-server approved for {DEPLOY_GRANT_MINUTES} min."
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
# The announcement shows the whole issue up to these bounds, and the owner's
# approval binds the hash of the whole title and body, so anything longer is
# offered drop-only (it could not have been read in full). tasklib.MAX_BODY on
# the box is the same 3500, so file_task never files an issue that would be.
BODY_CHARS = 3500
TITLE_CHARS = 200
SOLO_MINUTES = 60
Call = Callable[..., dict]


_TAIL_SEPARATOR = "—— reply below ——"


def is_drop_only(issue: dict) -> bool:
    """True when only `drop` may be offered: the text is longer than an announcement
    shows (so the owner cannot have read what `task`/`solo` would approve), or it
    is an ops fault, which no agent can fix."""
    return (len(issue.get("body", "")) > BODY_CHARS or len(issue.get("title", "")) > TITLE_CHARS
            or "ops" in issue.get("labels", []))


def format_announcement(issue: dict, code: str) -> str:
    source = next((x.split(":", 1)[1] for x in issue.get("labels", []) if x.startswith("source:")), "manual")
    head = f"{issue['repo']}#{issue['number']} [{source}] {issue['title'][:TITLE_CHARS]}"
    body = issue.get("body", "")
    trimmed = len(body) > BODY_CHARS or len(issue["title"]) > TITLE_CHARS
    shown = body[:BODY_CHARS] + ("\n[… trimmed]" if len(body) > BODY_CHARS else "")
    if "ops" in issue.get("labels", []):
        tail = f"code {code} — needs you on the box, not an agent.\ndrop {code}"
    elif trimmed:
        tail = f"code {code} — too long to show in full — read it on GitHub; only drop is offered.\ndrop {code}"
    else:
        tail = f"code {code}\ntask {code} | solo {code} | drop {code}"
    # A fixed marker precedes the tail so an issue body cannot pass itself off as the
    # reply instructions (e.g. a body containing the literal text "drop 9999").
    return f"{head}\n\n{shown}\n\n{_TAIL_SEPARATOR}\n{tail}"


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
    if verb != "drop" and entry.get("drop_only", False):
        return (f"{ref} was offered drop only: it is too long to show here in full (shorten it on GitHub), "
                f"or it is an ops fault (fix it on the box). This code is spent; if the issue is still "
                f"in triage it will be announced again.")
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
    title, body = r.get("title"), r.get("body")
    if title is None or body is None:
        return f"{ref}: box returned no title/body for approve_task"
    t = call("run_task", {"repo": entry["slug"], "prompt": _solo_prompt(entry["slug"], entry["number"], title, body),
                          "minutes": SOLO_MINUTES}, 60)
    if "error" in t:
        return f"{ref} moved to fleet:human, but run_task failed: {t['error']}"
    task_id = t.get("id")
    if task_id is None:
        return f"{ref}: box returned no id for run_task"
    return f"{ref}: run_task {task_id} started; it opens a PR that closes #{entry['number']}."


class Announcer:
    """Posts each fleet:triage issue once, with a code. Lives in the listener
    because getUpdates is single-consumer and this bot is the owner's only channel."""

    def __init__(self, tasks: TaskStore, grants: GrantStore, call: Call, send: Callable[[str], bool]) -> None:
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
                                       avoid=self.grants.pending_codes(now), drop_only=is_drop_only(issue))
            if code and not self.send(format_announcement(issue, code)):
                # The send failed: give the code back so the next tick re-announces
                # instead of the issue silently sitting unannounced for 7 days.
                self.tasks.take(code, now)


def _send(client: httpx.Client, token: str, chat_id: int, text: str) -> bool:
    """True only once Telegram has actually accepted the message."""
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except httpx.HTTPError:
        logger.exception("failed to send approval reply")
        return False
    if response.status_code // 100 != 2:
        logger.warning("sendMessage failed: HTTP %s: %s", response.status_code, response.text[:300])
        return False
    try:
        ok = bool(response.json().get("ok"))
    except ValueError:
        logger.warning("sendMessage returned a non-JSON body")
        return False
    if not ok:
        logger.warning("sendMessage returned ok=false")
    return ok


def _dispatch(
    update: dict,
    owner_id: int,
    store: GrantStore,
    tasks: TaskStore | None,
    call: Call | None,
    repos: tuple[str, ...],
    now_fn: Callable[[], float],
) -> str | None:
    """Route one parsed update to its handler; None means nothing to send.

    Grant and deploy replies are judged by when the owner sent them (the message's
    `date`), not by when this loop gets around to processing it: the announcer tick
    can hold the loop for up to 120s (list_triage's own timeout), and
    REQUEST_TTL_SECONDS is also 120s, so processing-time expiry would spuriously
    reject a reply the owner sent in time. Task replies use 7-day codes, so
    processing time is fine there, and a broken handler must not take the listener
    down with it.
    """
    parsed = parse_command(update, owner_id)
    if parsed is None:
        return None
    verb, arg = parsed
    if verb in TASK_VERBS:
        if tasks is None or call is None:
            return None
        try:
            return handle_task(tasks, verb, arg, now_fn(), call, repos)
        except Exception as exc:
            logger.exception("handle_task failed")
            return f"task reply failed: {exc}"
    sent_at = float((update.get("message") or {}).get("date") or now_fn())
    return handle(store, verb, arg, sent_at)


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
                reply = _dispatch(update, owner_id, store, tasks, call, repos, time.time)
                if reply is not None:
                    _send(client, token, owner_id, reply)
            now = time.time()
            if announcer is not None and now - last_announce >= ANNOUNCE_EVERY:
                last_announce = now
                try:
                    announcer.tick(now)
                except Exception:
                    # The announcer must never take grant approval down with it.
                    logger.exception("announcer tick failed")
