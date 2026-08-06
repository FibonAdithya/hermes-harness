"""Broker configuration: the allowlists the agent cannot write to."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrokerConfig:
    owner_telegram_id: int
    approvals_bot_token: str
    repos: tuple[str, ...]
    ssh_targets: dict[str, str]
    state_dir: Path

    def check_repo(self, repo: str) -> None:
        if repo not in self.repos:
            raise ValueError(f"repo {repo!r} is not allowlisted")


def load_config(path: Path) -> BrokerConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        owner = int(data["owner_telegram_id"])
        token = str(data["approvals_bot_token"])
        repos = tuple(data["repos"])
        targets = dict(data["ssh_targets"])
        state_dir = Path(data["state_dir"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"broker config is invalid: {exc}") from exc
    if not owner:
        raise ValueError("owner_telegram_id must be a non-zero numeric id")
    if not token:
        raise ValueError("approvals_bot_token must be set")
    return BrokerConfig(owner, token, repos, targets, state_dir)
