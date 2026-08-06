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
