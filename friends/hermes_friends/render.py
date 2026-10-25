"""Pure rendering of a friend's Hermes profile files and Caddy route."""
from importlib import resources

import yaml

from .model import Friend
from .owner import OwnerEnv
from .policy import AUX_MODEL, MAIN_MODEL, PUBLIC_HOST

GOOGLE_TOOLS = (
    "search_gmail_messages",
    "get_gmail_message_content",
    "draft_gmail_message",
    "modify_gmail_message_labels",
    "list_calendars",
    "get_events",
    "manage_event",
)
SIGNIN_TOOLS = ("start_google_auth",)
TELEGRAM_TOOLSETS = ["web", "terminal", "file", "cronjob", "memory", "todo",
                     "session_search", "clarify", "skills"]

PROFILES_ROOT = "/home/hermes/.hermes/profiles"


def render_config(f: Friend) -> str:
    aux = {"provider": "nous", "model": AUX_MODEL}
    cfg = {
        "model": {"default": MAIN_MODEL, "provider": "nous",
                  "base_url": "https://inference-api.nousresearch.com/v1"},
        "auxiliary": {
            "web_extract": dict(aux),
            "compression": dict(aux),
            "title_generation": {"enabled": True, **aux},
        },
        "terminal": {
            "backend": "docker", "cwd": ".", "timeout": 180, "container_cpu": 1,
            "container_memory": 1024, "container_disk": 51200,
            "container_persistent": True, "docker_mount_cwd_to_workspace": False,
            "lifetime_seconds": 300,
        },
        "web": {"backend": "firecrawl", "use_gateway": True},
        "timezone": f.tz,
        "platform_toolsets": {"telegram": list(TELEGRAM_TOOLSETS)},
        "mcp_servers": {
            "google": {
                "command": "uvx",
                "args": ["workspace-mcp", "--single-user", "--tool-tier",
                         "extended" if f.activated else "complete",
                         "--permissions", "gmail:drafts", "calendar:full"],
                "env": {
                    "GOOGLE_OAUTH_CLIENT_ID": "${GOOGLE_OAUTH_CLIENT_ID}",
                    "GOOGLE_OAUTH_CLIENT_SECRET": "${GOOGLE_OAUTH_CLIENT_SECRET}",
                    "USER_GOOGLE_EMAIL": f.email,
                    "WORKSPACE_MCP_PORT": str(f.port),
                    "GOOGLE_OAUTH_REDIRECT_URI":
                        f"https://{PUBLIC_HOST}/{f.name}/oauth2callback",
                    "WORKSPACE_MCP_CREDENTIALS_DIR":
                        f"{PROFILES_ROOT}/{f.name}/google-credentials",
                },
                "enabled": True,
                "tools": {
                    "include": list(GOOGLE_TOOLS if f.activated else SIGNIN_TOOLS),
                    "resources": False,
                    "prompts": False,
                },
            }
        },
    }
    return yaml.safe_dump(cfg, sort_keys=False)


def render_env(f: Friend, bot_token: str, owner: OwnerEnv) -> str:
    if not isinstance(f.telegram_id, int) or isinstance(f.telegram_id, bool) or f.telegram_id < 1:
        raise ValueError("telegram_id must be a positive integer")
    return (
        f"TELEGRAM_BOT_TOKEN={bot_token}\n"
        f"TELEGRAM_ALLOWED_USERS={f.telegram_id}\n"
        "TERMINAL_ENV=docker\n"
        "TERMINAL_DOCKER_IMAGE=hermes-sandbox:latest\n"
        "TERMINAL_DOCKER_NETWORK=false\n"
        f"GOOGLE_OAUTH_CLIENT_ID={owner.google_client_id}\n"
        f"GOOGLE_OAUTH_CLIENT_SECRET={owner.google_client_secret}\n"
    )


def _template(*parts: str):
    t = resources.files("hermes_friends").joinpath("template")
    for p in parts:
        t = t.joinpath(p)
    return t


def render_soul(f: Friend) -> str:
    return _template("SOUL.md").read_text(encoding="utf-8")


def signin_line(f: Friend) -> str:
    return ("On the user's first message, call start_google_auth for "
            f"{f.email} and send them the link.")


def render_memory(f: Friend) -> str:
    lines = [f"The user's first name is {f.name.capitalize()}.",
             f"Their timezone is {f.tz}."]
    if not f.activated:
        lines.append(signin_line(f))
    return "\n".join(lines) + "\n"


def render_caddy(f: Friend) -> str:
    return (
        f"handle /{f.name}/oauth2callback {{\n"
        "\trewrite * /oauth2callback?{query}\n"
        f"\treverse_proxy 127.0.0.1:{f.port}\n"
        "}\n"
    )


def skill_files() -> dict[str, bytes]:
    out: dict[str, bytes] = {}

    def walk(node, prefix):
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            if child.is_dir():
                if child.name != "__pycache__":
                    walk(child, f"{prefix}{child.name}/")
            else:
                out[f"{prefix}{child.name}"] = child.read_bytes()

    walk(_template("skills"), "")
    return out
