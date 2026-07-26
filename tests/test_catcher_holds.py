"""Schema-v2 catcher logic: the concurrent-holds ceiling (Q2a) and the
positive-ID court counter (Q2c). Pure, offline."""

import datetime as dt
from zoneinfo import ZoneInfo

from tennisbot.catcher import (Candidate, WeekSlot, count_paid_court_bookings,
                               count_unpaid_holds, held_court_date_keys,
                               plan_cycle)
from tennisbot.prefs import Prefs, Rule

LONDON = ZoneInfo("Europe/London")

# Real court surface tokens (from config/targets.yaml).
COURTS = ("Tennis - Synth", "Tennis - Tarmac", "Outdoor Tennis")
ACTIVITIES = ("Tennis (adv) Sun 1300", "Tennis (adv) Wed 1900")


def _now(iso):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


def _booking(text, paid, day, mon):
    return {"text": text, "paid": paid, "day": day, "mon": mon}


def _slots(*specs):
    return {"paddington": [WeekSlot(d, t, st, "T") for (d, t, st) in specs]}


# ==========================================================================
# Positive court-ID counter (Q2c)
# ==========================================================================

def test_positive_id_excludes_a_swim_and_a_coaching_activity():
    week = [dt.date(2026, 7, d) for d in range(20, 27)]
    bookings = [
        _booking("Tennis - Synth", True, 22, "Jul"),          # court ✓
        _booking("Swim - Adult", True, 23, "Jul"),            # swim ✗
        _booking("Tennis (adv) Wed 1900", True, 22, "Jul"),   # coaching ✗
        _booking("Gym Session", True, 24, "Jul"),             # gym ✗
    ]
    n = count_paid_court_bookings(bookings, week, ACTIVITIES, COURTS)
    assert n == 1                       # only the real court hire counts


def test_empty_court_tokens_fall_back_to_the_negative_rule():
    # CRITICAL fail-safe: with NO court tokens we can't positively ID a court, so
    # we over-count (paid non-activity) rather than under-count. A swim here IS
    # counted (safe: a false "cap reached", never an over-book).
    week = [dt.date(2026, 7, d) for d in range(20, 27)]
    bookings = [
        _booking("Swim - Adult", True, 23, "Jul"),
        _booking("Tennis (adv) Wed 1900", True, 22, "Jul"),   # still excluded
    ]
    assert count_paid_court_bookings(bookings, week, ACTIVITIES, ()) == 1
    # ...and the coaching activity is excluded in BOTH branches.


def test_held_keys_over_count_and_survive_a_token_miss():
    # C1 fail-SAFE: idempotency uses the NEGATIVE rule, so a court whose text
    # lacks a known surface token is STILL marked held (never re-booked), AND an
    # ad-hoc booking (a swim) also marks its date held (over-count = safe skip).
    bookings = [
        _booking("Tennis Court 3 (unlisted surface)", False, 23, "Jul"),  # token miss → still held ✓
        _booking("Swim - Adult", False, 24, "Jul"),          # over-counted (safe) ✓
        _booking("Tennis (adv) Wed 1900", False, 25, "Jul"),  # activity ✗
    ]
    assert held_court_date_keys(bookings, ACTIVITIES) == {(23, "Jul"),
                                                          (24, "Jul")}


def test_count_unpaid_holds_over_counts_and_survives_a_token_miss():
    # C1 fail-SAFE: the ceiling uses the NEGATIVE rule, so a token-miss court
    # hold and an ad-hoc swim both count (over-count trips the ceiling sooner —
    # never a ceiling that fails to trip). Only paid + activities are excluded.
    bookings = [
        _booking("Tennis Court 3 (unlisted surface)", False, 22, "Jul"),  # token miss → counts ✓
        _booking("Swim - Adult", False, 23, "Jul"),          # over-counted (safe) ✓
        _booking("Tennis - Synth", True, 24, "Jul"),          # PAID ✗
        _booking("Tennis (adv) Wed 1900", False, 25, "Jul"),  # activity ✗
    ]
    assert count_unpaid_holds(bookings, ACTIVITIES) == 2


# ==========================================================================
# Hold ceiling in plan_cycle (Q2a)
# ==========================================================================

def _prefs(max_holds=5, weekly_cap=3):
    return Prefs(centres=("paddington",), max_holds=max_holds,
                 weekly_cap=weekly_cap)


