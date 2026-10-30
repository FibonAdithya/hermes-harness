"""Policy constants. Single source; nothing else hard-codes these."""
import re

# Config keys the tuner may change through the bridge (spec 5.1).
CONFIG_ALLOW_PREFIXES = ("tools.tool_search.", "compression.")
# Always rejected, even if a prefix above would match.
CONFIG_FORBIDDEN_PREFIXES = (
    "mcp_servers.", "terminal.", "model", "provider", "auxiliary.",
    "platform_toolsets", "delegation.", "plugins.",
)

# Composed tools (spec 5.2).
TOOL_PREFIX = "tuned_"
# A composed tool's name (and its directory name): limits, ledger and plugin share it.
TOOL_NAME_RE = re.compile(re.escape(TOOL_PREFIX) + r"[A-Za-z0-9_]{1,58}")
TOOLSET = "tuner"
SANDBOX_ARGV = (
    "docker", "run", "--rm", "-i", "--network=none", "--memory", "512m",
    "--pids-limit", "64", "--cpus", "1", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--read-only", "--user", "65534:65534",
)
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

# Shadow (dry-run trial) sessions: the plugin's pre_tool_call guard lets only
# these tools run; every other tool is blocked and recorded (spec 4.5). An
# allowlist, because Hermes has many state-changing tools beyond MUTATING_TOOLS.
# tool_call (the Tool Search bridge) is NOT allowed: when Hermes unwraps it
# (agent/tool_executor.py) the hook fires once, under the underlying tool's name,
# which is checked here as usual; when the unwrap fails Hermes swallows the error
# and dispatches tool_call itself with the hook skipped on the recursion, so a
# tool_call that reaches the guard must be blocked.
# Composed tools (TOOL_PREFIX) are allowed because they force a dry run themselves.
SHADOW_ALLOW = (CALLABLE_TOOLS - MUTATING_TOOLS) | frozenset({
    "tool_search",
    "tool_describe",
    "skill_view",
    "skills_list",
    "session_search",
    "todo",
})


def shadow_blocked(name: str) -> bool:
    """True when a shadow session must not run tool `name`."""
    return not name.startswith(TOOL_PREFIX) and name not in SHADOW_ALLOW


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
# Drafting pauses only when 28-day spend exceeds both 28-day savings and this allowance
# (spec 6). Without it, any spend before the first saving (a failed draft) paused
# drafting until that spend aged out. ESTIMATE, same size as the weekly cap.
PAUSE_ALLOWANCE_USD = 1.0
DRAFT_EST_USD = 0.02  # ESTIMATE
# Every tuner decision and figure prices a run from its token breakdown at the
# configured model's list prices (bridge `pricing` -> model.Pricing, then
# measure.priced_cost), never from Hermes's estimated_cost_usd: that changed regime
# when billing moved to a subscription (~2026-10-17, $0/NULL). One list prices both
# sides of every comparison. When the lookup fails, the list stored in
# cycle_state.json is used if fetched at most this long ago; else no pricing.
PRICING_MAX_AGE_S = 7 * 86400
JOB_TRIAL_RUNS = 5
GLOBAL_TRIAL_DAYS = 7
# A trial with no verdict this long after live_from (or applied_at while it waits
# for a restart) is rolled back.
TRIAL_DEADLINE_DAYS = 21
TRIAGE_AGREEMENT_MIN = 0.90
SHADOW_LEAD_MIN = 20
SHADOW_PREFIX = "tuner-shadow-"

# Gateway restart window (spec 4.7).
RESTART_WINDOW = (2, 5)
RESTART_TZ = "Europe/London"
QUIET_MIN = 30
JOB_MARGIN_MIN = 15

# Carried-bytes ranking (tuner design 2026-10-28).
CHARS_PER_TOKEN = 4  # ESTIMATE: messages.token_count is NULL on every tool row
MIN_DRAFT_MONTHLY_USD = 0.05  # ESTIMATE
# A parked candidate (backoff) may draft again this many days after its newest
# outcome: the drafting request's tool schemas are not hashed (it sends {}), so a
# change there could not unpark it otherwise.
PARK_MAX_DAYS = 28

# Result reducers (tuner design 2026-10-28). Calendar sync only.
REDUCER_JOBS = frozenset({"cc68e24f21cd"})
REDUCER_TOOLS = frozenset({
    "mcp__google__get_gmail_message_content",
    "mcp__google__search_gmail_messages",
    "mcp__google__get_events",
})
# A subdomain matches too: us02web.zoom.us matches zoom.us.
MEETING_HOSTS = frozenset({"meet.google.com", "zoom.us", "teams.microsoft.com"})
REDUCER_CAP_LADDER = (16000, 8000, 4000, 2000)
# A reducer is drafted only if it cuts at least this share of the tool's replayed
# bytes (ESTIMATE, tunable; below the measured one-third).
REDUCER_MIN_CUT = 0.15
# The greedy search stops when the best next step cuts less than this share more.
REDUCER_STEP_MIN = 0.02
# Fewer replayed runs with a result for the tool than this: no reducer.
REPLAY_MIN_SESSIONS = 3
# Days of recorded history replayed and priced.
REPLAY_WINDOW_DAYS = 28
# Composed-tool replay (tuner design 2026-10-28): the newest this-many replayable runs,
# each under this sandbox timeout, all composed replays of one cycle sharing
# REPLAY_CYCLE_BUDGET_S. A run starts only while REPLAY_TIMEOUT_S + 30 s of the budget
# is left, so replay ends within 360 s + 60 s + relay's reap/docker-rm slack. Worst
# case per cycle: 3 drafts x 360 s + ~420 s of replay = ~1500 s (ESTIMATE), inside
# the service's TimeoutStartSec=1800. Without the shared budget, 2 composed drafts
# (one per editable job) x 6 x ~65 s took the total to ~1860 s.
REPLAY_MAX_SESSIONS = 6
REPLAY_TIMEOUT_S = 60
REPLAY_CYCLE_BUDGET_S = 360

# Replay used-string check (tuner design 2026-10-28). A reducer goes live only if
# every string the model reused verbatim from a result survives the reduction.
USED_MIN_CHARS = 8
# Only the first this-many normalised chars of a result and of each later message
# are compared; used strings past that point are not found.
USED_MAX_CHARS = 50_000
# Drop strings present in more than this share of a tool's results.
BOILERPLATE_SHARE = 0.5
# With fewer results than this in the window, nothing counts as boilerplate.
BOILERPLATE_MIN_RESULTS = 4
