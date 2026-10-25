import pytest

from hermes_friends.owner import read_owner_env


def test_owner_env_reads_values(hermes_root):
    o = read_owner_env(hermes_root)
    assert o.chat_id == 999000111
    assert o.google_client_id == "fake-client-id.apps.example"
    assert o.google_client_secret == "fake-client-secret"


def test_owner_env_rejects_two_ids(hermes_root):
    env = (hermes_root / ".env").read_text()
    for bad in ["1,2", "", "-3", "abc"]:
        (hermes_root / ".env").write_text(
            env.replace("TELEGRAM_ALLOWED_USERS=999000111", f"TELEGRAM_ALLOWED_USERS={bad}"))
        with pytest.raises(ValueError):
            read_owner_env(hermes_root)


def test_owner_env_requires_google(hermes_root):
    (hermes_root / ".env").write_text("TELEGRAM_ALLOWED_USERS=5\nGOOGLE_OAUTH_CLIENT_ID=x\n")
    with pytest.raises(ValueError):
        read_owner_env(hermes_root)
