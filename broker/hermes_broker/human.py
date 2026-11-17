"""fleet:human issues in the approvals chat.

A fleet night escalates an issue it cannot settle by labelling it fleet:human and
commenting why. The listener announces each such issue once, with a code bound to
the hash the box listed (the issue and its latest escalation), and takes the
owner's `answer <code> <text>`, `drop <code>` or `take <code>`. The box refuses an
answer or take whose hash no longer matches, so a reply cannot answer a question
that changed after it was shown.

Codes live in their own TaskStore (human_codes.json) and avoid every other store's
live codes, so `drop <code>` reaches exactly the store that issued it.
"""

from __future__ import annotations

import logging
from typing import Callable

from .approvals import _TAIL_SEPARATOR, BODY_CHARS, TITLE_CHARS, Call, _avoid_codes
from .grants import GrantStore
from .proposals import ProposalStore
from .tasks import TaskStore

logger = logging.getLogger(__name__)

# The same cap the box enforces (tasklib.MAX_ANSWER); checked here so an over-long
# answer is refused before any box call.
ANSWER_CHARS = 2000
NO_ESCALATION = "(no escalation comment on the issue; read it on GitHub)"
HUMAN_VERBS = ("answer", "take")


def _without_separator(text: str) -> str:
    """`text` with the reply separator removed until none is left: one pass is not
    enough, since removing one can join the pieces around it into another."""
    while True:
        prev, text = text, text.replace(_TAIL_SEPARATOR, "")
        if text == prev:
            return text


def format_human(issue: dict, code: str) -> str:
    """The announcement: head, the escalation text (or why it is missing), then the
    separator exactly once and the real reply instructions after it."""
    title = issue.get("title", "")[:TITLE_CHARS]
    head = _without_separator(f"fleet:human  {issue['repo']}#{issue['number']}  {title}")
    escalation = issue.get("escalation")
    if isinstance(escalation, dict) and isinstance(escalation.get("text"), str):
        text = escalation["text"]
    elif isinstance(issue.get("note"), str) and issue["note"]:
        text = issue["note"]
    else:
        text = NO_ESCALATION
    shown = _without_separator(text[:BODY_CHARS]) + ("\n[… trimmed]" if len(text) > BODY_CHARS else "")
    tail = f"code {code}\nanswer {code} <your text> | drop {code} | take {code}"
    return f"{head}\n\n{shown}\n\n{_TAIL_SEPARATOR}\n{tail}"


def handle_human(humans: TaskStore, verb: str, arg: str, now: float, call: Call) -> str:
    """Apply the owner's answer/drop/take reply. The code is spent whatever happens;
    if the issue still waits on the owner, the next tick announces it with a new one.
    For `answer`, `arg` is "<code>\\n<text>" (parse_command's form)."""
    code, _, text = arg.partition("\n")
    entry = humans.take(code, now)
    if entry is None:
        return "No announced fleet:human issue with that code."
    ref = f"{entry['repo']}#{entry['number']}"
    again = "This code is spent; if the issue still waits on you it will be announced again with a new code."
    if verb == "answer":
        text = text.strip()
        if not 1 <= len(text) <= ANSWER_CHARS:
            return (f"{ref}: an answer must be 1 to {ANSWER_CHARS} characters; yours is {len(text)}. "
                    f"Nothing was sent. {again}")
        r = call("answer_human", {"repo": entry["repo"], "number": entry["number"], "hash": entry["hash"],
                                  "text": text}, 120)
        if "error" in r:
            return f"{ref}: {r['error']}\n{again}"
        return f"{ref}: answer recorded; the issue is back in fleet:ready for the next fleet night."
    if verb == "take":
        r = call("take_human", {"repo": entry["repo"], "number": entry["number"], "hash": entry["hash"]}, 120)
        if "error" in r:
            return f"{ref}: {r['error']}\n{again}"
        return f"{ref} is yours: assigned to {r.get('assignee', 'the box login')}; it will not be announced again."
    if verb == "drop":
        r = call("close_task", {"repo": entry["repo"], "number": entry["number"]}, 60)
        return f"{ref}: {r['error']}" if "error" in r else f"Dropped {ref}."
    return f"{ref}: unknown reply {verb!r}. {again}"


class HumanAnnouncer:
    """Posts each fleet:human issue once per escalation (the box's hash), with a code."""

    def __init__(self, humans: TaskStore, grants: GrantStore, tasks: TaskStore | None,
                 proposals: ProposalStore | None, call: Call, send: Callable[[str], bool]) -> None:
        self.humans, self.grants, self.tasks, self.proposals = humans, grants, tasks, proposals
        self.call, self.send = call, send
        self.box_down = False

    def tick(self, now: float) -> None:
        r = self.call("list_human", {}, 120)
        if "error" in r:
            if not self.box_down:
                self.send(f"fleet:human announcer: cannot list fleet:human on tig-server: {r['error']}")
                self.box_down = True
            return
        if self.box_down:
            self.send("fleet:human announcer: tig-server reachable again.")
            self.box_down = False
        for err in r.get("errors", []):
            logger.warning("list_human: %s", err)
        issues = r.get("issues", [])
        self.humans.prune(set(r.get("repos_ok", [])), {(i["repo"], i["number"]) for i in issues}, now)
        for issue in issues:
            code = self.humans.announce(issue["repo"], issue["slug"], issue["number"], issue["hash"], now,
                                        avoid=_avoid_codes(self.grants, self.tasks, self.proposals, now))
            if code and not self.send(format_human(issue, code)):
                # Give the code back so the next tick re-announces.
                self.humans.take(code, now)
