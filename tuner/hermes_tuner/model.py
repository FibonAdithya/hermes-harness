from __future__ import annotations

from dataclasses import dataclass


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
