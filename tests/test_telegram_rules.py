"""Schema-v2 Telegram surface: rule commands, the holds ceiling, the two live
switches and their per-switch CONFIRM handshake. Pure logic + the persisting
CommandSession seam; no token, no sockets. `now` injected throughout."""

import datetime as dt

import pytest

from tennisbot.prefs import LONDON, Prefs, Rule, load_prefs, save_prefs
from tennisbot.telegram_commands import (CONFIRM_TIMEOUT, CommandSession,
                                         handle_message)

OWNER = "12345"
CENTRES = ["paddington", "westway"]
NOW = dt.datetime(2026, 7, 24, 10, 0, tzinfo=LONDON)


def send(text, prefs=None, chat_id=OWNER, now=NOW, pending=None, target=None,
         **kw):
    return handle_message(text, chat_id, prefs or Prefs.defaults(),
                          owner_chat_id=OWNER, now=now,
                          pending_confirm_until=pending,
                          pending_confirm_target=target,
                          valid_centres=CENTRES, **kw)


def session(tmp_path):
    return CommandSession(OWNER, tmp_path, valid_centres=CENTRES)


# ---------------------------------------------------------------------------
# /rules + /rule
# ---------------------------------------------------------------------------

def test_rules_on_an_empty_config_says_any_day_any_time():
    r = send("/rules")
    assert r.prefs is None                                   # read-only
    assert "any day, any time" in r.reply


def test_rule_appends_in_priority_order():
    p1 = send("/rule Tue Thu 18:00-").prefs
    assert p1.rules == (Rule(days=("Tue", "Thu"), earliest="18:00"),)
    p2 = send("/rule Sat 10:00-15:00", p1).prefs
    assert p2.rules == (Rule(days=("Tue", "Thu"), earliest="18:00"),
                        Rule(days=("Sat",), earliest="10:00", latest="15:00"))


def test_rule_sun_any_is_a_day_only_rule():
    p = send("/rule Sun any").prefs
    assert p.rules == (Rule(days=("Sun",)),)


def test_rule_open_ended_window_with_no_days():
    p = send("/rule 18:00-").prefs
    assert p.rules == (Rule(days=(), earliest="18:00"),)     # any day, ≥18:00


def test_rule_del_removes_the_nth_rule():
    p = Prefs(rules=(Rule(days=("Tue",)), Rule(days=("Sat",)),
                     Rule(days=("Sun",))))
    got = send("/rule del 2", p).prefs
    assert got.rules == (Rule(days=("Tue",)), Rule(days=("Sun",)))


def test_rule_del_out_of_range_is_rejected_and_keeps_config():
    p = Prefs(rules=(Rule(days=("Tue",)),))
    r = send("/rule del 5", p)
    assert r.prefs is None and "No rule #5" in r.reply


def test_rule_rejects_an_inverted_window():
    r = send("/rule Sat 20:00-10:00")
    assert r.prefs is None and "before" in r.reply


def test_rules_clear_removes_all_rules():
    p = Prefs(rules=(Rule(days=("Tue",)), Rule(days=("Sat",))))
    got = send("/rules clear", p).prefs
    assert got.rules == () and got.days == ()


def test_rules_list_shows_priority_numbers():
    p = Prefs(rules=(Rule(days=("Sun",), earliest="18:00"),
                     Rule(days=("Sat",))))
    r = send("/rules", p)
    assert r.prefs is None
    assert "1." in r.reply and "2." in r.reply and "Sun" in r.reply


# ---------------------------------------------------------------------------
# /days & /window as single-rule shorthand + the ≥2-rule guard
# ---------------------------------------------------------------------------

def test_days_and_window_still_edit_the_sole_rule():
    p1 = send("/days Tue Thu").prefs
    assert p1.rules == (Rule(days=("Tue", "Thu")),)
    p2 = send("/window 18:00-22:00", p1).prefs
    # /window edits the SAME sole rule, preserving its days.
    assert p2.rules == (Rule(days=("Tue", "Thu"), earliest="18:00",
                             latest="22:00"),)


def test_days_rejected_when_there_are_two_or_more_rules():
    p = Prefs(rules=(Rule(days=("Tue",)), Rule(days=("Sat",))))
    r = send("/days Wed", p)
    assert r.prefs is None                                   # never destroys them
    assert "/rule" in r.reply and p.rules == (Rule(days=("Tue",)),
                                              Rule(days=("Sat",)))


