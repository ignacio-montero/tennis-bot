"""Deterministic tests for the inbound Telegram transport (API_SPEC §2.5).

No sockets, no token, no live Telegram. The loop body takes `fetch` and `send`
as injected callables, so every §2.5 obligation is exercised with fakes:

- offset advances after a normal update,
- offset advances even when the handler RAISES (the poison-message guard),
- a save failure doesn't kill the loop,
- a missing TELEGRAM_CHAT_ID is detected at startup,
- malformed/empty payloads don't crash and authorise nobody.
"""

import datetime as dt

import pytest

from tennisbot import telegram_poll as tp
from tennisbot.prefs import LONDON, Prefs, load_prefs, save_prefs
from tennisbot.telegram_commands import CommandSession

OWNER = "12345"
CENTRES = ["paddington", "westway"]


def make_update(update_id, text, chat_id=OWNER, key="message"):
    """A minimal Telegram update. `key` lets us forge edited_message etc."""
    return {"update_id": update_id,
            key: {"message_id": update_id, "text": text,
                  "chat": {"id": chat_id, "type": "private"}}}


class Recorder:
    """A fake `send` that records every outbound text."""

    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, text):
        self.sent.append(text)


# -- extract_message (pure) --------------------------------------------------

def test_extract_plain_message():
    text, chat_id = tp.extract_message(make_update(1, "/status"))
    assert text == "/status"
    assert chat_id == OWNER


@pytest.mark.parametrize("update", [
    {"update_id": 1},                                    # no message at all
    {"update_id": 1, "edited_message": {"text": "/cap 9",
                                        "chat": {"id": OWNER}}},
    {"update_id": 1, "channel_post": {"text": "/cap 9",
                                      "chat": {"id": OWNER}}},
    {"update_id": 1, "my_chat_member": {"chat": {"id": OWNER}}},
    {"update_id": 1, "message": {"chat": {"id": OWNER}}},  # message, no text
    {"update_id": 1, "message": {"text": "/cap 9"}},       # no chat
    "not-a-dict",
    None,
])
def test_extract_ignores_non_plain_messages(update):
    # None of these carry an actionable command; none must reach the handler.
    assert tp.extract_message(update) is None


# -- offset advances on a normal update --------------------------------------

