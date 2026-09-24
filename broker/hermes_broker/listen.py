"""Entry point for the approvals listener service."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .approvals import run_listener
from .config import load_config
from .grants import GrantStore


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(os.environ["HERMES_BROKER_CONFIG"]))
    run_listener(
        GrantStore(cfg.state_dir / "grants.json"),
        cfg.approvals_bot_token,
        cfg.owner_telegram_id,
    )


if __name__ == "__main__":
    main()
