"""Edge-case coverage for the inbound Telegram transport (API_SPEC §2.5).

Companion to `test_telegram_poll.py`. That file proves the happy path and the
headline §2.5 obligations; this one hunts the corners the poison-message guard
actually has to survive in production:

- `send` itself raising (Telegram API down) on both the happy path and while
  emitting the error reply — the guard must still advance the offset,
- BOTH the handler and the error-reply send raising (double fault),
- offset MONOTONICITY across a batch that mixes valid + id-less updates in any
  order, and a non-None starting offset that a stale/low update must never drag
  backwards,
- `extract_message` against the full zoo of non-`message` update kinds
  (callback_query, empty {}, non-dict message, non-str text, media-only,
  half-built `chat`) — every one must yield None and authorise nobody,
- auth being the HANDLER's job: the transport passes the raw chat id straight
  through, so a foreign sender still reaches `handle` (which rejects it),
- `resolve_credentials` blank/whitespace token,
- the §2.5.4 no-concatenation rule under an HTML-injection attempt.

All deterministic, no sockets, no token — `fetch`/`send` are injected fakes.
"""

import pytest

from tennisbot import telegram_poll as tp
from tennisbot.prefs import Prefs, load_prefs, save_prefs
from tennisbot.telegram_commands import CommandSession

OWNER = "12345"
FOREIGN = "99999"
CENTRES = ["paddington", "westway"]


def make_update(update_id, text, chat_id=OWNER, key="message"):
    return {"update_id": update_id,
            key: {"message_id": update_id, "text": text,
                  "chat": {"id": chat_id, "type": "private"}}}


class Recorder:
    """Fake `send` that records outbound text."""

    def __init__(self):
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)


class ExplodingSend:
    """Fake `send` that records the call then raises — models a dead Telegram
    API. Used to prove the guard never lets a send failure escape."""

    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        raise RuntimeError("telegram sendMessage is down")


class BoomSession:
    """A handler that always raises (the poison message)."""

    def handle(self, *a, **k):
        raise RuntimeError("kaboom")


class SpySession:
    """Records exactly what the transport hands the handler, and replies with
    nothing — so we can assert on the arguments the transport passes through
    without any persistence side effects."""

    def __init__(self):
        self.seen = []

    def handle(self, text, chat_id, *, paid_this_week=None, next_scan=None):
        self.seen.append((text, chat_id))
        return None


# -- send() failures: the guard must still advance (§2.5.1/§2.5.2) ------------

def test_send_failure_on_happy_path_still_advances_offset(tmp_path):
    # Handler succeeds and returns a reply, but Telegram is down so send()
    # raises. The offset must still advance (the write already happened; only
    # the confirmation was lost) — never redeliver a message we acted on.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = ExplodingSend()
    acked = tp.process_update(make_update(200, "/cap 2"), session, send)
    assert acked == 201
    # send tried twice: the real reply, then the constant error reply — both
    # raised, both were swallowed.
    assert len(send.calls) == 2
    assert send.calls[1] == tp.ERROR_REPLY


def test_double_fault_handler_and_error_reply_both_raise():
    # The worst case: handler raises, and the error-reply send ALSO raises.
    # process_update must STILL return the acking offset and never propagate.
    send = ExplodingSend()
    acked = tp.process_update(make_update(300, "/cap 2"), BoomSession(), send)
    assert acked == 301
    assert send.calls == [tp.ERROR_REPLY]     # only the error reply was attempted


def test_batch_keeps_going_after_a_send_failure(tmp_path):
    # A send failure on the first update must not abort the rest of the batch.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = ExplodingSend()

    def fetch(offset):
        return [make_update(1, "/help"), make_update(2, "/help")]

    assert tp.poll_once(fetch, session, send, None) == 3


# -- offset monotonicity: never move backward (§2.5.1 corollary) -------------

def test_idless_update_before_a_valid_one_does_not_stall_offset(tmp_path):
    # Mirror of the existing "valid then id-less" test, order reversed: the
    # id-less update must be skipped for offset purposes, not clobber it.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)

    def fetch(offset):
        return [{"message": {"text": "x"}}, make_update(60, "/help")]

    assert tp.poll_once(fetch, session, Recorder(), None) == 61


def test_descending_ids_in_a_batch_never_lower_the_offset(tmp_path):
    # Telegram delivers ascending, but max() (not last-wins) must hold even if a
    # batch arrived out of order — otherwise we'd re-request already-acked ones.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)

    def fetch(offset):
        return [make_update(70, "/help"), make_update(65, "/help")]

    assert tp.poll_once(fetch, session, Recorder(), None) == 71


def test_low_update_cannot_drag_a_high_starting_offset_backward(tmp_path):
    # Starting offset already past this update (a redelivery / stale batch):
    # the offset must stay put, never regress.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)

    def fetch(offset):
        return [make_update(50, "/help")]

    assert tp.poll_once(fetch, session, Recorder(), 100) == 100


