"""Deterministic tests for the catcher's pure logic (no network, no browser).

Covers the week-grid parser, the prefs matcher, the weekly-cap counter, the
lapsed-hold policy, and plan_cycle — the offline half of catcher.py."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tennisbot.catcher import (Candidate, WeekSlot, classify_hold,
                               count_paid_court_bookings, match_candidates,
                               parse_week_cells, plan_cycle, record_hold,
                               should_rebook, slot_key, week_dates_for)
from tennisbot.prefs import Prefs

LONDON = ZoneInfo("Europe/London")


def _now(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


# ==========================================================================
# Week-grid parser (data-qa-id per recon/FINDINGS.md)
# ==========================================================================

def _cell(qa, **kw):
    return {"qa": qa, "value": kw.get("value", ""),
            "tdcls": kw.get("tdcls", ""), "disabled": kw.get("disabled", False)}


def test_parse_available_cell_reformats_date_and_pads_time():
    qa = ("button-ActivityID=156TENNIS2 Date=26/07/2026 08:00:00 "
          "Availability= Available")
    [s] = parse_week_cells([_cell(qa)])
    assert s == WeekSlot(date="2026-07-26", time="08:00",
                         state="available", activity_id="156TENNIS2")


def test_parse_not_available_is_unavailable_not_available():
    # "Not Available" CONTAINS "available" — the negative must win.
    qa = ("button-ActivityID=156TENNIS2 Date=26/07/2026 19:00:00 "
          "Availability= Not Available")
    [s] = parse_week_cells([_cell(qa)])
    assert s.state == "unavailable"


def test_parse_my_booking_third_state():
    qa = ("button-ActivityID=156TENNIS2 Date=26/07/2026 20:00:00 "
          "Availability= My Booking")
    [s] = parse_week_cells([_cell(qa)])
    assert s.state == "my_booking"


def test_parse_falls_back_to_corroborating_signals_when_availability_blank():
    # data-qa-id present but Availability empty → cross-check td class / disabled.
    qa = "button-ActivityID=156TENNIS2 Date=26/07/2026 09:00:00 Availability= "
    [avail] = parse_week_cells([_cell(qa, tdcls="itemavailable", value="Available")])
    assert avail.state == "available"
    [na] = parse_week_cells([_cell(qa, tdcls="itemnotavailable", disabled=True)])
    assert na.state == "unavailable"


def test_parse_skips_unparseable_cells():
    assert parse_week_cells([{"qa": "garbage"}, {"qa": ""}, {}]) == []


# ==========================================================================
# Matcher (Stage-2 fine filter)
# ==========================================================================

def _prefs(**kw):
    return Prefs(**kw)


def test_match_filters_by_day_time_and_state():
    prefs = _prefs(centres=("paddington",), days=("Sun",), earliest="18:00")
    slots = {
        "paddington": [
            WeekSlot("2026-07-26", "18:00", "available", "T"),   # Sun ✓
            WeekSlot("2026-07-26", "17:00", "available", "T"),   # before 18:00 ✗
            WeekSlot("2026-07-27", "19:00", "available", "T"),   # Mon (not Sun) ✗
            WeekSlot("2026-07-26", "20:00", "my_booking", "T"),  # already held ✗
        ]
    }
    got = match_candidates(slots, prefs, _now("2026-07-20T09:00:00"))
    assert got == [Candidate("paddington", "2026-07-26", "18:00")]


def test_match_orders_by_centre_priority_then_date_then_time():
    prefs = _prefs(centres=("westway", "paddington"))
    slots = {
        "paddington": [WeekSlot("2026-07-25", "18:00", "available", "T")],
        "westway": [WeekSlot("2026-07-26", "20:00", "available", "T"),
                    WeekSlot("2026-07-26", "18:00", "available", "T")],
    }
    got = match_candidates(slots, prefs, _now("2026-07-20T09:00:00"))
    # westway first (priority), earliest time first within it, then paddington.
    assert got == [Candidate("westway", "2026-07-26", "18:00"),
                   Candidate("westway", "2026-07-26", "20:00"),
                   Candidate("paddington", "2026-07-25", "18:00")]


def test_match_skips_past_slots_today():
    prefs = _prefs(centres=("paddington",))
    slots = {"paddington": [WeekSlot("2026-07-20", "08:00", "available", "T"),
                            WeekSlot("2026-07-20", "21:00", "available", "T")]}
    got = match_candidates(slots, prefs, _now("2026-07-20T18:00:00"))
    assert got == [Candidate("paddington", "2026-07-20", "21:00")]


# ==========================================================================
# Weekly cap
# ==========================================================================

def test_week_dates_are_monday_to_sunday():
    # 2026-07-23 is a Thursday; its Mon-reset week is 20 Jul (Mon)–26 Jul (Sun).
    wk = week_dates_for(dt.date(2026, 7, 23))
    assert wk[0] == dt.date(2026, 7, 20) and wk[-1] == dt.date(2026, 7, 26)


def _booking(text, paid, day, mon):
    return {"text": text, "paid": paid, "day": day, "mon": mon}


def test_cap_counts_paid_courts_only_this_week_excluding_activities():
    week = week_dates_for(dt.date(2026, 7, 23))
    activity_matches = ("Tennis (adv) Sun 1300", "Tennis (adv) Wed 1900")
    bookings = [
        _booking("Tennis - Synth", True, 22, "Jul"),          # paid court, in-week ✓
        _booking("Tennis - Tarmac", True, 25, "Jul"),         # paid court, in-week ✓
        _booking("Tennis - Synth", False, 24, "Jul"),         # UNPAID hold ✗
        _booking("Tennis - Synth", True, 29, "Jul"),          # next week ✗
        _booking("Tennis (adv) Wed 1900", True, 22, "Jul"),   # activity ✗
    ]
    assert count_paid_court_bookings(bookings, week, activity_matches) == 2


def test_cap_monday_reset_separates_adjacent_weeks():
    activity_matches = ()
    this_wk = week_dates_for(dt.date(2026, 7, 23))    # 20–26 Jul
    bookings = [_booking("Tennis - Synth", True, 27, "Jul")]  # Mon 27 = next week
    assert count_paid_court_bookings(bookings, this_wk, activity_matches) == 0
    next_wk = week_dates_for(dt.date(2026, 7, 27))
    assert count_paid_court_bookings(bookings, next_wk, activity_matches) == 1


# ==========================================================================
# Lapsed-hold policy (§4.4)
# ==========================================================================

def test_classify_daytime_vs_overnight():
    assert classify_hold(_now("2026-07-20T09:00:00")) == "daytime"
    assert classify_hold(_now("2026-07-20T22:59:00")) == "daytime"
    assert classify_hold(_now("2026-07-20T23:00:00")) == "overnight"
    assert classify_hold(_now("2026-07-20T02:00:00")) == "overnight"


def test_first_hold_when_no_memory():
    d = should_rebook(None, _now("2026-07-20T14:00:00"))
    assert d.should_book and d.phase == "first"


def test_daytime_hold_rebooks_at_most_once():
    entry = record_hold(None, _now("2026-07-20T14:00:00"), "first")
    # lapsed → one re-book allowed
    d1 = should_rebook(entry, _now("2026-07-20T15:00:00"))
    assert d1.should_book and d1.phase == "daytime"
    entry = record_hold(entry, _now("2026-07-20T15:00:00"), d1.phase)
    # lapsed again → released for the day
    d2 = should_rebook(entry, _now("2026-07-20T16:00:00"))
    assert not d2.should_book and d2.phase == "released"


def test_overnight_hold_persists_every_cycle_until_9am():
    entry = record_hold(None, _now("2026-07-20T23:30:00"), "first")
    # 00:35, 01:30, 08:30 → all persist (overnight)
    for t in ("2026-07-21T00:35:00", "2026-07-21T01:30:00", "2026-07-21T08:30:00"):
        d = should_rebook(entry, _now(t))
        assert d.should_book and d.phase == "overnight"
        entry = record_hold(entry, _now(t), d.phase)
    # after 09:00 the daytime rule takes over: one more, then released.
    d_day = should_rebook(entry, _now("2026-07-21T09:30:00"))
    assert d_day.should_book and d_day.phase == "daytime"
    entry = record_hold(entry, _now("2026-07-21T09:30:00"), d_day.phase)
    d_rel = should_rebook(entry, _now("2026-07-21T10:30:00"))
    assert not d_rel.should_book and d_rel.phase == "released"


# ==========================================================================
# plan_cycle
# ==========================================================================

def _slots(*specs):
    return {"paddington": [WeekSlot(d, t, st, "T") for (d, t, st) in specs]}


def test_plan_books_the_soonest_match():
    prefs = _prefs(centres=("paddington",), weekly_cap=3)
    slots = _slots(("2026-07-26", "18:00", "available"),
                   ("2026-07-25", "19:00", "available"))
    plan = plan_cycle(slots, prefs, {}, [], (), _now("2026-07-20T09:00:00"))
    assert plan.to_book == Candidate("paddington", "2026-07-25", "19:00")
    assert plan.reason != "cap_reached"


def test_plan_silent_when_no_match():
    prefs = _prefs(centres=("paddington",), days=("Mon",))
    slots = _slots(("2026-07-26", "18:00", "available"))   # a Sunday
    plan = plan_cycle(slots, prefs, {}, [], (), _now("2026-07-20T09:00:00"))
    assert plan.to_book is None and plan.reason == "no_bookable"


def test_plan_blocks_at_cap_and_reports_the_candidate():
    prefs = _prefs(centres=("paddington",), weekly_cap=1)
    slots = _slots(("2026-07-23", "18:00", "available"))
    bookings = [_booking("Tennis - Synth", True, 22, "Jul")]   # 1 paid this week
    plan = plan_cycle(slots, prefs, {}, bookings, (), _now("2026-07-20T09:00:00"))
    assert plan.to_book is None and plan.reason == "cap_reached"
    assert plan.candidate == Candidate("paddington", "2026-07-23", "18:00")
    assert plan.paid == 1


def test_plan_cap_zero_pauses_booking():
    prefs = _prefs(centres=("paddington",), weekly_cap=0)
    slots = _slots(("2026-07-23", "18:00", "available"))
    plan = plan_cycle(slots, prefs, {}, [], (), _now("2026-07-20T09:00:00"))
    assert plan.to_book is None and plan.reason == "cap_reached"


def test_plan_skips_released_slot_and_takes_the_next():
    prefs = _prefs(centres=("paddington",), weekly_cap=3)
    slots = _slots(("2026-07-23", "18:00", "available"),
                   ("2026-07-23", "19:00", "available"))
    # 18:00 already re-booked its one daytime allowance → released.
    key = slot_key("paddington", "2026-07-23", "18:00")
    memory = {key: {"first_held": "2026-07-20T14:00:00+01:00",
                    "overnight_rebooks": 0, "daytime_rebooks": 1}}
    plan = plan_cycle(slots, prefs, memory, [], (), _now("2026-07-20T15:00:00"))
    assert plan.to_book == Candidate("paddington", "2026-07-23", "19:00")
