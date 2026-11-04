from __future__ import annotations

import pytest

from hermes_tuner.model import Fix
from hermes_tuner.prompt_block import (END, START, block_text, check, splice, strip_block,
                                       with_prompt)


@pytest.mark.parametrize("owner", ["Triage.", "Triage.\n", "Triage.  ", ""])
def test_splice_keeps_owner_text_byte_for_byte(owner):
    """Review Focus 3. Catches: rstrip/strip applied to the owner's text."""
    assert strip_block(splice(owner, " Use batch reads. ")) == owner


def test_splice_replaces_existing_block():
    """Review Focus 2. Catches: appending a second block."""
    assert splice(splice("Triage.", "a"), "b") == splice("Triage.", "b")


def test_splice_strips_the_block_text_only():
    """Catches: block text spliced with its surrounding whitespace."""
    assert splice("T.", "  x  \n") == "T.\n\n" + START + "\nx\n" + END


def test_check_rejects_dropped_owner_rules():
    """Incident A's second failure. Catches: comparing only the block."""
    cur = "Triage. Always keep security alerts."
    assert check(splice(cur, "x").replace(" Always keep security alerts.", ""), cur) is not None


def test_check_accepts_a_splice_over_an_existing_block():
    """Catches: check rejecting a current prompt that already carries a block."""
    cur = splice("Triage.", "old")
    assert check(splice(cur, "new"), cur) is None


@pytest.mark.parametrize("new", [
    lambda c: splice(c, "x") + "\n" + START + "\nx\n" + END,   # two blocks
    lambda c: splice(c, "x") + " trailing",                      # block not at the end
    lambda c: splice(c, "x " + END + " y")])                     # marker smuggled in the block
def test_check_rejects_malformed_blocks(new):
    """Catches: counting markers loosely or allowing text after END."""
    assert check(new("Triage."), "Triage.") is not None


def test_check_rejects_a_prompt_without_a_block():
    """Catches: treating a bare unchanged prompt as a valid edit."""
    assert check("Triage.", "Triage.") is not None


def test_check_rejects_text_after_end_that_the_current_prompt_also_has():
    """The owner-text comparison cannot see trailing text both prompts share.
    Catches: dropping the must-end-the-prompt check."""
    cur = "T.\n\n" + START + "\na\n" + END + " tail"
    assert check("T.\n\n" + START + "\nx\n" + END + " tail", cur) == \
        "tuner notes block must end the prompt"


def test_check_rejects_two_blocks_that_the_current_prompt_also_has():
    """strip_block removes only the first block, so a second one shared with the current
    prompt passes the owner-text comparison. Catches: loosening the exactly-one count."""
    second = "\n\n" + START + "\nb\n" + END
    cur = splice("T.", "a") + second
    assert check(splice("T.", "x") + second, cur) == \
        "job prompt must contain exactly one tuner notes block"


def test_check_needs_the_current_prompt():
    """Catches: passing when the job could not be read."""
    assert check(splice("Triage.", "x"), None) is not None
    assert check(splice("Triage.", "x"), "Triage.") is None


def test_with_prompt_splices_a_job_prompt_payload():
    """Catches: with_prompt leaving the block unspliced or keeping the block key."""
    fix = Fix("f", "job_prompt", "j", {"block": "b", "add_toolsets": ["tuner"]}, "k")
    out = with_prompt(fix, {"prompt": "Owner."})
    assert out.payload == {"prompt": splice("Owner.", "b"), "add_toolsets": ["tuner"]}
    assert (out.id, out.kind, out.target, out.candidate_key) == ("f", "job_prompt", "j", "k")


def test_with_prompt_splices_a_composed_tools_job_prompt():
    """Catches: composed_tool job_prompt left as a bare block, or None turned into text."""
    base = {"name": "tuned_x", "job_prompt": "use it"}
    out = with_prompt(Fix("f", "composed_tool", "j", dict(base), "k"), {"prompt": "Owner."})
    assert out.payload == {"name": "tuned_x", "job_prompt": splice("Owner.", "use it")}
    none = with_prompt(Fix("f", "composed_tool", "j", dict(base, job_prompt=None), "k"),
                       {"prompt": "Owner."})
    assert none.payload["job_prompt"] is None


@pytest.mark.parametrize("job", [None, {}, {"prompt": 5}])
def test_with_prompt_unreadable_job_prompt_yields_a_bare_block(job):
    """Catches: splicing onto a missing prompt, which would let check pass."""
    fix = Fix("f", "job_prompt", "j", {"block": "b", "add_toolsets": []}, "k")
    out = with_prompt(fix, job)
    assert out.payload["prompt"] == "b"
    assert check(out.payload["prompt"], None) is not None


def test_with_prompt_leaves_other_kinds_alone():
    """Catches: with_prompt touching config or skill payloads."""
    fix = Fix("f", "config", "k", {"key": "k", "value": 1}, "c")
    assert with_prompt(fix, {"prompt": "Owner."}) == fix


@pytest.mark.parametrize("text, want", [
    (splice("Fetch mail with tuned_x.", "Skip starred."), "\nSkip starred.\n"),
    (splice("", "Skip starred."), "\nSkip starred.\n"),
    ("Fetch mail with tuned_x.", None),
    ("Owner.\n\n" + START + "\nno end", None),
    ("Owner. " + START + "\nx\n" + END, None)])
def test_block_text_is_the_block_strip_block_removes(text, want):
    """Catches: block_text finding a block strip_block would not (no leading blank line,
    or no END), or returning text outside the markers."""
    assert block_text(text) == want
