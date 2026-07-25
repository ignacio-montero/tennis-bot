"""Security + HTTP-layer tests for the Telegram transport.

Covers the two gaps the critic flagged: (#1) the bot token must NEVER reach a
log/exception string (CWE-532 — it leaks via the token-in-URL that httpx errors
stringify), and (#4) make_http_fetch — the only socket-touching function — was
untested. Uses httpx.MockTransport, so no real network.
"""
import httpx
import pytest

from tennisbot import telegram_poll as T
from tennisbot.notify.telegram import Telegram, TelegramError

TOKEN = "123456:SECRET-TOKEN-ABC"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── #1: the token never leaks, at either source ────────────────────────────
def test_sender_error_is_token_redacted(monkeypatch):
    # notify/telegram.py is the PRODUCTION leak source: str(httpx error) embeds
    # the bot-URL (with token). The sender must raise a redacted message so no
    # caller's log(error=str(e)) can leak it — incl. watchd.py:232 on the box.
    import tennisbot.notify.telegram as mod
    real_client = httpx.Client                       # capture BEFORE patching
    def boom(request):
        return httpx.Response(429, request=request)
    monkeypatch.setattr(mod.httpx, "Client",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as ei:
        Telegram(TOKEN, "999").send("hi")
    assert "SECRET-TOKEN-ABC" not in str(ei.value)
    assert "***" in str(ei.value)


def test_fetch_transient_error_is_token_redacted():
    # make_http_fetch (the socket function, previously untested) must redact too.
    def boom(request):
        raise httpx.ConnectError("connection failed", request=request)
    fetch = T.make_http_fetch(TOKEN, poll_timeout=1, client=_client(boom))
    with pytest.raises(T.TransportError) as ei:
        fetch(0)
    assert "SECRET-TOKEN-ABC" not in str(ei.value)


# ── #4 + #2/#3: make_http_fetch behaviour and error classification ─────────
def test_fetch_extracts_result_and_sends_offset():
    seen = {}
    def ok(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})
    fetch = T.make_http_fetch(TOKEN, poll_timeout=30, client=_client(ok))
    assert fetch(42) == [{"update_id": 5}]
    assert "offset=42" in seen["url"] and "timeout=30" in seen["url"]
    # no offset on first call (offset=None/0) — must omit the param, not send 0
    fetch(0)
    assert "offset=" not in seen["url"]


@pytest.mark.parametrize("code", [401, 404])
def test_bad_token_is_FATAL_not_a_retry_loop(code):
    # A wrong token can't become right by retrying: fail loud, don't spin inert.
    def bad(request):
        return httpx.Response(code, request=request)
    fetch = T.make_http_fetch(TOKEN, poll_timeout=1, client=_client(bad))
    with pytest.raises(T.FatalTransportError):
        fetch(0)


@pytest.mark.parametrize("code", [409, 429, 500, 503])
def test_transient_http_errors_are_TransportError_not_fatal(code):
    def transient(request):
        return httpx.Response(code, request=request)
    fetch = T.make_http_fetch(TOKEN, poll_timeout=1, client=_client(transient))
    with pytest.raises(T.TransportError) as ei:
        fetch(0)
    assert not isinstance(ei.value, T.FatalTransportError)   # loop keeps going
    assert "SECRET-TOKEN-ABC" not in str(ei.value)


def test_extract_message_handles_int_chat_id():
    # Real Telegram sends chat.id as an INT; the existing fixtures forge it as a
    # str. Both reviewers flagged the gap — the transport must pass the int
    # straight through (the handler stringifies both sides).
    assert T.extract_message(
        {"message": {"text": "/status", "chat": {"id": 12345}}}) == ("/status", 12345)
