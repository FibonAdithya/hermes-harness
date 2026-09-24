"""Approval channel: a Telegram bot the broker owns and the agent cannot see.

Grants are issued only from a message that is (a) from the owner's numeric id,
(b) an exact `approve <4 digits>` or `revoke`, and (c) not forwarded. (c) is the
defence against As-built #20: text someone else wrote, forwarded in, arrives
through a channel already vetted as the owner.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from .grants import GrantStore

logger = logging.getLogger(__name__)

_APPROVE = re.compile(r"^approve\s+(\d{4})$", re.IGNORECASE)
_REVOKE = re.compile(r"^revoke$", re.IGNORECASE)

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
    if _REVOKE.match(text):
        return ("revoke", "")
    return None


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
                verb, code = parsed
                now = time.time()
                if verb == "revoke":
                    store.revoke(None)
                    _send(client, token, owner_id, "Revoked. All boxes locked.")
                    logger.info("all grants revoked by owner")
                    continue

                reason = store.pending_reason(code)
                box = store.approve(code, now)
                if box is None:
                    _send(client, token, owner_id, "No pending request with that code.")
                    logger.info("rejected approval code")
                else:
                    minutes = int((store.expires_at(box) - now) / 60)
                    _send(
                        client,
                        token,
                        owner_id,
                        f"Granted {box} for {minutes} min.\nFor: {reason}",
                    )
                    logger.info("granted %s for %s minutes", box, minutes)
