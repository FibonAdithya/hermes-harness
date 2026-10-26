"""Pattern scan for credentials and one-time codes in note text."""
import re

PATTERNS = [
    ("openai-key", r"sk-[A-Za-z0-9_-]{20,}"),
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("github-pat", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("slack-token", r"xox[bp]-[A-Za-z0-9-]{10,}"),
    ("aws-key", r"AKIA[0-9A-Z]{16}"),
    ("pem", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("bearer", r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ("otp", r"(?i)\b(code|otp)\b\D{0,12}\d{6,8}\b"),
    ("password", r"(?i)\b(password|passwd|api[ _-]?key)\b\s*[:=]\s*\S+"),
]
_COMPILED = [(i, re.compile(p)) for i, p in PATTERNS]


def find_secrets(text: str) -> list[str]:
    return [i for i, rx in _COMPILED if rx.search(text)]
