import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_NAME = re.compile(r"[a-z0-9]{1,32}")
_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def validate_name(s: str) -> str:
    if s == "default":
        raise ValueError("name 'default' is reserved")
    if not _NAME.fullmatch(s):
        raise ValueError("name must be 1-32 lowercase letters and digits")
    return s


def validate_telegram_id(s: str) -> int:
    if not re.fullmatch(r"[0-9]+", s) or int(s) <= 0:
        raise ValueError("telegram id must be a positive integer")
    return int(s)


def validate_tz(s: str) -> str:
    if not s:
        raise ValueError("timezone is empty")
    try:
        ZoneInfo(s)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown timezone: {s!r}") from None
    return s


def validate_email(s: str) -> str:
    if len(s) > 254 or not re.fullmatch(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", s):
        raise ValueError("email must look like name@host.tld (letters, digits and ._%+- only)")
    return s


def validate_bot_token(s: str) -> str:
    if not _TOKEN.fullmatch(s):
        raise ValueError("bot token must look like 123456:ABC... from BotFather")
    return s
