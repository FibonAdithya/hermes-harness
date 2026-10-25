import dataclasses

import pytest
import yaml

from hermes_friends import render
from hermes_friends.model import Friend
from hermes_friends.owner import OwnerEnv
from hermes_friends.render import (
    GOOGLE_TOOLS, SIGNIN_TOOLS, TELEGRAM_TOOLSETS, render_caddy, render_config,
    render_env, render_memory, render_soul, skill_files,
)


def mk(activated=False, **kw):
    base = dict(name="alice", telegram_id=4242, email="alice@example.com",
                tz="Europe/London", port=8101, cap_usd=5.0, activated=activated)
    base.update(kw)
    return Friend(**base)


OWNER = OwnerEnv(1, "cid-literal", "SECRET-literal")


def cfg(activated):
    return yaml.safe_load(render_config(mk(activated)))


def test_only_google_mcp_server():
    for a in (False, True):
        assert set(cfg(a)["mcp_servers"]) == {"google"}


def test_google_server_command():
    g = cfg(True)["mcp_servers"]["google"]
    assert g["command"] == "uvx" and g["args"][0] == "workspace-mcp"
    assert g["enabled"] is True


def test_preactivation_tools():
    g = cfg(False)["mcp_servers"]["google"]
    assert g["tools"]["include"] == ["start_google_auth"]
    assert "complete" in g["args"] and "extended" not in g["args"]


def test_postactivation_tools():
    g = cfg(True)["mcp_servers"]["google"]
    assert g["tools"]["include"] == list(GOOGLE_TOOLS)
    assert len(GOOGLE_TOOLS) == 7
    assert "start_google_auth" not in g["tools"]["include"]
    assert "extended" in g["args"] and "complete" not in g["args"]


def test_mcp_utility_tools_disabled():
    for a in (False, True):
        t = cfg(a)["mcp_servers"]["google"]["tools"]
        assert t["resources"] is False
        assert t["prompts"] is False


def test_no_send_tool_anywhere():
    for a in (False, True):
        text = render_config(mk(a))
        assert "send_gmail_message" not in text
        args = yaml.safe_load(text)["mcp_servers"]["google"]["args"]
        i = args.index("--permissions")
        assert args[i + 1] == "gmail:drafts"
        assert args[i + 2] == "calendar:full"
        assert args[i - 2:i] == ["--tool-tier", args[i - 1]]


def test_credentials_dir_is_inside_profile():
    env = cfg(False)["mcp_servers"]["google"]["env"]
    assert env["WORKSPACE_MCP_CREDENTIALS_DIR"].startswith(
        "/home/hermes/.hermes/profiles/alice/")


def test_redirect_and_port():
    env = cfg(False)["mcp_servers"]["google"]["env"]
    assert env["GOOGLE_OAUTH_REDIRECT_URI"] == "https://auth.fibonadithya.me/alice/oauth2callback"
    assert env["WORKSPACE_MCP_PORT"] == "8101"
    assert env["USER_GOOGLE_EMAIL"] == "alice@example.com"
    env2 = yaml.safe_load(render_config(mk(port=8150)))["mcp_servers"]["google"]["env"]
    assert env2["WORKSPACE_MCP_PORT"] == "8150"


def test_secrets_not_in_config():
    text = render_config(mk())
    assert "${GOOGLE_OAUTH_CLIENT_SECRET}" in text
    assert "${GOOGLE_OAUTH_CLIENT_ID}" in text
    assert "SECRET-literal" not in text


def test_toolsets_exclude_browser():
    ts = cfg(False)["platform_toolsets"]["telegram"]
    assert ts == TELEGRAM_TOOLSETS
    for bad in ("browser", "image_gen", "delegation", "code_execution",
                "computer_use", "homeassistant"):
        assert bad not in ts


def test_web_via_gateway():
    assert cfg(False)["web"]["use_gateway"] is True
    assert cfg(False)["web"]["backend"] == "firecrawl"


def test_models_pinned():
    c = cfg(False)
    assert c["model"]["provider"] == "nous"
    assert c["model"]["default"]
    assert c["auxiliary"]
    for v in c["auxiliary"].values():
        assert v["provider"] == "nous"
        assert v["model"]


def test_terminal_and_timezone():
    c = cfg(False)
    assert c["terminal"]["backend"] == "docker"
    assert c["terminal"]["container_persistent"] is True
    assert c["terminal"]["container_memory"] == 1024
    assert c["timezone"] == "Europe/London"


def test_env_allowlist():
    env = render_env(mk(), "123:tok", OWNER)
    lines = env.splitlines()
    assert "TELEGRAM_ALLOWED_USERS=4242" in lines
    assert "TERMINAL_ENV=docker" in lines
    assert "TELEGRAM_BOT_TOKEN=123:tok" in lines
    assert "GOOGLE_OAUTH_CLIENT_ID=cid-literal" in lines
    assert "GOOGLE_OAUTH_CLIENT_SECRET=SECRET-literal" in lines


@pytest.mark.parametrize("bad", [0, -5])
def test_env_rejects_nonpositive_id(bad):
    with pytest.raises(ValueError):
        render_env(mk(telegram_id=bad), "t", OWNER)


def test_caddy_block():
    c = render_caddy(mk())
    assert "handle /alice/oauth2callback" in c
    assert "reverse_proxy 127.0.0.1:8101" in c
    assert "rewrite * /oauth2callback?{query}" in c
    assert "handle_path" not in c
    assert "/*" not in c
    assert c.count("handle ") == 1


def test_memory_signin_line_only_before_activation():
    pre = render_memory(mk(False))
    post = render_memory(mk(True))
    assert "start_google_auth" in pre and "alice@example.com" in pre
    assert "start_google_auth" not in post
    for m in (pre, post):
        assert "Alice" in m and "Europe/London" in m


def test_soul_has_no_scanner_phrases():
    s = render_soul(mk()).lower()
    for p in ("ignore", "disregard", "previous instructions", "system prompt",
              "you are now", "jailbreak"):
        assert p not in s
    assert "tell you about it" in s


def test_skill_files():
    files = skill_files()
    assert list(files) == ["email/assistant/SKILL.md"]
    text = files["email/assistant/SKILL.md"].decode()
    assert text.startswith("---\nname:")
    for t in GOOGLE_TOOLS:
        assert t in text
    assert "send_gmail_message" not in text
    assert "start_google_auth" not in text
    for w in ("wiki", "broker", "fleet", "compute"):
        assert w not in text.lower()
