"""Approval channel: a Telegram bot the broker owns and the agent cannot see.

Grants are issued only from a message that is (a) from the owner's numeric id,
(b) an exact `approve <4 digits>`, `deploy <7-40 hex>` or `revoke`, and (c) not forwarded. (c) is the
defence against As-built #20: text someone else wrote, forwarded in, arrives
through a channel already vetted as the owner.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from .grants import DEPLOY_GRANT_MINUTES, GrantStore

logger = logging.getLogger(__name__)

_APPROVE = re.compile(r"^approve\s+(\d{4})$", re.IGNORECASE)
_REVOKE = re.compile(r"^revoke$", re.IGNORECASE)
_DEPLOY = re.compile(r"^deploy\s+([0-9a-f]{7,40})$", re.IGNORECASE)

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
) -> None:
    offset = 0
    with httpx.Client() as client:
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
                _send(client, token, owner_id, handle(store, verb, arg, time.time()))
