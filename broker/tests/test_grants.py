import json
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


# ---- deploy grants: bound to one commit, single use ---------------------------

SHA = "91a2e066c76837d9ac80243755ebc628993a0454"
OTHER = "35b4e9e0000000000000000000000000000000aa"


def test_deploy_is_not_a_box_request_access_can_ask_for(store):
    with pytest.raises(ValueError):
        store.create_request("deploy", 10, "x", now=1000.0)


def test_approved_deploy_grants_exactly_that_commit_once(store):
    store.create_deploy_request(SHA, now=1000.0)
    assert store.approve_deploy(SHA[:7], now=1010.0) == SHA
    assert store.take_deploy_grant(now=1020.0) == SHA
    assert store.take_deploy_grant(now=1021.0) is None


def test_deploy_prefix_must_match_the_requested_commit(store):
    store.create_deploy_request(SHA, now=1000.0)
    assert store.approve_deploy(OTHER[:12], now=1010.0) is None
    assert store.take_deploy_grant(now=1011.0) is None
    # a mistyped prefix does not burn the request
    assert store.approve_deploy(SHA[:12].upper(), now=1012.0) == SHA


def test_deploy_prefix_shorter_than_seven_is_refused(store):
    store.create_deploy_request(SHA, now=1000.0)
    assert store.approve_deploy(SHA[:6], now=1010.0) is None
    assert store.take_deploy_grant(now=1011.0) is None


def test_pending_deploy_expires(store):
    store.create_deploy_request(SHA, now=1000.0)
    assert store.approve_deploy(SHA, now=1000.0 + REQUEST_TTL_SECONDS + 1) is None


def test_deploy_grant_lasts_ten_minutes(store):
    store.create_deploy_request(SHA, now=1000.0)
    store.approve_deploy(SHA, now=1000.0)
    assert store.take_deploy_grant(now=1000.0 + 601) is None
    store.create_deploy_request(SHA, now=2000.0)
    store.approve_deploy(SHA, now=2000.0)
    assert store.take_deploy_grant(now=2000.0 + 599) == SHA


def test_box_grant_does_not_unlock_deploy(store):
    code = store.create_request("tig-server", 60, "x", now=1000.0)
    store.approve(code, now=1000.0)
    assert store.take_deploy_grant(now=1001.0) is None


def test_revoke_all_clears_deploys(store):
    store.create_deploy_request(SHA, now=1000.0)
    store.approve_deploy(SHA, now=1000.0)
    store.create_deploy_request(OTHER, now=1001.0)
    store.revoke(None)
    assert store.take_deploy_grant(now=1002.0) is None
    assert store.approve_deploy(OTHER, now=1002.0) is None


def test_request_codes_avoid_live_task_codes(store, monkeypatch):
    import hermes_broker.grants as grants
    draws = iter([4321, 1111])
    monkeypatch.setattr(grants.secrets, "randbelow", lambda n: next(draws))
    assert store.create_request("tig-server", 30, "r", now=1.0, avoid={"4321"}) == "1111"


def test_pending_codes_lists_unexpired_requests(store):
    code = store.create_request("tig-server", 30, "r", now=1.0)
    assert store.pending_codes(now=2.0) == {code}
    assert store.pending_codes(now=1.0 + 121) == set()


SHA_F = "f" * 40


def test_a_fleet_deploy_grant_is_not_a_harness_grant_and_is_not_spent_by_asking(store):
    store.create_deploy_request(SHA_F, now=1.0, target="fleet")
    # amended 2026-09-26 in review: a bare approval (target defaults to harness) must not
    # grant a fleet request -- this is the exact vulnerability the review round fixed.
    assert store.approve_deploy(SHA_F[:7], now=2.0) is None
    assert store.approve_deploy(SHA_F[:7], now=2.0, target="fleet") == SHA_F
    assert store.deploy_target() == "fleet"
    assert store.take_deploy_grant(now=3.0) is None            # harness asks: refused, not consumed
    assert store.take_deploy_grant(now=3.0, target="fleet") == SHA_F
    assert store.take_deploy_grant(now=3.0, target="fleet") is None


def test_unknown_deploy_target_rejected(store):
    with pytest.raises(ValueError):
        store.create_deploy_request(SHA_F, now=1.0, target="droplet")


def test_approve_deploy_with_wrong_target_keeps_request_pending(store):
    """Fix round 1, item 1 mutation target: drop the target comparison in approve_deploy."""
    store.create_deploy_request(SHA_F, now=1000.0, target="fleet")
    assert store.approve_deploy(SHA_F[:7], now=1001.0, target="harness") is None
    assert store.approve_deploy(SHA_F[:7], now=1002.0, target="fleet") == SHA_F


def test_expired_fleet_grant_is_consumed_and_gone(store):
    """Fix round 1, item 4b."""
    store.create_deploy_request(SHA_F, now=1000.0, target="fleet")
    assert store.approve_deploy(SHA_F[:7], now=1000.0, target="fleet") == SHA_F
    assert store.take_deploy_grant(now=1000.0 + 601, target="fleet") is None
    # the grant is gone, not merely read as expired-in-place: a second ask also finds nothing
    assert store.take_deploy_grant(now=1000.0 + 602, target="fleet") is None


