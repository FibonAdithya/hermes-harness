from hermes_broker.approvals import parse_command

OWNER = 111222333


def msg(text, **extra):
    message = {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER}, "text": text}
    message.update(extra)
    return {"update_id": 9, "message": message}


def test_approve_is_parsed():
    assert parse_command(msg("approve 7391"), OWNER) == ("approve", "7391")


def test_approve_is_case_insensitive_and_trims():
    assert parse_command(msg("  Approve 7391 "), OWNER) == ("approve", "7391")


def test_revoke_is_parsed():
    assert parse_command(msg("revoke"), OWNER) == ("revoke", "")


def test_other_text_ignored():
    assert parse_command(msg("hello"), OWNER) is None
    assert parse_command(msg("approve"), OWNER) is None
    assert parse_command(msg("approve 73911"), OWNER) is None
    assert parse_command(msg("approve abcd"), OWNER) is None


def test_non_owner_ignored():
    update = msg("approve 7391")
    update["message"]["from"]["id"] = 999
    assert parse_command(update, OWNER) is None


def test_forwarded_message_ignored():
    """The injection road of As-built #20: someone else's text, forwarded in."""
    assert parse_command(msg("approve 7391", forward_origin={"type": "user"}), OWNER) is None


def test_legacy_forward_fields_ignored():
    assert parse_command(msg("approve 7391", forward_from={"id": 5}), OWNER) is None
    assert parse_command(msg("approve 7391", forward_sender_name="Someone"), OWNER) is None
    assert parse_command(msg("approve 7391", forward_date=123456), OWNER) is None


def test_edited_message_ignored():
    assert parse_command({"update_id": 9, "edited_message": {"text": "approve 7391"}}, OWNER) is None


def test_non_message_update_ignored():
    assert parse_command({"update_id": 9, "callback_query": {"data": "approve 7391"}}, OWNER) is None


def test_missing_text_ignored():
    update = msg("approve 7391")
    del update["message"]["text"]
    assert parse_command(update, OWNER) is None


def test_deploy_is_parsed_lowercased():
    assert parse_command(msg("deploy 91a2e06"), OWNER) == ("deploy", "91a2e06")
    assert parse_command(msg(" Deploy 91A2E066C768 "), OWNER) == ("deploy", "91a2e066c768")


def test_deploy_needs_seven_to_forty_hex():
    assert parse_command(msg("deploy 91a2e0"), OWNER) is None
    assert parse_command(msg("deploy " + "a" * 41), OWNER) is None
    assert parse_command(msg("deploy master"), OWNER) is None
    assert parse_command(msg("deploy"), OWNER) is None


def test_deploy_from_non_owner_or_forwarded_ignored():
    update = msg("deploy 91a2e06")
    update["message"]["from"]["id"] = 999
    assert parse_command(update, OWNER) is None
    assert parse_command(msg("deploy 91a2e06", forward_origin={"type": "user"}), OWNER) is None


def test_deploy_reply_names_the_full_commit(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "grants.json")
    sha = "91a2e066c76837d9ac80243755ebc628993a0454"
    store.create_deploy_request(sha, now=1000.0)
    assert handle(store, "deploy", "0000000", now=1001.0) == "No pending deploy of a commit starting 0000000."
    assert handle(store, "deploy", "91a2e06", now=1002.0) == f"Deploy of {sha} to tig-server approved for 10 min."
    assert store.take_deploy_grant(now=1003.0) == sha


def test_approve_and_revoke_replies_unchanged(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "grants.json")
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    assert handle(store, "approve", "0000" if code != "0000" else "0001", now=1001.0) == "No pending request with that code."
    assert handle(store, "approve", code, now=1001.0) == "Granted tig-server for 30 min.\nFor: fix parser"
    assert handle(store, "revoke", "", now=1002.0) == "Revoked. All boxes locked."
    assert not store.is_active("tig-server", now=1003.0)
