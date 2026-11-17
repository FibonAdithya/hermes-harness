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


def test_proposal_wiring_does_not_need_tig_server():
    from hermes_broker.listen import _proposal_wiring
    cfg = _cfg({})
    store, tuner_dir = _proposal_wiring(cfg)
    assert store is not None and tuner_dir == cfg.tuner_dir
    assert store.path == cfg.state_dir / "proposals.json"


def test_human_wiring_needs_tig_server():
    from hermes_broker.listen import _human_wiring
    assert _human_wiring(_cfg({})) is None
    cfg = _cfg({"tig-server": "user@host"})
    humans = _human_wiring(cfg)
    assert humans is not None and humans.path == cfg.state_dir / "human_codes.json"


def test_main_passes_the_human_store_to_the_listener(tmp_path, monkeypatch):
    import json
    import logging

    from hermes_broker import listen
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({
        "owner_telegram_id": 5, "approvals_bot_token": "123:abc", "repos": [],
        "ssh_targets": {"tig-server": "user@host"}, "state_dir": str(tmp_path / "state")}))
    monkeypatch.setenv("HERMES_BROKER_CONFIG", str(config))
    started = {}
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        listen.main(listener=lambda store, token, owner_id, **kw: started.update(kw), run=lambda *a, **k: None)
    finally:
        root.handlers[:], root.level = handlers, level
    assert started["humans"].path == tmp_path / "state" / "human_codes.json"
    assert started["tasks"].path == tmp_path / "state" / "tasks.json"
