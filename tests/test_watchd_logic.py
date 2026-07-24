"""Deterministic tests for watchd scheduling/flip logic (no network)."""

import datetime as dt
from zoneinfo import ZoneInfo

from tennisbot.watchd import (FlipTracker, REQUIRE_CLOSED, bracket_hot_window,
                              in_blackout, in_hot_window, next_blackout_end)

LONDON = ZoneInfo("Europe/London")


def _at(day: int, hhmm: str) -> dt.datetime:
    # 2026-07-06 is a Monday; day = weekday offset (0=Mon .. 6=Sun).
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(2026, 7, 6 + day, h, m, tzinfo=LONDON)


# -- blackouts ---------------------------------------------------------------

def test_blackout_covers_wed_and_sun_jobs():
    assert in_blackout(_at(2, "19:00"))      # Wed primary
    assert in_blackout(_at(2, "20:30"))      # Wed backup
    assert in_blackout(_at(6, "13:00"))      # Sun primary
    assert in_blackout(_at(6, "14:30"))      # Sun backup


def test_blackout_leaves_evening_window_free():
    assert not in_blackout(_at(2, "21:50"))  # Wed evening drop window is clear
    assert not in_blackout(_at(6, "21:50"))
    assert not in_blackout(_at(0, "19:00"))  # Monday 19:00 fine


def test_next_blackout_end():
    end = next_blackout_end(_at(2, "19:00"))
    assert (end.hour, end.minute) == (19, 25)


# -- hot windows -------------------------------------------------------------

def test_hot_window_membership():
    assert in_hot_window(_at(0, "21:50"), ["21:35-22:15"])
    assert not in_hot_window(_at(0, "22:15"), ["21:35-22:15"])
    assert not in_hot_window(_at(0, "12:00"), ["21:35-22:15"])


def test_hot_window_crossing_midnight():
    w = ["23:40-00:30"]
    assert in_hot_window(_at(0, "23:40"), w)      # start inclusive
    assert in_hot_window(_at(0, "23:59"), w)      # before midnight
    assert in_hot_window(_at(0, "00:00"), w)      # midnight itself
    assert in_hot_window(_at(0, "00:29"), w)      # after midnight
    assert not in_hot_window(_at(0, "00:30"), w)  # end exclusive
    assert not in_hot_window(_at(0, "23:39"), w)
    assert not in_hot_window(_at(0, "12:00"), w)


def test_auto_bracket_window_crossing_midnight_matches():
    # The real bracket recorded 2026-07-12→13: last_closed 23:59, first_open
    # 00:19 → auto window "23:54-00:24" crosses midnight and must match.
    bracket = {"date": "2026-07-20",
               "last_closed": "2026-07-12T23:59:40+01:00",
               "first_open": "2026-07-13T00:19:40+01:00"}
    w = bracket_hot_window(bracket, pad_min=5)
    assert w == "23:54-00:24"
    assert in_hot_window(_at(0, "23:58"), [w])
    assert in_hot_window(_at(0, "00:10"), [w])
    assert not in_hot_window(_at(0, "01:00"), [w])


def test_bracket_hot_window_pads_both_sides():
    bracket = {"date": "2026-07-19",
               "last_closed": "2026-07-12T21:48:10+01:00",
               "first_open": "2026-07-12T21:51:02+01:00"}
    assert bracket_hot_window(bracket, pad_min=5) == "21:43-21:56"
    assert bracket_hot_window(None) is None


# -- flip detection ----------------------------------------------------------

def _feed_closed(tr, date, n, t0="2026-07-12T20:00:00+01:00"):
    for i in range(n):
        assert tr.observe(date, 0, "row_full",
                          f"2026-07-12T20:0{i}:00+01:00",
                          f"2026-07-12T20:0{i}:30+01:00") is None


def test_flip_after_sustained_closed_yields_drop_with_bracket():
    tr = FlipTracker.new()
    _feed_closed(tr, "2026-07-19", REQUIRE_CLOSED)
    ev = tr.observe("2026-07-19", 12, "avail/60",
                    "2026-07-12T21:50:00+01:00", "2026-07-12T21:50:12+01:00")
    assert ev["event"] == "drop"
    assert ev["last_closed"] == f"2026-07-12T20:0{REQUIRE_CLOSED-1}:30+01:00"
    assert ev["first_open"] == "2026-07-12T21:50:00+01:00"
    # further open reads are silent
    assert tr.observe("2026-07-19", 10, "avail/60", "x", "y") is None


def test_open_without_baseline_is_flagged_not_a_drop():
    tr = FlipTracker.new()
    ev = tr.observe("2026-07-19", 12, "avail/60", "t1", "t2")
    assert ev["event"] == "open_at_first_read"
    assert tr.observe("2026-07-19", 12, "avail/60", "t3", "t4") is None


def test_error_reads_do_not_break_the_closed_streak():
    tr = FlipTracker.new()
    _feed_closed(tr, "2026-07-19", REQUIRE_CLOSED)
    assert tr.observe("2026-07-19", -2, "error:TimeoutError", "t", "t") is None
    ev = tr.observe("2026-07-19", 5, "avail/60", "t5", "t6")
    assert ev["event"] == "drop"


def test_dates_tracked_independently():
    tr = FlipTracker.new()
    _feed_closed(tr, "2026-07-19", REQUIRE_CLOSED)
    ev = tr.observe("2026-07-20", 3, "avail/60", "t1", "t2")
    assert ev["event"] == "open_at_first_read"   # +8 has its own baseline


# -- nightly drop blackout (sidecar hand-off) --------------------------------

def test_nightly_drop_blackout_covers_midnight():
    # watchd must yield to the live drop booker across midnight, every night.
    assert in_blackout(_at(0, "23:55"))       # Mon pre-warm launch window
    assert in_blackout(_at(3, "00:03"))       # Thu just after the drop
    assert not in_blackout(_at(0, "23:40"))   # before the window
    assert not in_blackout(_at(0, "00:10"))   # after the window


def test_blackout_is_open_before_the_sprinter_wakes():
    """THE coupling: `drop-loop --lead-min` and DROP_BLACKOUT are two halves of
    one handshake. The booker wakes at 00:00 − lead to do a cold login; if the
    blackout isn't open yet, watchd is still holding the EA session and the two
    collide (2026-07-23: 14s of margin, and the login timed out).

    This test fails if someone raises the lead without widening the blackout.
    """
    from tennisbot.runner import run_drop_loop
    import inspect
    lead_min = inspect.signature(run_drop_loop).parameters["lead_min"].default
    wake_h, wake_m = divmod(int(round(24 * 60 - lead_min)), 60)   # 00:00 − lead
    assert in_blackout(_at(0, f"{wake_h:02d}:{wake_m:02d}")), (
        f"booker wakes at {wake_h:02d}:{wake_m:02d} (lead_min={lead_min}) but "
        f"DROP_BLACKOUT is not open then — widen it")


def test_next_blackout_end_drop_window_crosses_midnight():
    end = next_blackout_end(_at(0, "23:55"))  # Mon 23:55 -> Tue 00:07
    assert (end.hour, end.minute) == (0, 7)
    assert end.date() == dt.date(2026, 7, 7)  # rolled to the next day
    end2 = next_blackout_end(_at(1, "00:03"))  # Tue 00:03 -> Tue 00:07 (same day)
    assert (end2.hour, end2.minute) == (0, 7)
    assert end2.date() == dt.date(2026, 7, 7)