def test_a_reply_sent_before_the_request_existed_is_refused_and_the_request_stays(store):
    """`now` is the reply's message date: a reply older than the request cannot be approving it."""
    code = store.create_request("tig-gpu", 60, "run eval", now=1000.0)
    assert store.approve(code, now=999.0) is None
    assert store.is_active("tig-gpu", now=1000.0) is False
    assert store.approve(code, now=1001.0) == "tig-gpu"


def test_a_legacy_request_without_created_at_uses_expires_at_minus_ttl(store):
    import json
    from hermes_broker.grants import REQUEST_TTL_SECONDS
    store.path.write_text(json.dumps({"pending": {"4321": {"box": "tig-gpu", "minutes": 60, "reason": "r",
                                                           "expires_at": 1000.0 + REQUEST_TTL_SECONDS}},
                                      "grants": {}}))
    assert store.approve("4321", now=999.0) is None
    assert store.approve("4321", now=1000.0) == "tig-gpu"


def test_a_deploy_reply_sent_before_the_request_existed_is_refused_and_the_request_stays(store):
    sha = "a" * 40
    store.create_deploy_request(sha, now=1000.0, target="fleet")
    assert store.approve_deploy(sha[:7], now=999.0, target="fleet") is None
    assert store.approve_deploy(sha[:7], now=1001.0, target="fleet") == sha


SHA = "4774e2314f46" + "0" * 28


def test_auto_request_is_valid_for_24h(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=23 * 3600) == SHA
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=25 * 3600) is None


def test_agent_request_still_expires_at_120s(store):
    store.create_deploy_request(SHA, now=0.0)
    assert store.approve_deploy("4774e23", now=121.0) is None
    store.create_deploy_request(SHA, now=0.0)
    assert store.approve_deploy("4774e23", now=119.0) == SHA


def test_auto_approval_writes_no_grant(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=10.0) == SHA
    data = json.loads(store.path.read_text())
    assert "deploy" not in data["grants"] and "pending_deploy" not in data
    assert store.take_deploy_grant(now=11.0) is None


def test_approve_deploy_refuses_an_auto_request(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_deploy("4774e23", now=10.0) is None
    assert store.pending_deploy_origin(10.0) == "auto"


def test_approve_auto_deploy_refuses_an_agent_request(store):
    store.create_deploy_request(SHA, now=0.0)
    assert store.approve_auto_deploy("4774e23", now=10.0) is None
    assert store.pending_deploy_origin(10.0) == "agent"


def test_auto_wrong_target_or_prefix_keeps_it_pending(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=10.0, target="fleet") is None
    assert store.approve_auto_deploy("deadbee", now=10.0) is None
    assert store.approve_auto_deploy("4774e23", now=10.0) == SHA


def test_auto_prefix_is_lowercased_and_needs_seven_chars(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e2", now=10.0) is None
    assert store.approve_auto_deploy("4774E23", now=10.0) == SHA


def test_auto_reply_sent_before_the_request_existed_is_refused(store):
    store.create_deploy_request(SHA, now=100.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=99.0) is None
    assert store.pending_deploy_origin(101.0) == "auto"


def test_expired_auto_request_is_popped(store):
    store.create_deploy_request(SHA, now=0.0, ttl_seconds=86400, origin="auto")
    assert store.approve_auto_deploy("4774e23", now=86401.0) is None
    assert "pending_deploy" not in json.loads(store.path.read_text())


def test_record_without_origin_is_agent(store):
    store.path.write_text(json.dumps({"pending": {}, "grants": {}, "pending_deploy": {
        "sha": SHA, "target": "harness", "created_at": 0, "expires_at": 120}}))
    assert store.pending_deploy_origin(10.0) == "agent"
    assert store.approve_auto_deploy("4774e23", now=10.0) is None
    assert store.approve_deploy("4774e23", now=10.0) == SHA


def test_pending_deploy_origin_none_when_absent_or_expired(store):
    assert store.pending_deploy_origin(0.0) is None
    store.create_deploy_request(SHA, now=0.0)
    assert store.pending_deploy_origin(121.0) is None


@pytest.mark.parametrize("setup, busy", [
    ("agent_harness", True), ("agent_fleet", True), ("auto", False),
    ("grant", True), ("nothing", False)])
def test_deploy_slot_busy(store, setup, busy):
    if setup == "agent_harness":
        store.create_deploy_request(SHA, 0.0)
    elif setup == "agent_fleet":
        store.create_deploy_request(SHA, 0.0, target="fleet")
    elif setup == "auto":
        store.create_deploy_request(SHA, 0.0, ttl_seconds=86400, origin="auto")
    elif setup == "grant":
        store.create_deploy_request(SHA, 0.0)
        assert store.approve_deploy("4774e23", now=1.0) == SHA
    assert store.deploy_slot_busy(50.0) is busy
    if busy:
        assert store.deploy_slot_busy(10_000.0) is False


def test_unknown_origin_rejected(store):
    with pytest.raises(ValueError):
        store.create_deploy_request(SHA, now=0.0, origin="owner")
