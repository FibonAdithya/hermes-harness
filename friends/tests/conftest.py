import json

import pytest

OWNER_TOKEN = "111111:" + "O" * 35
APPROVALS_TOKEN = "222222:" + "P" * 35


@pytest.fixture
def hermes_root(tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    (root / "broker").mkdir()
    (root / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={OWNER_TOKEN}\n"
        "TELEGRAM_ALLOWED_USERS=999000111\n"
        "GOOGLE_OAUTH_CLIENT_ID=fake-client-id.apps.example\n"
        "GOOGLE_OAUTH_CLIENT_SECRET=fake-client-secret\n"
    )
    (root / "broker" / "broker.json").write_text(
        json.dumps({"approvals_bot_token": APPROVALS_TOKEN})
    )
    return root
