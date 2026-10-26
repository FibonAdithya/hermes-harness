"""Pattern scan for credentials and one-time codes in note text."""
import re

# A prose value ("password is hunter2x") must hold a digit or symbol, so "the password is short"
# passes; after ":" or "=" any value counts.
_VALUE = r"(?=\S*[0-9!@#$%^&*_])\S{6,}"
_KEYWORD = r"(?:password|passwd|api[ _-]?key)\w*"  # no leading \b: DB_PASSWORD, OPENAI_API_KEY

PATTERNS = [
    ("openai-key", r"sk-[A-Za-z0-9_-]{20,}"),
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("github-pat", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("slack-token", r"xox[bp]-[A-Za-z0-9-]{10,}"),
    ("aws-key", r"AKIA[0-9A-Z]{16}"),
    ("pem", r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"),
    ("bearer", r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ("otp", r"(?i)\b(?:code|otp)\b\D{0,12}\d{6,8}\b|\b\d{6,8}\b\D{0,30}\b(?:code|otp)\b"),
    ("password", rf"(?i){_KEYWORD}(?:\s*[:=]\s*\S+|\s+(?:(?:is|was)\s+)?{_VALUE})"),
]
_COMPILED = [(i, re.compile(p)) for i, p in PATTERNS]


def find_secrets(text: str) -> list[str]:
    return [i for i, rx in _COMPILED if rx.search(text)]
