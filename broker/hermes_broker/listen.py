"""Entry point for the approvals listener service."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import box
from .approvals import run_listener
from .config import load_config
from .grants import GrantStore
from .tasks import TaskStore


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(os.environ["HERMES_BROKER_CONFIG"]))
    target = cfg.ssh_targets["tig-server"]
    run_listener(
        GrantStore(cfg.state_dir / "grants.json"),
        cfg.approvals_bot_token,
        cfg.owner_telegram_id,
        tasks=TaskStore(cfg.state_dir / "tasks.json"),
        call=lambda verb, args, timeout=60: box.call(target, verb, args, timeout=timeout),
        repos=cfg.repos,
    )


if __name__ == "__main__":
    main()
