"""Loop-level tests for the catcher — offline, with a FAKE scanner (no browser,
no network). Exercises scheduling, blackout skip, crash-resilience, booking +
state persistence, the once-per-day heartbeat, and the once-per-week cap notice.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

from tennisbot.catcher import load_state, run_catcher_loop, slot_key
from tennisbot.models import RunResult, Slot
from tennisbot.prefs import Prefs, save_prefs

LONDON = ZoneInfo("Europe/London")


def _at(iso: str):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


class CapturingNotifier:
    def __init__(self):
        self.sends = []
        self.photos = []

    def send(self, text):
        self.sends.append(text)

    def send_photo(self, path, caption=None):
        self.photos.append((path, caption))


class FakeScanner:
    """Stands in for `_PlaywrightScanner`: returns canned grid + bookings and
    records what it was asked to book. Never touches a browser."""

    def __init__(self, slots_by_centre=None, bookings=(), raise_on_scan=False):
        self.slots_by_centre = slots_by_centre or {}
        self.bookings = list(bookings)
        self.raise_on_scan = raise_on_scan
        self.scanned = 0
        self.booked = []
        self.torn = 0

    def scan(self, prefs):
        self.scanned += 1
        if self.raise_on_scan:
            raise RuntimeError("scan boom")
        return self.slots_by_centre

    def get_bookings(self):
        return list(self.bookings)

    def book(self, centre, date, time, prefs):
        self.booked.append((centre, date, time))
        return RunResult(ok=True, dry_run=not prefs.catcher_live, message="would book",
                         chosen=Slot(date=date, time=time, court="Court 1",
                                     available=True, selector="#x"),
                         screenshot_path=None)

    def teardown(self):
        self.torn += 1


def _slots(*specs):
    from tennisbot.catcher import WeekSlot
    return {"paddington": [WeekSlot(d, t, st, "T") for (d, t, st) in specs]}


# --------------------------------------------------------------------------

def test_books_a_matched_cancellation_and_persists_memory(tmp_path):
    # Fresh prefs dir ⇒ defaults (paddington, any day, cap 3, dry-run).
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T08:00:00"),   # pre-heartbeat
                     sleep_fn=lambda *_a, **_k: None)

    assert scanner.booked == [("paddington", "2026-07-25", "18:00")]
    assert any("Would book" in s for s in tg.sends)         # dry-run notice
    # per-slot memory persisted for the lapsed-hold rule
    state = load_state(tmp_path)
    assert slot_key("paddington", "2026-07-25", "18:00") in state["slots"]


def test_empty_cycle_is_silent(tmp_path):
    scanner = FakeScanner(_slots())          # nothing available
    tg = CapturingNotifier()
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T08:00:00"),
                     sleep_fn=lambda *_a, **_k: None)
    assert scanner.booked == []
    assert tg.sends == []                     # no per-cycle noise


def test_blackout_skips_the_scan(tmp_path):
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    # 23:50 Mon is inside DROP_BLACKOUT (23:45–00:07) — must not scan/book.
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T23:50:00"),
                     sleep_fn=lambda *_a, **_k: None)
    assert scanner.scanned == 0 and scanner.booked == []


def test_one_bad_cycle_does_not_kill_the_loop(tmp_path):
    scanner = FakeScanner(raise_on_scan=True)
    tg = CapturingNotifier()
    run_catcher_loop(max_cycles=3, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T08:00:00"),
                     sleep_fn=lambda *_a, **_k: None)
    assert scanner.scanned == 3               # kept going through failures
    assert all("cycle failed" in s for s in tg.sends) and len(tg.sends) == 3


def test_daily_heartbeat_fires_once_per_day(tmp_path):
    scanner = FakeScanner(_slots())          # nothing to book → isolate heartbeat
    tg = CapturingNotifier()
    run_catcher_loop(max_cycles=2, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T10:00:00"),   # past 09:00
                     sleep_fn=lambda *_a, **_k: None)
    hearts = [s for s in tg.sends if s.startswith("💓")]
    assert len(hearts) == 1
    assert "DRY-RUN" in hearts[0] and "Paid this week" in hearts[0]


def test_cap_reached_notifies_once_per_week(tmp_path):
    save_prefs(Prefs(centres=("paddington",), weekly_cap=0), tmp_path)  # 0 = pause
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    run_catcher_loop(max_cycles=2, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T08:00:00"),
                     sleep_fn=lambda *_a, **_k: None)
    caps = [s for s in tg.sends if s.startswith("🧢")]
    assert len(caps) == 1 and scanner.booked == []
