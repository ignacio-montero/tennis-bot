"""Inbound Telegram transport — long-poll `getUpdates` (API_SPEC §2 + §2.5).

The command *logic* is pure and lives in `telegram_commands.py`; this module is
the I/O shell around it. It owns the two impure edges the pure handler must not:
the `getUpdates` long-poll (inbound) and the outbound `Telegram.send`. It plugs
them into `CommandSession.handle()`, the seam the handler exposes for exactly
this.

The design keeps the HTTP paper-thin and the decisions testable by splitting the
loop into three layers, only the outermost of which touches a socket:

- `extract_message()` — **pure**: an update dict → `(text, chat_id)` or `None`.
  Only a plain `message` is actioned; `edited_message` / `channel_post` /
  `my_chat_member` / non-text messages yield `None` (and authorise nobody — the
  handler fails closed on a `None` chat id anyway).
- `process_update()` — handle ONE update and return the offset that
  acknowledges it (`update_id + 1`). This is the **poison-message guard**
  (API_SPEC §2.5.1): it wraps the handler+send in a broad `except`, so a raised
  handler (e.g. `save_prefs` on a read-only volume, §2.5.2) never escapes and
  the offset advances regardless — otherwise Telegram redelivers the bad update
  forever and, under `restart: unless-stopped`, that is a crash loop.
- `poll_once()` — fetch a batch and fold each update through `process_update`,
  returning the new offset. Takes `fetch` and `send` as **injected callables**,
  so the whole loop body is exercised in tests with no network and no token.

`run_prefs_transport()` is the only thing that opens a socket: it builds the real
`getUpdates` fetcher and the real `Telegram` sender and spins `poll_once` forever.
"""

from __future__ import annotations

import os
import time
from typing import Callable

import httpx
import structlog

from .notify.telegram import API, Telegram
from .telegram_commands import CommandSession

log = structlog.get_logger()


class TransportError(RuntimeError):
    """A transient getUpdates failure (network, 5xx, 429, 409). Token-redacted;
    the outer loop backs off and retries."""


class FatalTransportError(TransportError):
    """A non-recoverable getUpdates failure (401/404 = bad token). The loop must
    STOP — retrying forever would look healthy while doing nothing."""

# Long-poll hold time on the Telegram side (seconds). The HTTP read timeout must
# exceed this or httpx aborts the connection every cycle (see make_http_fetch).
DEFAULT_POLL_TIMEOUT = 30

# A constant, HTML-safe reply. NEVER build a reply from user input (API_SPEC
# §2.5.4): replies go out with parse_mode=HTML, and the handler already escapes
# at its render boundary, so anything we synthesise here must also be inert.
ERROR_REPLY = "⚠️ Something went wrong handling that — it's been logged."

Fetch = Callable[[object], list]      # (offset) -> list[update dict]
Send = Callable[[str], None]          # (text) -> None


# --------------------------------------------------------------------------
# Pure: update payload -> what to do
# --------------------------------------------------------------------------

def extract_message(update) -> tuple[str, object] | None:
    """`(text, chat_id)` from a plain text `message`, else `None`.

    Deliberately narrow: we act ONLY on `message`. `edited_message`,
    `channel_post`, `my_chat_member` and non-text messages (photos, stickers)
    return `None` — there is no command in them, and none of them should be
    able to authorise a sender. Robust to a malformed payload (missing keys,
    wrong types) so a junk update can't crash the loop.
    """
    if not isinstance(update, dict):
        return None
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None                       # not a plain message: nothing to do
    text = msg.get("text")
    if not isinstance(text, str):
        return None                       # non-text message: no command
    chat = msg.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if chat_id is None:
        return None                       # nowhere to reply, nobody to authorise
    return text, chat_id


# --------------------------------------------------------------------------
# One update — the poison-message guard (API_SPEC §2.5.1)
# --------------------------------------------------------------------------

