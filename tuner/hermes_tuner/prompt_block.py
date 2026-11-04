"""P2: the tuner edits only its own block at the end of a job prompt. The drafting model
writes the block; the tuner splices it after the owner's prompt between fixed markers,
and check() rejects any new prompt whose text outside the block differs from the
current one."""
from __future__ import annotations

from dataclasses import replace

from .model import Fix

START = "--- tuner notes (written by hermes-tuner; replaced on each edit) ---"
END = "--- end tuner notes ---"
_LEAD = "\n\n" + START


def strip_block(text: str) -> str:
    """Remove the first well-formed block ("\\n\\n" + START ... END) and nothing else."""
    i = text.find(_LEAD)
    if i < 0:
        return text
    j = text.find(END, i + len(_LEAD))
    if j < 0:
        return text
    return text[:i] + text[j + len(END):]


def splice(current: str, block: str) -> str:
    return strip_block(current) + "\n\n" + START + "\n" + block.strip() + "\n" + END


def check(new: str, current: str | None) -> str | None:
    """A reason `new` is not the current prompt with only its tuner notes block
    replaced, or None."""
    if current is None:
        return "current job prompt unknown"
    if new.count(START) != 1 or new.count(END) != 1 or new.index(END) < new.index(START):
        return "job prompt must contain exactly one tuner notes block"
    if not new.endswith(END):
        return "tuner notes block must end the prompt"
    if strip_block(new) != strip_block(current):
        return "job prompt edit changes the owner's text outside the tuner notes block"
    return None


def with_prompt(fix: Fix, job: dict | None) -> Fix:
    """Turn the drafted block(s) into full prompt text. A job whose prompt is not a
    string leaves the bare block, which check() rejects."""
    owner = job.get("prompt") if isinstance(job, dict) else None

    def full(block: str) -> str:
        return splice(owner, block) if isinstance(owner, str) else block

    p = fix.payload
    if fix.kind == "job_prompt" and isinstance(p.get("block"), str):
        new = {"prompt": full(p["block"]), "add_toolsets": p.get("add_toolsets", [])}
        return replace(fix, payload=new)
    if fix.kind == "composed_tool" and isinstance(p.get("job_prompt"), str):
        return replace(fix, payload=dict(p, job_prompt=full(p["job_prompt"])))
    return fix
