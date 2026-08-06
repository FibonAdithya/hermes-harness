import pytest
from hermes_broker.grants import GrantStore
from hermes_broker.server import Locked, require_grant


@pytest.fixture
def store(tmp_path):
    return GrantStore(tmp_path / "grants.json")


def test_locked_without_a_grant(store):
    with pytest.raises(Locked) as exc:
        require_grant(store, "tig-server", now=1000.0)
    assert "LOCKED" in str(exc.value)
    assert "tig-server" in str(exc.value)


def test_unlocked_with_a_live_grant(store):
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    store.approve(code, now=1000.0)
    require_grant(store, "tig-server", now=1001.0)


def test_grant_for_one_box_does_not_unlock_another(store):
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    store.approve(code, now=1000.0)
    with pytest.raises(Locked):
        require_grant(store, "tig-gpu", now=1001.0)


def test_expired_grant_locks_again(store):
    code = store.create_request("tig-gpu", 1, "quick job", now=1000.0)
    store.approve(code, now=1000.0)
    require_grant(store, "tig-gpu", now=1030.0)
    with pytest.raises(Locked):
        require_grant(store, "tig-gpu", now=1061.0)


def test_agent_cannot_self_approve(store):
    """Nothing the agent calls may issue a grant; only the approvals bot does."""
    code = store.create_request("tig-gpu", 30, "x", now=1000.0)
    with pytest.raises(Locked):
        require_grant(store, "tig-gpu", now=1001.0)
    assert store.approve(code, now=1001.0) == "tig-gpu"
