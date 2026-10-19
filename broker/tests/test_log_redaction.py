"""The approvals bot token must never reach the journal.

httpx logs every request URL at INFO, and Telegram puts the token in the URL
(`/bot<id>:<secret>/getUpdates`); a failed poll's exception text carries the same URL.
"""
import io
import logging

import pytest

from hermes_broker.listen import configure_logging

TOKEN = "123456789:fake-test-token_ABCdef0123456789"
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=0&timeout=30"


@pytest.fixture
def stream():
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    buf = io.StringIO()
    configure_logging(buf)
    yield buf
    for h in list(root.handlers):
        if h not in before_handlers:
            root.removeHandler(h)
    root.setLevel(before_level)
    for n, lvl in levels.items():
        logging.getLogger(n).setLevel(lvl)


def test_httpx_request_lines_are_not_logged(stream):
    logging.getLogger("httpx").info("HTTP Request: GET %s \"HTTP/1.1 200 OK\"", URL)
    logging.getLogger("httpcore.http11").info("send_request_headers.started %s", URL)
    assert stream.getvalue() == ""


def test_token_redacted_from_any_message(stream):
    logging.getLogger("hermes_broker.approvals").warning("sendMessage failed for %s", URL)
    out = stream.getvalue()
    assert "sendMessage failed for" in out
    assert TOKEN not in out and TOKEN.split(":")[1] not in out
    assert "bot<redacted>/getUpdates" in out


def test_token_redacted_from_tracebacks(stream):
    try:
        raise RuntimeError(f"Client error '404 Not Found' for url '{URL}'")
    except RuntimeError:
        logging.getLogger("hermes_broker.approvals").exception("getUpdates failed; retrying")
    out = stream.getvalue()
    assert "getUpdates failed; retrying" in out and "RuntimeError" in out
    assert TOKEN.split(":")[1] not in out


def test_own_info_lines_still_logged(stream):
    logging.getLogger("hermes_broker.approvals").info("deploy of abc1234 approved")
    assert "deploy of abc1234 approved" in stream.getvalue()