def test_mixed_valid_and_malformed_batch_moves_offset_forward_only(tmp_path):
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)

    def fetch(offset):
        return [
            make_update(80, "/help"),                               # valid
            {"update_id": 81, "edited_message": {"text": "/cap 0",
                                                 "chat": {"id": OWNER}}},
            {"garbage": True},                                      # no id at all
        ]

    assert tp.poll_once(fetch, session, Recorder(), None) == 82


def test_empty_batch_leaves_offset_unchanged():
    def fetch(offset):
        return []

    # Session is never touched (no updates), so a bare object is fine.
    assert tp.poll_once(fetch, object(), Recorder(), 500) == 500
    assert tp.poll_once(fetch, object(), Recorder(), None) is None


# -- extract_message: the full non-message zoo → None, never raise -----------

@pytest.mark.parametrize("update", [
    {},                                                       # utterly empty
    {"update_id": 1},                                         # id only
    {"update_id": 1, "callback_query": {"data": "/cap 9",
        "message": {"chat": {"id": OWNER}}}},                 # inline button tap
    {"update_id": 1, "message": {"text": "x", "chat": {}}},   # chat, but no id
    {"update_id": 1, "message": {"text": "x", "chat": "nope"}},   # chat not a dict
    {"update_id": 1, "message": {"text": 123, "chat": {"id": OWNER}}},  # text int
    {"update_id": 1, "message": {"text": ["/cap"],           # text a list
                                 "chat": {"id": OWNER}}},
    {"update_id": 1, "message": {"photo": [{"file_id": "z"}],  # media, no text
                                 "chat": {"id": OWNER}}},
    {"update_id": 1, "message": "nope"},                     # message not a dict
    {"update_id": 1, "message": None},                       # message null
    [1, 2, 3],                                               # not even a dict
])
def test_extract_message_returns_none_and_never_raises(update):
    assert tp.extract_message(update) is None


# -- auth is the HANDLER's job: transport passes the raw chat id through ------

def test_extract_passes_a_foreign_chat_id_through_unchanged():
    # The transport does NOT filter by sender — it hands the raw id on, and the
    # handler is what fails closed. Prove the id is passed verbatim.
    text, chat_id = tp.extract_message(make_update(1, "/cap 0", chat_id=FOREIGN))
    assert (text, chat_id) == ("/cap 0", FOREIGN)


def test_transport_hands_the_raw_chat_id_to_the_handler():
    # process_update must call handle() with the sender's real id (here a
    # stranger's) — it must not pre-authorise, filter, or rewrite it. The
    # handler is the sole authority; this guards against the transport
    # accidentally growing an auth check that diverges from the handler's.
    spy = SpySession()
    tp.process_update(make_update(5, "/cap 0", chat_id=FOREIGN), spy, Recorder())
    assert spy.seen == [("/cap 0", FOREIGN)]


# -- resolve_credentials: blank/whitespace token (§2.5.5) --------------------

def test_blank_token_detected_at_startup():
    env = {"TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_BOT_TOKEN": "   "}
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        tp.resolve_credentials(env)


def test_empty_env_reports_chat_id_first():
    # Chat id is the first thing checked, so an entirely empty env should point
    # at the chat id (the handler fails closed on it → silent no-op).
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        tp.resolve_credentials({})


# -- §2.5.4: reply passed through, never re-concatenated from user input ------

def test_html_injection_attempt_is_not_reflected_raw(tmp_path):
    # The handler escapes user input at its render boundary; the transport must
    # pass that reply through byte-for-byte and NEVER splice the raw input back
    # in. Feed a centre name full of HTML and confirm the raw markup never
    # reaches `send` (it would 400 the HTML-parse-mode API and lose the reply).
    save_prefs(Prefs(), tmp_path)
    evil = "/centres <b>evil</b>"
    expected = CommandSession(OWNER, tmp_path,
                              valid_centres=CENTRES).handle(evil, OWNER)

    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = Recorder()

    def fetch(offset):
        return [make_update(9, evil)]

    tp.poll_once(fetch, session, send, None)
    assert send.sent == [expected]                 # unmodified pass-through
    assert "<b>evil</b>" not in send.sent[0]        # raw markup never reflected
    assert load_prefs(tmp_path).centres == ("paddington",)   # rejected, unchanged


# -- integration: the /live confirm handshake survives across a batch --------

def test_live_confirm_handshake_threads_through_the_transport(tmp_path):
    # `/live on` then `CONFIRM` arrive as two updates in one batch. The transport
    # must thread CommandSession's handshake state between them so the second
    # message flips `live` — proving the seam preserves stateful, multi-message
    # flows (not just one-shot commands).
    save_prefs(Prefs(live=False), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = Recorder()

    def fetch(offset):
        return [make_update(1, "/live on"), make_update(2, "CONFIRM")]

    offset = tp.poll_once(fetch, session, send, None)
    assert offset == 3
    assert load_prefs(tmp_path).live is True
    assert len(send.sent) == 2                     # armed, then confirmed
