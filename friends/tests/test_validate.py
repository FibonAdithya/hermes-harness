import pytest

from hermes_friends.policy import gateway_unit
from hermes_friends.validate import (
    validate_bot_token, validate_email, validate_name, validate_telegram_id, validate_tz,
)


def test_name_rules():
    assert validate_name("alice") == "alice"
    assert validate_name("bob2") == "bob2"
    for bad in ["", "Alice", "al-ice", "default", "../x"]:
        with pytest.raises(ValueError):
            validate_name(bad)


def test_telegram_id_rules():
    assert validate_telegram_id("123456") == 123456
    for bad in ["", "0", "-5", "@alice", " 12"]:
        with pytest.raises(ValueError):
            validate_telegram_id(bad)


def test_tz_rules():
    assert validate_tz("Asia/Kolkata") == "Asia/Kolkata"
    for bad in ["Mars/Base", ""]:
        with pytest.raises(ValueError):
            validate_tz(bad)


def test_bot_token_rules():
    t = "123456:" + "A" * 35
    assert validate_bot_token(t) == t
    with pytest.raises(ValueError):
        validate_bot_token("abc")


def test_email_rules():
    assert validate_email("a@example.com") == "a@example.com"
    for bad in ["", "no-at-sign", "a b@example.com", "a@exa mple.com"]:
        with pytest.raises(ValueError):
            validate_email(bad)


def test_gateway_unit():
    assert gateway_unit("alice") == "hermes-gateway-alice"