def test_below_the_ceiling_books_normally():
    prefs = _prefs(max_holds=2)
    slots = _slots(("2026-07-25", "18:00", "available"))
    holds = [_booking("Tennis - Synth", False, 23, "Jul")]     # 1 unpaid < 2
    plan = plan_cycle(slots, prefs, {}, holds, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.to_book == Candidate("paddington", "2026-07-25", "18:00")


def test_at_the_ceiling_holds_off_and_reports_hold_ceiling_reached():
    prefs = _prefs(max_holds=2)
    slots = _slots(("2026-07-25", "18:00", "available"))
    holds = [_booking("Tennis - Synth", False, 23, "Jul"),
             _booking("Tennis - Tarmac", False, 24, "Jul")]    # 2 unpaid == 2
    plan = plan_cycle(slots, prefs, {}, holds, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.to_book is None and plan.reason == "hold_ceiling_reached"
    assert plan.candidate == Candidate("paddington", "2026-07-25", "18:00")
    assert plan.holds == 2


def test_ceiling_holds_the_top_priority_slot_first():
    # Candidates walk in priority order, so the ceiling stops at (and reports)
    # the highest-priority bookable slot.
    prefs = Prefs(centres=("paddington",), max_holds=1,
                  rules=(Rule(days=("Sun",), earliest="18:00"),
                         Rule(days=("Sat",))))
    slots = _slots(("2026-07-25", "10:00", "available"),      # Sat → rule 1
                   ("2026-07-26", "18:00", "available"))      # Sun → rule 0
    holds = [_booking("Tennis - Synth", False, 23, "Jul")]    # 1 == ceiling
    plan = plan_cycle(slots, prefs, {}, holds, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.reason == "hold_ceiling_reached"
    assert plan.candidate == Candidate("paddington", "2026-07-26", "18:00")


def test_max_holds_zero_pauses_all_new_holds():
    prefs = _prefs(max_holds=0)
    slots = _slots(("2026-07-25", "18:00", "available"))
    plan = plan_cycle(slots, prefs, {}, [], ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.to_book is None and plan.reason == "hold_ceiling_reached"


def test_ceiling_takes_precedence_over_the_weekly_cap():
    # Both would block; the ceiling is checked first, so its reason is reported.
    prefs = _prefs(max_holds=1, weekly_cap=1)
    slots = _slots(("2026-07-23", "18:00", "available"))
    bookings = [
        _booking("Tennis - Synth", True, 22, "Jul"),          # 1 paid == cap
        _booking("Tennis - Tarmac", False, 24, "Jul")]        # 1 unpaid == ceiling
    plan = plan_cycle(slots, prefs, {}, bookings, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.reason == "hold_ceiling_reached"


def test_an_unpaid_swim_hold_counts_toward_the_ceiling_fail_safe():
    # C1: the ceiling OVER-counts by design — an ad-hoc unpaid swim hold consumes
    # a hold slot (safe: hold off), rather than positive-ID risking a ceiling
    # that never trips. A paid swim would NOT count (only unpaid holds do).
    prefs = _prefs(max_holds=1)
    slots = _slots(("2026-07-25", "18:00", "available"))
    holds = [_booking("Swim - Adult", False, 23, "Jul")]      # unpaid non-activity
    plan = plan_cycle(slots, prefs, {}, holds, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.to_book is None and plan.reason == "hold_ceiling_reached"


# ==========================================================================
# Monday-straddle: the D0–D+7 scan crosses a Monday reset, so a THIS-week
# block must not silently black out a NEXT-week candidate — but the two
# ceilings differ (weekly cap is per-week, the hold ceiling is global).
# ==========================================================================

def test_capped_this_week_still_books_an_uncapped_next_week_slot():
    # weekly_cap is per Mon-reset week: this week (20–26 Jul) is capped, but the
    # walk must fall through to the Mon-27 (next week, 0 paid) candidate.
    prefs = _prefs(max_holds=5, weekly_cap=1)
    slots = _slots(("2026-07-23", "18:00", "available"),      # this wk → capped
                   ("2026-07-27", "18:00", "available"))      # next wk → free
    bookings = [_booking("Tennis - Synth", True, 22, "Jul")]  # 1 paid this week
    plan = plan_cycle(slots, prefs, {}, bookings, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.to_book == Candidate("paddington", "2026-07-27", "18:00")
    assert plan.reason != "cap_reached"


def test_the_hold_ceiling_is_global_and_holds_off_even_a_next_week_slot():
    # Unlike the weekly cap, the concurrent-holds ceiling is a GLOBAL stop:
    # over the ceiling, NOTHING new is held this cycle regardless of week — and
    # the slot reported is the highest-priority one (soonest = this week).
    prefs = _prefs(max_holds=1, weekly_cap=5)
    slots = _slots(("2026-07-23", "18:00", "available"),
                   ("2026-07-27", "18:00", "available"))
    holds = [_booking("Tennis - Synth", False, 22, "Jul")]    # 1 unpaid == ceiling
    plan = plan_cycle(slots, prefs, {}, holds, ACTIVITIES,
                      _now("2026-07-20T09:00:00"), COURTS)
    assert plan.to_book is None and plan.reason == "hold_ceiling_reached"
    assert plan.candidate == Candidate("paddington", "2026-07-23", "18:00")
