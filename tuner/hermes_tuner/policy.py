"""Policy constants. Single source; nothing else hard-codes these."""

# Config keys the tuner may change through the bridge (spec 5.1).
CONFIG_ALLOW_PREFIXES = ("tools.tool_search.", "compression.")
# Always rejected, even if a prefix above would match.
CONFIG_FORBIDDEN_PREFIXES = (
    "mcp_servers.", "terminal.", "model", "provider", "auxiliary.",
    "platform_toolsets", "delegation.", "plugins.",
)

# Composed tools (spec 5.2).
TOOL_PREFIX = "tuned_"
TOOLSET = "tuner"
SANDBOX_ARGV = ("docker", "run", "--rm", "-i", "--network=none", "--memory", "512m")
SANDBOX_IMAGE = "hermes-sandbox:latest"
RUN_TIMEOUT_S = 300
MAX_CALLS = 50
MAX_OUTPUT_BYTES = 50_000
COMPOSED_TOOL_NEEDS_RESTART = True

COMPUTE_READ_TOOLS = frozenset({
    "mcp__compute__night_status",
    "mcp__compute__night_log",
    "mcp__compute__list_repos",
    "mcp__compute__job_status",
    "mcp__compute__job_logs",
})
CALLABLE_TOOLS = frozenset({
    "mcp__google__search_gmail_messages",
    "mcp__google__get_gmail_message_content",
    "mcp__google__modify_gmail_message_labels",
    "mcp__google__draft_gmail_message",
    "mcp__google__list_calendars",
    "mcp__google__get_events",
    "mcp__google__manage_event",
    "web_search",
    "web_extract",
    "read_file",
    "search_files",
}) | COMPUTE_READ_TOOLS
MUTATING_TOOLS = frozenset({
    "mcp__google__modify_gmail_message_labels",
    "mcp__google__manage_event",
    "mcp__google__draft_gmail_message",
    "write_file",
    "patch",
    "terminal",
})

# Untrusted tool results (spec 5.3).
UNTRUSTED_PREFIXES = ("mcp__google__", "browser_")
UNTRUSTED_TOOLS = frozenset({"web_search", "web_extract", "session_search"})
# read_file is untrusted unless the path is under one of these.
TRUSTED_READ_ROOTS = ("/root/hermes-wiki/", "/root/.hermes/skills/")
COPY_MIN_CHARS = 40

# Jobs.
EDITABLE_JOBS = {"3bc4e11d29b1": "triage", "cc68e24f21cd": "calendar"}
NEVER_TOUCH_JOBS = frozenset({"8da9a6857256"})

# Spend and trials.
WEEKLY_CAP_USD = 1.0
DRAFT_EST_USD = 0.02  # ESTIMATE
JOB_TRIAL_RUNS = 5
GLOBAL_TRIAL_DAYS = 7
TRIAGE_AGREEMENT_MIN = 0.90
SHADOW_LEAD_MIN = 20
SHADOW_PREFIX = "tuner-shadow-"

# Gateway restart window (spec 4.7).
RESTART_WINDOW = (2, 5)
RESTART_TZ = "Europe/London"
QUIET_MIN = 30
JOB_MARGIN_MIN = 15