def test_window_rejected_when_there_are_two_or_more_rules():
    p = Prefs(rules=(Rule(days=("Tue",)), Rule(days=("Sat",))))
    r = send("/window 18:00-", p)
    assert r.prefs is None and "/rule" in r.reply


def test_days_rejection_with_multiple_rules_leaves_the_saved_config_untouched(
        tmp_path):
    # End-to-end through the persisting session: a rejected /days must write
    # NOTHING to disk (PRD §7 — an invalid update leaves the previous config
    # intact), proving the rejection isn't merely a not-returned-prefs in the
    # pure layer but actually never reaches save_prefs.
    before = save_prefs(Prefs(rules=(Rule(days=("Tue",)), Rule(days=("Sat",)))),
                        tmp_path)
    s = session(tmp_path)
    reply = s.handle("/days Wed", OWNER, now=NOW)
    assert "/rule" in reply                              # rejected with guidance
    assert load_prefs(tmp_path) == before                # config byte-for-byte intact


# ---------------------------------------------------------------------------
# /holds
# ---------------------------------------------------------------------------

def test_holds_sets_the_ceiling_and_zero_flags_paused():
    assert send("/holds 2").prefs.max_holds == 2
    r = send("/holds 0")
    assert r.prefs.max_holds == 0 and "paused" in r.reply


def test_holds_rejects_non_numbers():
    assert send("/holds -1").prefs is None
    assert send("/holds two").prefs is None


# ---------------------------------------------------------------------------
# The two live switches + per-switch CONFIRM handshake
# ---------------------------------------------------------------------------

def test_catcher_on_arms_only_the_catcher_target():
    r = send("/catcher on")
    assert r.prefs is None                                   # arm, not apply
    assert r.pending_confirm_until == NOW + CONFIRM_TIMEOUT
    assert r.pending_confirm_target == "catcher"


def test_drop_on_arms_only_the_drop_target():
    r = send("/drop on")
    assert r.pending_confirm_target == "drop"


def test_confirm_arms_the_targeted_switch_only():
    # CONFIRM after `/catcher on` must flip catcher_live ONLY, not drop_live.
    armed = send("/catcher on")
    r = send("CONFIRM", now=NOW + dt.timedelta(seconds=10),
             pending=armed.pending_confirm_until,
             target=armed.pending_confirm_target)
    assert r.prefs.catcher_live is True and r.prefs.drop_live is False


def test_confirm_arms_the_drop_only():
    armed = send("/drop on")
    r = send("CONFIRM", pending=armed.pending_confirm_until,
             target=armed.pending_confirm_target)
    assert r.prefs.drop_live is True and r.prefs.catcher_live is False


def test_catcher_off_is_immediate_no_handshake():
    r = send("/catcher off", Prefs(catcher_live=True, drop_live=True))
    assert r.prefs.catcher_live is False and r.prefs.drop_live is True
    assert r.pending_confirm_until is None


def test_live_off_is_a_panic_switch_turning_both_off():
    r = send("/live off", Prefs(catcher_live=True, drop_live=True))
    assert r.prefs.catcher_live is False and r.prefs.drop_live is False
    assert r.pending_confirm_until is None                   # no handshake


def test_live_on_arms_both():
    armed = send("/live on")
    assert armed.pending_confirm_target == "both"
    r = send("CONFIRM", pending=armed.pending_confirm_until,
             target=armed.pending_confirm_target)
    assert r.prefs.catcher_live is True and r.prefs.drop_live is True


# ---------------------------------------------------------------------------
# Handshake TARGET ISOLATION — the critical seam (an arming must never leak
# onto the other switch, and an unrelated message must cancel it cleanly).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["catcher", "drop"])
def test_arming_then_an_unrelated_command_cancels_the_arming(cmd):
    armed = send(f"/{cmd} on")
    # An unrelated command lands while armed: it must CANCEL the arming (clear
    # both pending fields) and, of course, arm nothing.
    r = send("/status", now=NOW + dt.timedelta(seconds=5),
             pending=armed.pending_confirm_until,
             target=armed.pending_confirm_target)
    assert r.pending_confirm_until is None and r.pending_confirm_target is None
    assert r.prefs is None                              # /status persists nothing
    assert "cancelled" in r.reply.lower()
    # A CONFIRM arriving AFTER the cancel must do nothing — the switch is disarmed.
    late = send("CONFIRM", now=NOW + dt.timedelta(seconds=10),
                pending=r.pending_confirm_until, target=r.pending_confirm_target)
    assert late.prefs is None


