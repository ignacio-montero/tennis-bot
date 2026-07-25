"""Edge-case coverage for the catcher's PURE logic (no browser, no network).

Complements test_catcher_logic.py. These are boundary-value and adversarial-input
tests chosen where the *bug risk* is highest: the DD/MM→ISO reformat (a silent
month/day swap would book the wrong day), the weekly-cap off-by-one and its
Monday-straddle behaviour, the lapsed-hold clock boundaries at exactly 09:00, and
the parser's fail-closed fallbacks on partial markup.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tennisbot.catcher import (Candidate, WeekSlot, _next_9am, classify_hold,
                               count_paid_court_bookings, match_candidates,
                               parse_week_cells, plan_cycle, should_rebook,
                               week_dates_for)
from tennisbot.prefs import Prefs

LONDON = ZoneInfo("Europe/London")


def _now(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


def _cell(qa, **kw):
    return {"qa": qa, "value": kw.get("value", ""),
            "tdcls": kw.get("tdcls", ""), "disabled": kw.get("disabled", False)}


def _prefs(**kw):
    return Prefs(**kw)


def _slots(*specs):
    return {"paddington": [WeekSlot(d, t, st, "T") for (d, t, st) in specs]}


def _booking(text, paid, day, mon="Jul"):
    return {"text": text, "paid": paid, "day": day, "mon": mon}


# ==========================================================================
# Parser — boundary/adversarial (recon/FINDINGS.md → "Week-view grid")
# ==========================================================================

def test_parse_reformats_ddmm_not_mmdd():
    """05/03/2026 must become 2026-03-05 (5 March), NOT 2026-05-03. A month/day
    swap would silently book the wrong calendar day — the highest-consequence
    parse bug, so it gets its own regression test with an unambiguous date
    (day > 12 and month <= 12 can't tell the two orderings apart)."""
    qa = ("button-ActivityID=156TENNIS2 Date=05/03/2026 09:00:00 "
          "Availability= Available")
    [s] = parse_week_cells([_cell(qa)])
    assert s.date == "2026-03-05"


def test_parse_real_recon_markup_verbatim():
    """The exact cell captured in recon (value + itemavailable td class +
    data-qa-id all present) parses to a bookable available slot."""
    qa = ("button-ActivityID=156TENNIS2 Date=26/07/2026 08:00:00 "
          "Availability= Available")
    [s] = parse_week_cells([_cell(qa, value="Available", tdcls="itemavailable")])
    assert s == WeekSlot("2026-07-26", "08:00", "available", "156TENNIS2")


def test_parse_malformed_date_is_skipped_not_fatal():
    """A qa-id whose Date isn't DD/MM/YYYY (here ISO-ish) fails the regex and is
    dropped, never crashing the whole scan — one stray control mustn't wedge a
    cycle."""
    qa = "button-ActivityID=X Date=2026/07/26 09:00:00 Availability= Available"
    assert parse_week_cells([_cell(qa)]) == []


def test_parse_fallback_my_booking_via_input_value():
    """When data-qa-id's Availability is blank, the corroborating <input value>
    must still surface a My Booking (idempotency depends on detecting held
    slots even if the qa-id is uninformative)."""
    qa = "button-ActivityID=X Date=26/07/2026 20:00:00 Availability= "
    [s] = parse_week_cells([_cell(qa, value="My Booking")])
    assert s.state == "my_booking"


def test_parse_fallback_unknown_fails_closed_to_unavailable():
    """Blank availability AND no corroborating signal ⇒ 'unavailable'. Fail
    closed: an unrecognised cell is treated as NOT bookable, never accidentally
    booked."""
    qa = "button-ActivityID=X Date=26/07/2026 20:00:00 Availability= "
    [s] = parse_week_cells([_cell(qa)])
    assert s.state == "unavailable"


def test_parse_fallback_not_available_via_input_value():
    qa = "button-ActivityID=X Date=26/07/2026 20:00:00 Availability= "
    [s] = parse_week_cells([_cell(qa, value="Not Available")])
    assert s.state == "unavailable"


# ==========================================================================
# Weekly cap — off-by-one + the Monday-straddle (D0–D+7 crosses week boundary)
# ==========================================================================

def test_cap_off_by_one_books_at_cap_minus_one_blocks_at_cap():
    """Boundary-value analysis on the cap comparison (`paid >= cap`): with cap=3,
    2 paid still books, exactly 3 paid blocks. Guards the classic off-by-one
    (`>` vs `>=`)."""
    prefs = _prefs(centres=("paddington",), weekly_cap=3)
    slots = _slots(("2026-07-23", "18:00", "available"))
    # Cap-filling bookings on OTHER days of the week (20/21/22), NOT day 23 —
    # a paid booking on the candidate's own date would be excluded first by the
    # authoritative per-date idempotency, which is a different property than the
    # cap boundary this test pins.
    two = [_booking("Tennis", True, d) for d in (20, 21)]
    three = [_booking("Tennis", True, d) for d in (20, 21, 22)]
    now = _now("2026-07-20T09:00:00")
    assert plan_cycle(slots, prefs, {}, two, (), now).to_book is not None
    assert plan_cycle(slots, prefs, {}, three, (), now).reason == "cap_reached"


def test_cap_unpaid_holds_do_not_block_booking():
    """Only PAID bookings count toward the cap (§4.3). cap=1 with one UNPAID hold
    this week must still allow a fresh book — a lapsed hold means 'I didn't get
    the court', so it can't consume the budget."""
    prefs = _prefs(centres=("paddington",), weekly_cap=1)
    slots = _slots(("2026-07-23", "18:00", "available"))
    bookings = [_booking("Tennis", False, 22)]      # unpaid hold, in-week
    plan = plan_cycle(slots, prefs, {}, bookings, (), _now("2026-07-20T09:00:00"))
    assert plan.to_book == Candidate("paddington", "2026-07-23", "18:00")


def test_cap_straddle_books_next_week_slot_when_this_week_is_full():
    """2026-07-25 is a Saturday. This week (Mon 20–Sun 26) is at cap (3 paid,
    cap 3); a slot has also freed next week (Mon 27, 0 paid). count_paid_court_
    bookings correctly keys each candidate to its OWN week, so next Monday's slot
    is NOT capped and should be booked. Expected: to_book == next-week slot."""
    prefs = _prefs(centres=("paddington",), weekly_cap=3)
    slots = _slots(("2026-07-26", "18:00", "available"),   # this week, capped
                   ("2026-07-27", "18:00", "available"))   # next week, free
    bookings = [_booking("Tennis", True, d) for d in (22, 23, 24)]
    plan = plan_cycle(slots, prefs, {}, bookings, (), _now("2026-07-25T09:00:00"))
    assert plan.to_book == Candidate("paddington", "2026-07-27", "18:00")


def test_cap_counter_keys_each_week_independently():
    """The underlying counter IS correct per week — this passes and localises the
    straddle bug above to plan_cycle's control flow, not the counter."""
    bookings = [_booking("Tennis", True, d) for d in (22, 23, 24)]
    this_wk = week_dates_for(dt.date(2026, 7, 25))     # 20–26 Jul
    next_wk = week_dates_for(dt.date(2026, 7, 27))     # 27 Jul–2 Aug
    assert count_paid_court_bookings(bookings, this_wk, ()) == 3
    assert count_paid_court_bookings(bookings, next_wk, ()) == 0


# ==========================================================================
# Lapsed-hold — clock boundaries at exactly 09:00 / 23:00 (§4.4)
# ==========================================================================

def test_classify_boundaries_inclusive_09_exclusive_23():
    assert classify_hold(_now("2026-07-20T08:59:00")) == "overnight"
    assert classify_hold(_now("2026-07-20T09:00:00")) == "daytime"   # 09:00 in
    assert classify_hold(_now("2026-07-20T22:59:00")) == "daytime"
    assert classify_hold(_now("2026-07-20T23:00:00")) == "overnight"  # 23:00 out


def test_overnight_persists_up_to_but_not_including_9am():
    """The handover is a STRICT boundary: 08:59 still 'overnight' (persist),
    exactly 09:00 flips to the daytime rule. An off-by-one here would either
    re-hold one cycle too long or drop a slot a cycle too early while asleep."""
    entry = {"first_held": "2026-07-20T23:30:00+01:00",
             "overnight_rebooks": 3, "daytime_rebooks": 0}
    assert should_rebook(entry, _now("2026-07-21T08:59:00")).phase == "overnight"
    assert should_rebook(entry, _now("2026-07-21T09:00:00")).phase == "daytime"


def test_next_9am_same_day_when_first_held_before_9():
    """An overnight first-hold BEFORE 09:00 (e.g. 06:00) hands over at the SAME
    day's 09:00, not the next day's — otherwise a 6am hold would persist a full
    extra 24h."""
    assert _next_9am(_now("2026-07-20T06:00:00")) == _now("2026-07-20T09:00:00")
    assert _next_9am(_now("2026-07-20T23:30:00")) == _now("2026-07-21T09:00:00")


# ==========================================================================
# Idempotency — a held (my_booking) slot is never a booking target
# ==========================================================================

def test_plan_cycle_ignores_my_booking_slots_entirely():
    """A grid slot the account already holds surfaces as 'my_booking'; even when
    it matches the prefs perfectly it must never become a booking target — this
    is the grid-level idempotency guard (§8.2)."""
    prefs = _prefs(centres=("paddington",), weekly_cap=3)
    slots = _slots(("2026-07-23", "18:00", "my_booking"))
    plan = plan_cycle(slots, prefs, {}, [], (), _now("2026-07-20T09:00:00"))
    assert plan.to_book is None and plan.reason == "no_bookable"


def test_match_excludes_my_booking_but_keeps_a_free_sibling():
    """Two slots on the same wanted day: one already held, one free. Only the
    free one is a candidate."""
    prefs = _prefs(centres=("paddington",))
    slots = _slots(("2026-07-23", "18:00", "my_booking"),
                   ("2026-07-23", "19:00", "available"))
    got = match_candidates(slots, prefs, _now("2026-07-20T09:00:00"))
    assert got == [Candidate("paddington", "2026-07-23", "19:00")]


def test_prune_expired_slots_drops_past_dates_only():
    # Per-slot memory for past dates is dead weight (keys are date-unique, only
    # relevant in the D0-D+7 window) — pruning keeps catcher-state.json bounded.
    from tennisbot.catcher import prune_expired_slots
    state = {"slots": {
        "paddington|2026-07-18|18:00": {"phase": "daytime"},   # past
        "paddington|2026-07-25|18:00": {"phase": "first"},     # today
        "paddington|2026-07-30|18:00": {"phase": "first"},     # future
    }}
    prune_expired_slots(state, "2026-07-25")
    assert set(state["slots"]) == {
        "paddington|2026-07-25|18:00", "paddington|2026-07-30|18:00"}
