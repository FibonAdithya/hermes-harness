import pytest

from hermes_friends.owner import read_env, read_owner_env


def test_owner_env_reads_values(hermes_root):
    o = read_owner_env(hermes_root)
    assert o.chat_id == 999000111
    assert o.google_client_id == "fake-client-id.apps.example"
    assert o.google_client_secret == "fake-client-secret"


def test_owner_env_rejects_two_ids(hermes_root):
    env = (hermes_root / ".env").read_text()
    for bad in ["1,2", "", "0", "-3", "abc"]:
        (hermes_root / ".env").write_text(
            env.replace("TELEGRAM_ALLOWED_USERS=999000111", f"TELEGRAM_ALLOWED_USERS={bad}"))
        with pytest.raises(ValueError):
            read_owner_env(hermes_root)


def test_owner_env_requires_google(hermes_root):
    base = "TELEGRAM_ALLOWED_USERS=5\n"
    for text in [base + "GOOGLE_OAUTH_CLIENT_ID=x\n", base + "GOOGLE_OAUTH_CLIENT_SECRET=y\n"]:
        (hermes_root / ".env").write_text(text)
        with pytest.raises(ValueError):
            read_owner_env(hermes_root)


@pytest.mark.parametrize("line", [
    "K=abc", "export K=abc", "export   K = abc", "  K=abc  ", "K=abc # note",
    "K='abc'", 'K="abc"', "export K=\"abc\" # note",
])
def test_read_env_forms(tmp_path, line):
    p = tmp_path / ".env"
    p.write_text("# comment\n\n" + line + "\n")
    assert read_env(p) == {"K": "abc"}


def test_read_env_quoted_keeps_hash(tmp_path):
    p = tmp_path / ".env"
    p.write_text("K='a #b'\nJ=a#b\n")
    assert read_env(p) == {"K": "a #b", "J": "a#b"}