def process_update(update, session: CommandSession, send: Send, *,
                   paid_this_week: int | None = None,
                   next_scan: str | None = None) -> int | None:
    """Handle one update and return `update_id + 1` (the acking offset), or
    `None` if the update carries no usable id.

    NEVER raises. A handler exception — the classic being `save_prefs` failing
    on a read-only/full volume (§2.5.2) — is caught, logged, and answered with a
    constant reply; the offset is still returned so the loop advances past the
    poison message instead of looping on it forever.
    """
    update_id = update.get("update_id") if isinstance(update, dict) else None
    extracted = extract_message(update)

    if extracted is not None:
        text, chat_id = extracted
        try:
            # The handler is the pure command surface; it authorises, validates,
            # persists, and hands back a ready-to-send reply (already HTML-safe).
            reply = session.handle(text, chat_id,
                                   paid_this_week=paid_this_week,
                                   next_scan=next_scan)
            if reply:
                send(reply)               # pass the handler's reply UNMODIFIED
        except Exception as e:
            # §2.5.1/§2.5.2: log, optionally reply, and fall through to advance
            # the offset. A stranger never receives this — `send` targets the
            # owner's chat, and the handler only reaches a raise after auth.
            log.warning("prefs_transport.handle_failed",
                        error=str(e), error_type=type(e).__name__)
            try:
                send(ERROR_REPLY)
            except Exception as e2:
                log.warning("prefs_transport.error_reply_failed", error=str(e2))

    if isinstance(update_id, int):
        return update_id + 1
    # No id to ack (only happens for a malformed payload, never a real update).
    log.warning("prefs_transport.update_without_id", update=repr(update)[:200])
    return None


def poll_once(fetch: Fetch, session: CommandSession, send: Send,
              offset: int | None, *, paid_this_week: int | None = None,
              next_scan: str | None = None) -> int | None:
    """Fetch one batch at `offset` and fold each update through the guard.

    Returns the new offset (highest `update_id + 1` seen). `max()` rather than
    "last one wins" so a single update in the batch that lacks an id can't drag
    the offset backwards and cause redelivery of ones we already handled.
    """
    updates = fetch(offset)
    for update in updates:
        acked = process_update(update, session, send,
                               paid_this_week=paid_this_week,
                               next_scan=next_scan)
        if acked is not None:
            offset = acked if offset is None else max(offset, acked)
    return offset


# --------------------------------------------------------------------------
# Startup checks (API_SPEC §2.5.5)
# --------------------------------------------------------------------------

def resolve_credentials(env=None) -> tuple[str, str]:
    """Return `(bot_token, owner_chat_id)` or raise loudly.

    §2.5.5: the handler fails CLOSED when `TELEGRAM_CHAT_ID` is missing — it
    authorises nobody, so every command silently no-ops. A deployment that ships
    without it is inert, not open, but the operator would never know. So we
    refuse to start and log at error level rather than run a transport that can
    do nothing. A missing token is likewise fatal — we can't even call
    `getUpdates`.
    """
    env = os.environ if env is None else env
    owner = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not owner:
        log.error("prefs_transport.no_chat_id",
                  detail="TELEGRAM_CHAT_ID missing/blank — handler fails closed, "
                         "so every command would be a silent no-op")
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not set: the command handler authorises nobody "
            "without it, so the transport would be inert. Refusing to start.")
    if not token:
        log.error("prefs_transport.no_token",
                  detail="TELEGRAM_BOT_TOKEN missing/blank — cannot poll getUpdates")
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set: cannot poll getUpdates.")
    return token, owner


# --------------------------------------------------------------------------
# The real HTTP fetcher (the ONLY socket in this module)
# --------------------------------------------------------------------------

