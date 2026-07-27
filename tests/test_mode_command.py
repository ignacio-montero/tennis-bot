"""The `/mode` Telegram command (API_SPEC §2.2) — pure logic, no sockets.

Covers setting rules/calendar, the read-only show, rejection of a bad arg, the
no-CONFIRM-handshake contract, the missing-URL warning, and the /status +
/help render of the active source.
"""

import datetime as dt

from tennisbot.prefs import LONDON, Prefs, load_prefs, save_prefs
from tennisbot.telegram_commands import (CommandSession, handle_message)

OWNER = "12345"
CENTRES = ["paddington", "westway"]
NOW = dt.datetime(2026, 7, 24, 10, 0, tzinfo=LONDON)


def send(text, prefs=None, chat_id=OWNER, **kw):
    return handle_message(text, chat_id, prefs or Prefs.defaults(),
                          owner_chat_id=OWNER, now=NOW,
                          valid_centres=CENTRES, **kw)


# -- setting the source -----------------------------------------------------

def test_mode_calendar_sets_the_source_with_no_handshake():
    r = send("/mode calendar")
    assert r.prefs is not None and r.prefs.mode == "calendar"
    assert r.pending_confirm_until is None          # NOT a live handshake
    assert r.ok is True


def test_mode_rules_restores_the_default_source():
    r = send("/mode rules", Prefs(mode="calendar"))
    assert r.prefs.mode == "rules"


def test_mode_no_arg_shows_current_without_writing():
    r = send("/mode", Prefs(mode="calendar"))
    assert r.prefs is None                          # a show never writes
    assert "calendar" in r.reply


def test_mode_rejects_unknown_arg_and_changes_nothing():
    r = send("/mode sometimes")
    assert r.prefs is None and r.ok is False
    assert "calendar" in r.reply and "rules" in r.reply   # helpful usage


def test_mode_switch_does_not_arm_live():
    # Switching source must not create holds by itself (PRD §4.7 / §9.5).
    r = send("/mode calendar")
    assert r.prefs.catcher_live is False and r.prefs.drop_live is False


# -- the missing-URL warning (nice-to-have, §2.2) ---------------------------

def test_mode_calendar_warns_when_url_unset():
    r = send("/mode calendar", calendar_configured=False)
    assert r.prefs.mode == "calendar"               # still switches
    assert "TENNISBOT_CALENDAR_ICS_URL" in r.reply


def test_mode_calendar_no_warning_when_url_configured():
    r = send("/mode calendar", calendar_configured=True)
    assert "TENNISBOT_CALENDAR_ICS_URL" not in r.reply


# -- render: /status and /help ---------------------------------------------

def test_status_shows_the_active_source():
    assert "calendar" in send("/status", Prefs(mode="calendar")).reply
    assert "rules" in send("/status", Prefs(mode="rules")).reply


def test_help_lists_the_mode_command():
    assert "/mode" in send("/help").reply


# -- through the persisting session ----------------------------------------

def test_mode_persists_through_the_session(tmp_path):
    save_prefs(Prefs(), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    session.handle("/mode calendar", chat_id=OWNER, now=NOW)
    assert load_prefs(tmp_path).mode == "calendar"


def test_bad_mode_leaves_stored_config_intact(tmp_path):
    save_prefs(Prefs(mode="rules"), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    reply = session.handle("/mode nope", chat_id=OWNER, now=NOW)
    assert "calendar" in reply                       # usage echoed
    assert load_prefs(tmp_path).mode == "rules"      # unchanged
