"""Deterministic tests for the inbound Telegram command surface (API_SPEC §2).

Pure logic only — no bot token, no sockets, no `getUpdates`. `now` is always
injected so the two-minute confirm handshake is tested without sleeping.
"""

import datetime as dt

from tennisbot.prefs import LONDON, Prefs, load_prefs, save_prefs
from tennisbot.telegram_commands import (CONFIRM_TIMEOUT, CommandSession,
                                         handle_message)

OWNER = "12345"
CENTRES = ["paddington", "westway"]
NOW = dt.datetime(2026, 7, 24, 10, 0, tzinfo=LONDON)


def send(text, prefs=None, chat_id=OWNER, now=NOW, pending=None, target="both",
         **kw):
    # `target` defaults to "both" because every handshake this suite arms is
    # `/live on` (= both). The pure handler now FAILS CLOSED on an untargeted
    # CONFIRM (W3), so the target must be threaded exactly as the session does.
    return handle_message(text, chat_id, prefs or Prefs.defaults(),
                          owner_chat_id=OWNER, now=now,
                          pending_confirm_until=pending,
                          pending_confirm_target=(target if pending else None),
                          valid_centres=CENTRES, **kw)


# -- authorisation -----------------------------------------------------------

def test_foreign_chat_id_changes_nothing_and_gets_no_reply():
    # PRD §7 + API_SPEC §2: a stranger must change nothing AND get silence —
    # replying "unauthorised" would confirm the bot exists to whoever probed it.
    r = send("/cap 99", chat_id="99999")
    assert r.prefs is None
    assert r.reply is None