def test_offset_advances_after_normal_update(tmp_path):
    save_prefs(Prefs(weekly_cap=3), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = Recorder()

    def fetch(offset):
        assert offset is None            # first cycle asks for everything
        return [make_update(100, "/cap 2")]

    new_offset = tp.poll_once(fetch, session, send, None)
    assert new_offset == 101             # update_id + 1
    assert load_prefs(tmp_path).weekly_cap == 2
    assert send.sent and "cap 2" in send.sent[0]


def test_offset_passed_back_on_next_fetch(tmp_path):
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    seen_offsets = []

    def fetch(offset):
        seen_offsets.append(offset)
        if offset is None:
            return [make_update(5, "/help")]
        return []                        # nothing new the second time

    offset = tp.poll_once(fetch, session, Recorder(), None)
    offset = tp.poll_once(fetch, session, Recorder(), offset)
    assert seen_offsets == [None, 6]     # acked 5, so we ask for >= 6


# -- THE poison-message guard: offset advances even when the handler RAISES ---

def test_offset_advances_when_handler_raises():
    # Highest-value test: an exception escaping the handler must NOT stop the
    # loop or leave the update un-acked (Telegram would redeliver it forever —
    # a crash loop under restart: unless-stopped). §2.5.1.
    class BoomSession:
        def handle(self, *a, **k):
            raise RuntimeError("kaboom")

    send = Recorder()
    offset = tp.process_update(make_update(42, "/cap 2"), BoomSession(), send)
    assert offset == 43                  # advanced despite the raise
    assert send.sent == [tp.ERROR_REPLY]  # owner told, with a constant reply


def test_loop_survives_a_raising_handler_and_keeps_going():
    class BoomSession:
        def handle(self, *a, **k):
            raise RuntimeError("kaboom")

    def fetch(offset):
        return [make_update(1, "/cap 2"), make_update(2, "/help")]

    # Neither update kills poll_once; the offset clears both.
    offset = tp.poll_once(fetch, BoomSession(), Recorder(), None)
    assert offset == 3


# -- a save failure doesn't kill the loop (§2.5.2) ---------------------------

def test_save_failure_does_not_kill_the_loop(tmp_path, monkeypatch):
    # save_prefs raises on a read-only / full volume (a plausible compose
    # mis-mount — the sprinter mounts this volume ro). The loop must survive it.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)

    def boom_save(*a, **k):
        raise OSError("Read-only file system")

    monkeypatch.setattr("tennisbot.telegram_commands.save_prefs", boom_save)
    send = Recorder()

    def fetch(offset):
        return [make_update(7, "/cap 2")]

    offset = tp.poll_once(fetch, session, send, None)
    assert offset == 8                   # acked despite the write failure
    assert send.sent == [tp.ERROR_REPLY]


# -- malformed / foreign updates: no crash, authorise nobody -----------------

def test_malformed_updates_advance_offset_without_acting(tmp_path):
    save_prefs(Prefs(weekly_cap=3), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = Recorder()

    def fetch(offset):
        return [
            {"update_id": 10},                                    # empty
            {"update_id": 11, "edited_message": {"text": "/cap 0",
                                                 "chat": {"id": OWNER}}},
            {"update_id": 12, "channel_post": {"text": "/cap 0",
                                               "chat": {"id": OWNER}}},
        ]

    offset = tp.poll_once(fetch, session, send, None)
    assert offset == 13                  # every update acked
    assert send.sent == []               # nothing actioned
    assert load_prefs(tmp_path).weekly_cap == 3    # config untouched


def test_foreign_sender_is_silent_and_changes_nothing(tmp_path):
    save_prefs(Prefs(weekly_cap=3), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = Recorder()

    def fetch(offset):
        return [make_update(20, "/cap 0", chat_id="99999")]

    offset = tp.poll_once(fetch, session, send, None)
    assert offset == 21                  # still acked
    assert send.sent == []               # silence, not a refusal (§2)
    assert load_prefs(tmp_path).weekly_cap == 3


# -- reply is passed through unmodified (§2.5.4) -----------------------------

def test_reply_is_passed_through_unmodified(tmp_path):
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    send = Recorder()
    expected = session.handle("/help", OWNER)   # what the handler produces

    def fetch(offset):
        return [make_update(30, "/help")]

    tp.poll_once(fetch, session, send, None)
    assert send.sent == [expected]              # byte-for-byte, no re-wrapping


# -- update without an id can't drag the offset backwards --------------------

def test_update_without_id_does_not_lower_offset(tmp_path):
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)

    def fetch(offset):
        return [make_update(50, "/help"), {"message": {"text": "x"}}]  # 2nd: no id

    offset = tp.poll_once(fetch, session, Recorder(), None)
    assert offset == 51


# -- startup credential checks (§2.5.5) --------------------------------------

def test_missing_chat_id_detected_at_startup():
    env = {"TELEGRAM_BOT_TOKEN": "tok"}     # no TELEGRAM_CHAT_ID
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        tp.resolve_credentials(env)


def test_blank_chat_id_detected_at_startup():
    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "   "}
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        tp.resolve_credentials(env)


def test_missing_token_detected_at_startup():
    env = {"TELEGRAM_CHAT_ID": OWNER}       # no TELEGRAM_BOT_TOKEN
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        tp.resolve_credentials(env)


def test_valid_credentials_returned():
    env = {"TELEGRAM_BOT_TOKEN": " tok ", "TELEGRAM_CHAT_ID": " 123 "}
    token, owner = tp.resolve_credentials(env)
    assert (token, owner) == ("tok", "123")


# -- the full loop with injected fetch/send, no network ----------------------

def test_run_transport_bounded_no_network(tmp_path, monkeypatch):
    # Exercise run_prefs_transport end-to-end but with fetch/send stubbed so no
    # socket is opened. max_iterations bounds it to two cycles.
    save_prefs(Prefs(weekly_cap=3), tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    monkeypatch.setenv("TENNISBOT_CONFIG_DIR", str(tmp_path))

    sent = []
    calls = {"n": 0}

    def fake_fetch(token, poll_timeout, client=None):
        def fetch(offset):
            calls["n"] += 1
            if calls["n"] == 1:
                return [make_update(1, "/cap 1")]
            return []
        fetch._client = None
        fetch._owns_client = False
        return fetch

    class FakeTelegram:
        def __init__(self, *a, **k):
            pass

        def send(self, text):
            sent.append(text)

    monkeypatch.setattr(tp, "make_http_fetch", fake_fetch)
    monkeypatch.setattr(tp, "Telegram", FakeTelegram)

    tp.run_prefs_transport(max_iterations=2)

    assert load_prefs(tmp_path).weekly_cap == 1
    assert sent and "cap 1" in sent[0]


def test_run_transport_survives_a_fetch_error(tmp_path, monkeypatch):
    # A getUpdates network hiccup must not kill the daemon: it backs off (we
    # stub sleep to 0) and keeps looping.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    monkeypatch.setenv("TENNISBOT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(tp.time, "sleep", lambda s: None)

    def fake_fetch(token, poll_timeout, client=None):
        def fetch(offset):
            raise ConnectionError("telegram unreachable")
        fetch._client = None
        fetch._owns_client = False
        return fetch

    monkeypatch.setattr(tp, "make_http_fetch", fake_fetch)
    monkeypatch.setattr(tp, "Telegram", lambda *a, **k: None)

    # Two cycles, both raising, must return cleanly (not propagate).
    tp.run_prefs_transport(notify=False, max_iterations=2, error_backoff_s=0)