def test_re_arming_switches_the_target_so_confirm_hits_the_new_switch():
    # Arm catcher, then instead send `/drop on`: the pending target must re-point
    # to drop, so the following CONFIRM arms ONLY drop (never the abandoned
    # catcher). This is the "CONFIRM must apply to the RIGHT switch" property.
    armed = send("/catcher on")
    re_armed = send("/drop on", now=NOW + dt.timedelta(seconds=5),
                    pending=armed.pending_confirm_until,
                    target=armed.pending_confirm_target)
    assert re_armed.pending_confirm_target == "drop"
    r = send("CONFIRM", now=NOW + dt.timedelta(seconds=10),
             pending=re_armed.pending_confirm_until,
             target=re_armed.pending_confirm_target)
    assert r.prefs.drop_live is True and r.prefs.catcher_live is False


def test_confirm_arms_nothing_after_the_two_minute_window_expires():
    armed = send("/catcher on")
    r = send("CONFIRM", now=NOW + CONFIRM_TIMEOUT + dt.timedelta(seconds=1),
             pending=armed.pending_confirm_until,
             target=armed.pending_confirm_target)
    assert r.prefs is None and "expired" in r.reply.lower()


# ---------------------------------------------------------------------------
# End-to-end through the persisting session (target threads between messages)
# ---------------------------------------------------------------------------

def test_session_threads_the_catcher_target_across_messages(tmp_path):
    s = session(tmp_path)
    s.handle("/drop on", OWNER, now=NOW)
    assert s._pending_confirm_target == "drop"
    s.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=5))
    on_disk = load_prefs(tmp_path)
    assert on_disk.drop_live is True and on_disk.catcher_live is False


def test_a_stranger_cannot_complete_a_targeted_handshake(tmp_path):
    s = session(tmp_path)
    s.handle("/catcher on", OWNER, now=NOW)
    assert s.handle("CONFIRM", "66666", now=NOW + dt.timedelta(seconds=5)) is None
    assert load_prefs(tmp_path).catcher_live is False        # stranger changed nothing
    s.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=10))
    assert load_prefs(tmp_path).catcher_live is True


def test_untargeted_confirm_arms_nothing_fails_closed():
    # W3: a live handshake with a deadline but NO target must arm NOTHING (never
    # default to "both"). It cancels and re-prompts.
    armed = send("/live on")
    r = send("CONFIRM", pending=armed.pending_confirm_until, target=None)
    assert r.prefs is None                                  # nothing armed
    assert r.pending_confirm_until is None                  # handshake cleared
    assert "nothing changed" in r.reply.lower()


def test_status_warns_when_drop_is_live_and_a_rule_has_a_ceiling():
    # W2: the drop enforces only the floor, so surface the caveat — but only when
    # it matters (drop armed AND a rule carries a `latest`).
    p = Prefs(drop_live=True, rules=(Rule(days=("Sat",), earliest="10:00",
                                          latest="15:00"),))
    assert "only the window FLOOR" in send("/status", p).reply
    # No ceiling ⇒ no warning.
    p2 = Prefs(drop_live=True, rules=(Rule(days=("Sat",), earliest="10:00"),))
    assert "only the window FLOOR" not in send("/status", p2).reply
    # Drop not live ⇒ no warning even with a ceiling.
    p3 = Prefs(rules=(Rule(days=("Sat",), earliest="10:00", latest="15:00"),))
    assert "only the window FLOOR" not in send("/status", p3).reply


def test_holds_rejects_non_ascii_digits():
    # S3: consistency with /cap — non-ASCII digits and superscripts are rejected.
    assert send("/holds ٥").prefs is None            # Arabic-Indic 5
    assert send("/holds ⁵").prefs is None            # superscript


def test_rule_del_rejects_non_ascii_digits():
    p = Prefs(rules=(Rule(days=("Tue",)),))
    assert send("/rule del ١", p).prefs is None       # Arabic-Indic 1


def test_help_lists_the_new_commands():
    r = send("/help")
    for cmd in ("/rules", "/rule", "/holds", "/catcher", "/drop", "/live"):
        assert cmd in r.reply


def test_status_renders_two_flags_and_a_rule_list():
    p = Prefs(catcher_live=True, drop_live=False, max_holds=2,
              rules=(Rule(days=("Sun",), earliest="18:00"),
                     Rule(days=("Sat",))))
    r = send("/status", p)
    assert "catcher LIVE" in r.reply and "drop DRY" in r.reply
    assert "Rules (priority order):" in r.reply
    assert "Holds ceiling: 2" in r.reply