def make_http_fetch(token: str, poll_timeout: int = DEFAULT_POLL_TIMEOUT,
                    client: httpx.Client | None = None) -> Fetch:
    """Build a `fetch(offset) -> [update, ...]` bound to `getUpdates`.

    Long-poll: the `timeout` query param tells Telegram to hold the connection
    open up to `poll_timeout` seconds waiting for an update (one round-trip
    instead of a busy-poll), so the HTTP read timeout is set generously above it.
    `offset` acknowledges everything below it and asks only for newer updates.
    """
    base = f"{API}/bot{token}"
    owns_client = client is None
    client = client or httpx.Client(timeout=poll_timeout + 15)

    def _redact(text: str) -> str:
        return text.replace(token, "***") if token else text

    def fetch(offset):
        params: dict[str, object] = {"timeout": poll_timeout}
        if offset:
            params["offset"] = offset
        try:
            r = client.get(f"{base}/getUpdates", params=params)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            # 401 (revoked/typo'd token) and 404 (malformed token) are NOT
            # transient — retrying loops forever looking healthy while inert,
            # AND re-leaks nothing now but wastes cycles. Fail loud (§2.5.5's
            # spirit: a misconfigured deploy must not be silently useless).
            # 409 = another getUpdates consumer on this token; distinct log so
            # the thrash is diagnosable. `_redact` guards the URL-embedded token
            # in every raised message (CWE-532).
            if code in (401, 404):
                raise FatalTransportError(
                    f"Telegram rejected the token (HTTP {code}) — check "
                    f"TELEGRAM_BOT_TOKEN. Refusing to spin uselessly.") from None
            if code == 409:
                raise TransportError(
                    "HTTP 409 — another getUpdates consumer is using this "
                    "token (only one is allowed). Backing off.") from None
            raise TransportError(f"getUpdates HTTP {code}") from None
        except httpx.HTTPError as e:                 # connect/read/timeout etc.
            raise TransportError(_redact(str(e))) from None
        return r.json().get("result", [])

    fetch._client = client            # type: ignore[attr-defined]
    fetch._owns_client = owns_client  # type: ignore[attr-defined]
    return fetch


# --------------------------------------------------------------------------
# The loop (the `prefs` subcommand runs this)
# --------------------------------------------------------------------------

def run_prefs_transport(*, config_dir=None, notify: bool = True,
                        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
                        max_iterations: int | None = None,
                        paid_this_week: int | None = None,
                        next_scan: str | None = None,
                        error_backoff_s: float = 3.0) -> None:
    """Long-poll forever, feeding each message to `CommandSession` (API_SPEC §2).

    `max_iterations` bounds the loop for tests/smoke checks; `None` = forever.
    `paid_this_week` / `next_scan` are the two `/status` facts the config doc
    can't hold (API_SPEC §2.1): standalone there is no catcher to supply them, so
    they stay `None` (rendered as "unknown"). The catcher loop, when it hosts
    this transport, passes them in.
    """
    token, owner = resolve_credentials()          # §2.5.5, before any socket
    session = CommandSession(owner, config_dir)   # valid_centres read fresh
    notifier = Telegram(token, owner) if notify else None

    def send(text: str) -> None:
        if notifier is not None:
            notifier.send(text)

    fetch = make_http_fetch(token, poll_timeout)
    offset: int | None = None
    log.info("prefs_transport.up", owner=owner, poll_timeout=poll_timeout,
             notify=notify)

    iterations = 0
    try:
        while True:
            try:
                offset = poll_once(fetch, session, send, offset,
                                   paid_this_week=paid_this_week,
                                   next_scan=next_scan)
            except FatalTransportError as e:
                # A bad token can never become good by retrying — stop loudly so
                # `restart: unless-stopped` surfaces it, not a silent inert loop.
                log.error("prefs_transport.fatal", error=str(e))
                raise
            except Exception as e:
                # A network hiccup on getUpdates must not kill the daemon — back
                # off briefly and retry. (Per-update handler errors are already
                # contained inside process_update.) `str(e)` is safe: fetch
                # redacts the token before raising.
                log.warning("prefs_transport.poll_error",
                            error=str(e), error_type=type(e).__name__)
                time.sleep(error_backoff_s)

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
    finally:
        client = getattr(fetch, "_client", None)
        if client is not None and getattr(fetch, "_owns_client", False):
            client.close()
    log.info("prefs_transport.done", iterations=iterations)
