import pytest
from hermes_broker.grants import GrantStore, BOXES, REQUEST_TTL_SECONDS


@pytest.fixture
def store(tmp_path):
    return GrantStore(tmp_path / "grants.json")


def test_no_grant_by_default(store):
    assert store.is_active("tig-gpu", now=1000.0) is False


def test_approve_activates_only_the_requested_box(store):
    code = store.create_request("tig-gpu", 60, "run eval", now=1000.0)
    assert store.approve(code, now=1010.0) == "tig-gpu"
    assert store.is_active("tig-gpu", now=1010.0) is True
    assert store.is_active("tig-server", now=1010.0) is False


def test_grant_expires_after_the_window(store):
    code = store.create_request("tig-gpu", 60, "run eval", now=1000.0)
    store.approve(code, now=1000.0)
    assert store.is_active("tig-gpu", now=1000.0 + 3599) is True
    assert store.is_active("tig-gpu", now=1000.0 + 3601) is False


def test_pending_request_expires(store):
    code = store.create_request("tig-gpu", 60, "run eval", now=1000.0)
    stale = 1000.0 + REQUEST_TTL_SECONDS + 1
    assert store.approve(code, now=stale) is None
    assert store.is_active("tig-gpu", now=stale) is False


def test_unknown_code_is_rejected(store):
    store.create_request("tig-gpu", 60, "run eval", now=1000.0)
    assert store.approve("0000", now=1000.0) is None


def test_code_is_single_use(store):
    code = store.create_request("tig-gpu", 60, "run eval", now=1000.0)
    assert store.approve(code, now=1000.0) == "tig-gpu"
    assert store.approve(code, now=1001.0) is None


def test_new_request_replaces_pending_one_for_same_box(store):
    first = store.create_request("tig-gpu", 60, "eval a", now=1000.0)
    second = store.create_request("tig-gpu", 60, "eval b", now=1001.0)
    assert store.approve(first, now=1002.0) is None
    assert store.approve(second, now=1002.0) == "tig-gpu"


def test_revoke_is_immediate(store):
    code = store.create_request("tig-server", 60, "fix test", now=1000.0)
    store.approve(code, now=1000.0)
    store.revoke("tig-server")
    assert store.is_active("tig-server", now=1001.0) is False


def test_revoke_all(store):
    for box in BOXES:
        code = store.create_request(box, 60, "x", now=1000.0)
        store.approve(code, now=1000.0)
    store.revoke(None)
    assert not any(store.is_active(b, now=1001.0) for b in BOXES)


def test_unknown_box_rejected(store):
    with pytest.raises(ValueError):
        store.create_request("laptop", 60, "x", now=1000.0)


def test_minutes_are_bounded(store):
    with pytest.raises(ValueError):
        store.create_request("tig-gpu", 0, "x", now=1000.0)
    with pytest.raises(ValueError):
        store.create_request("tig-gpu", 481, "x", now=1000.0)


def test_state_survives_a_new_instance(tmp_path):
    path = tmp_path / "grants.json"
    code = GrantStore(path).create_request("tig-gpu", 60, "x", now=1000.0)
    assert GrantStore(path).approve(code, now=1000.0) == "tig-gpu"


def test_file_is_owner_only(store):
    store.create_request("tig-gpu", 60, "x", now=1000.0)
    assert (store.path.stat().st_mode & 0o777) == 0o600
