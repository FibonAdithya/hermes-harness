from __future__ import annotations

import math
from dataclasses import asdict, dataclass
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
    # Hermes's token breakdown (sessions table). input_tokens is FRESH input only;
    # History sets prompt_tokens = input + cache_read + cache_write (Hermes's
    # CanonicalUsage.prompt_tokens). Defaults keep older constructors working.
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Fix:
    id: str
    kind: Literal["composed_tool", "job_prompt", "config", "skill", "new_reach"]
    target: str
    payload: dict
    candidate_key: str


def _rate(d: dict, key: str, optional: bool) -> float | None:
    v = d.get(key)
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError(f"pricing {key} must be a finite number >= 0, got {v!r}")
    return float(v)


@dataclass(frozen=True)
class Pricing:
    """A model's list prices in USD per million tokens, as Hermes resolves them
    (provider /models API, else its dated docs snapshot of the vendor's pricing
    page). A missing cache rate means cache tokens cost the input rate."""
    model: str
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float | None
    cache_write_per_m: float | None
    source: str
    source_url: str | None
    version: str | None
    fetched_at: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Pricing:
        """From `to_dict` output or the bridge's `pricing` result (which names the
        version `pricing_version` and has no fetched_at: 0.0). ValueError when a
        rate is missing, negative or not a number, or the model is empty."""
        if not isinstance(d, dict):
            raise ValueError("pricing must be an object")
        model = d.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("pricing model must be a non-empty string")
        version = d.get("version", d.get("pricing_version"))
        url = d.get("source_url")
        fetched = d.get("fetched_at", 0.0)
        if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
            raise ValueError("pricing fetched_at must be a number")
        if not math.isfinite(fetched):
            raise ValueError("pricing fetched_at must be finite")
        return cls(model=model,
                   input_per_m=_rate(d, "input_per_m", False),  # type: ignore[arg-type]
                   output_per_m=_rate(d, "output_per_m", False),  # type: ignore[arg-type]
                   cache_read_per_m=_rate(d, "cache_read_per_m", True),
                   cache_write_per_m=_rate(d, "cache_write_per_m", True),
                   source=str(d.get("source") or "unknown"),
                   source_url=url if isinstance(url, str) else None,
                   version=version if isinstance(version, str) else None,
                   fetched_at=float(fetched))
