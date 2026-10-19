"""Entry point for the approvals listener service."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import box
from .approvals import Call, run_listener
from .config import BrokerConfig, load_config
from .grants import GrantStore
from .proposals import ProposalStore
from .tasks import TaskStore

logger = logging.getLogger(__name__)


def _task_wiring(cfg: BrokerConfig) -> tuple[TaskStore | None, Call | None]:
    """Task announcer/reply wiring, or (None, None) if tig-server isn't configured.

    A missing tig-server target must not take grant approval (approve/deploy/revoke)
    down with it, so this degrades instead of raising.
    """
    target = cfg.ssh_targets.get("tig-server")
    if target is None:
        logger.error("tig-server not in ssh_targets; task announcer and replies disabled")
        return None, None
    return (
        TaskStore(cfg.state_dir / "tasks.json"),
        lambda verb, args, timeout=60: box.call(target, verb, args, timeout=timeout),
    )


def _proposal_wiring(cfg: BrokerConfig) -> tuple[ProposalStore, Path]:
    """Proposal announcing and the weekly outbox need only local files, not tig-server."""
    return ProposalStore(cfg.state_dir / "proposals.json"), cfg.tuner_dir


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(os.environ["HERMES_BROKER_CONFIG"]))
    tasks, call = _task_wiring(cfg)
    proposals, tuner_dir = _proposal_wiring(cfg)
    run_listener(
        GrantStore(cfg.state_dir / "grants.json"),
        cfg.approvals_bot_token,
        cfg.owner_telegram_id,
        tasks=tasks,
        call=call,
        repos=cfg.repos,
        proposals=proposals,
        tuner_dir=tuner_dir,
        state_dir=cfg.state_dir,
    )


if __name__ == "__main__":
    main()