def test_foreign_chat_id_cannot_reach_the_store(tmp_path):
    # Same rule one layer up, through the persisting seam: the file on disk
    # must be untouched.
    save_prefs(Prefs(weekly_cap=3), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    assert session.handle("/cap 99", chat_id="66666") is None
    assert load_prefs(tmp_path).weekly_cap == 3


# -- read commands -----------------------------------------------------------

def test_status_leads_with_the_mode():
    # §8.8: a Telegram-set `live` flag is invisible persisted state unless the
    # read-back shouts it, so mode comes FIRST, before any preference.
    r = send("/status", Prefs(catcher_live=True, drop_live=True, days=("Tue",), earliest="18:00"))
    assert r.prefs is None                        # a read never writes
    assert r.reply.splitlines()[0].endswith("<b>LIVE</b>")
    assert "18:00" in r.reply and "Tue" in r.reply


def test_status_returns_the_active_settings(tmp_path):
    # PRD §7: "the read-back command returns the currently-active settings" —
    # i.e. what's on disk, not a cached copy in memory.
    save_prefs(Prefs(centres=("westway",), weekly_cap=1, slot_length_hours=2),
               tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    reply = session.handle("/status", OWNER, now=NOW)
    assert "westway" in reply and "2h" in reply and "1" in reply


def test_status_shows_injected_runtime_facts():
    # paid-count and next-scan belong to the catcher, not the config; they are
    # injected so this module stays pure (and buildable before the catcher is).
    r = send("/status", paid_this_week=2, next_scan="10:30")
    assert "2/3 paid this week" in r.reply and "10:30" in r.reply


def test_help_lists_the_commands():
    r = send("/help")
    for cmd in ("/status", "/centres", "/days", "/window", "/length", "/cap",
                "/live"):
        assert cmd in r.reply


def test_bot_suffix_is_stripped():
    # Telegram delivers "/status@tennisbot" when the bot is in a group.
    assert send("/status@tennis_bot").reply.startswith("🟢")


# -- config commands (§2.2) --------------------------------------------------

def test_centres_sets_and_echoes_full_config():
    r = send("/centres paddington westway")
    assert r.prefs.centres == ("paddington", "westway")
    # Every change replies with the new value AND the whole active config, so
    # the phone always shows the full picture.
    assert "westway" in r.reply and r.prefs.summary() in r.reply


def test_days_accepts_names_and_any():
    assert send("/days Tue Thu").prefs.days == ("Tue", "Thu")
    assert send("/days thu,tue").prefs.days == ("Tue", "Thu")   # canonicalised
    assert send("/days any").prefs.days == ()


def test_window_forms():
    assert (send("/window 18:00-22:00").prefs.earliest,
            send("/window 18:00-22:00").prefs.latest) == ("18:00", "22:00")
    open_ended = send("/window 18:00-").prefs
    assert open_ended.earliest == "18:00" and open_ended.latest is None
    cleared = send("/window any", Prefs(earliest="18:00", latest="20:00")).prefs
    assert cleared.earliest is None and cleared.latest is None


def test_length_and_cap():
    assert send("/length 2").prefs.slot_length_hours == 2
    assert send("/cap 0").prefs.weekly_cap == 0
    assert "paused" in send("/cap 0").reply      # 0 is a real state, flag it


# -- rejection (§1.3 / PRD §7) ----------------------------------------------

def test_invalid_time_is_rejected_and_config_left_intact():
    # PRD §7: "an invalid setting is rejected with a helpful Telegram reply and
    # leaves the previous config intact". `prefs is None` IS "intact" here —
    # the caller has nothing to save.
    before = Prefs(earliest="18:00", latest="22:00")
    r = send("/window 25:00-26:00", before)
    assert r.prefs is None and r.ok is False
    assert "18:00" in r.reply                    # tells you what still applies
    assert "HH:MM" in r.reply                    # ...and what a good value is


def test_backwards_window_is_rejected():
    r = send("/window 22:00-18:00")
    assert r.prefs is None and "before" in r.reply


def test_unknown_centre_is_rejected_with_the_valid_keys():
    r = send("/centres atlantis")
    assert r.prefs is None
    assert "paddington" in r.reply and "westway" in r.reply


def test_bad_length_and_cap_rejected():
    assert send("/length 3").prefs is None
    assert send("/cap -1").prefs is None
    assert send("/cap three").prefs is None


def test_rejected_update_never_reaches_disk(tmp_path):
    save_prefs(Prefs(slot_length_hours=1), tmp_path)
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    reply = session.handle("/length 5", OWNER, now=NOW)
    assert "1 or 2" in reply
    assert load_prefs(tmp_path).slot_length_hours == 1


def test_unknown_command_and_chatter_get_a_pointer_not_a_change():
    assert send("/dance").prefs is None and "/help" in send("/dance").reply
    assert send("hello bot").prefs is None


# -- live handshake (§2.3) ---------------------------------------------------

def test_live_off_is_immediate():
    r = send("/live off", Prefs(catcher_live=True, drop_live=True))
    assert r.prefs.live is False and "DRY-RUN" in r.reply


def test_live_on_arms_but_does_not_enable():
    # The whole point of the speed bump: the consequential transition takes two
    # deliberate messages, everything else stays instant.
    r = send("/live on")
    assert r.prefs is None                        # nothing persisted yet
    assert "CONFIRM" in r.reply
    assert r.pending_confirm_until == NOW + CONFIRM_TIMEOUT


def test_confirm_within_the_window_enables_live():
    armed = send("/live on")
    r = send("CONFIRM", now=NOW + dt.timedelta(seconds=30),
             pending=armed.pending_confirm_until)
    assert r.prefs.live is True and "LIVE" in r.reply
    assert r.pending_confirm_until is None        # one-shot: cannot be reused


def test_any_other_message_cancels_the_arming():
    armed = send("/live on")
    r = send("/status", pending=armed.pending_confirm_until)
    assert r.prefs is None                        # still dry-run
    assert "cancelled" in r.reply
    assert r.pending_confirm_until is None
    # ...and a CONFIRM after the cancellation must NOT sneak live on.
    late = send("CONFIRM", pending=r.pending_confirm_until)
    assert late.prefs is None


def test_confirm_after_the_timeout_does_not_enable_live():
    armed = send("/live on")
    r = send("CONFIRM", now=NOW + CONFIRM_TIMEOUT + dt.timedelta(seconds=1),
             pending=armed.pending_confirm_until)
    assert r.prefs is None and "expired" in r.reply


def test_bare_confirm_with_nothing_armed_does_nothing():
    r = send("CONFIRM")
    assert r.prefs is None and "Nothing to confirm" in r.reply


def test_confirm_is_case_insensitive():
    # Deliberate leniency: phone keyboards autocapitalise unpredictably, and
    # "confirm" typed by accident in the 2-min window is not a realistic risk.
    armed = send("/live on")
    assert send("confirm", pending=armed.pending_confirm_until).prefs.live


def test_re_sending_live_on_re_arms_the_handshake():
    armed = send("/live on")
    again = send("/live on", pending=armed.pending_confirm_until, now=NOW)
    assert again.prefs is None                      # still not live
    assert again.pending_confirm_until == NOW + CONFIRM_TIMEOUT


def test_live_on_when_already_live_is_a_no_op():
    r = send("/live on", Prefs(catcher_live=True, drop_live=True))
    assert r.prefs is None and r.pending_confirm_until is None


def test_session_persists_live_only_after_confirm(tmp_path):
    # End-to-end through the persisting seam: the handshake state survives
    # between messages inside the session, and only the confirmed flip lands
    # on disk.
    session = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    session.handle("/live on", OWNER, now=NOW)
    assert load_prefs(tmp_path).live is False
    session.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=10))
    assert load_prefs(tmp_path).live is True


# -- persistence across a restart -------------------------------------------

def test_changes_persist_across_a_restarted_session(tmp_path):
    # PRD §7: settings survive a container restart. A second CommandSession
    # object over the same directory is the restart.
    s1 = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    s1.handle("/days Tue Thu", OWNER, now=NOW)
    s1.handle("/window 18:00-22:00", OWNER, now=NOW)
    s2 = CommandSession(OWNER, tmp_path, valid_centres=CENTRES)
    reply = s2.handle("/status", OWNER, now=NOW)
    assert "Tue, Thu" in reply and "18:00–22:00" in reply
    assert load_prefs(tmp_path).days == ("Tue", "Thu")
