from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arg_keys: tuple[str, ...]
    ref: str | None
    result_chars: int


@dataclass(frozen=True)
class Session:
    id: str
    source: str
    job_id: str | None
    started_at: float
    est_cost: float
    prompt_tokens: int
    output_tokens: int
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class Fix:
    id: str
    kind: Literal["composed_tool", "job_prompt", "config", "skill", "new_reach"]
    target: str
    payload: dict
    candidate_key: str
