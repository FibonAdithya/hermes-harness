"""A missing tig-server target must disable task wiring, not crash startup."""
from pathlib import Path

from hermes_broker.config import BrokerConfig
from hermes_broker.listen import _task_wiring


def _cfg(ssh_targets):
    return BrokerConfig(
        owner_telegram_id=1,
        approvals_bot_token="t",
        repos=(),
        ssh_targets=ssh_targets,
        state_dir=Path("/tmp/hermes-broker-test-listen"),
    )


def test_missing_tig_server_disables_task_wiring(caplog):
    tasks, call = _task_wiring(_cfg({}))
    assert tasks is None and call is None


def test_present_tig_server_wires_task_store_and_call():
    tasks, call = _task_wiring(_cfg({"tig-server": "user@host"}))
    assert tasks is not None and callable(call)
