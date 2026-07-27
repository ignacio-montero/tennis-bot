"""Calendar mode wired end-to-end through the drop and the catcher, driven by a
FAKE `.ics` fetch — no browser, no network (ARCHITECTURE §9).

The money path: (a) a calendar event books the right window; (b) an unreadable
calendar books NOTHING and alerts LOUD; (c) rules mode is byte-for-byte today's
ordering. The rules-mode drop parity is already covered in test_runner_logic.py;
here we add the calendar paths + a direct match_candidates parity assertion.
"""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.calendar_source import CalendarReadError
from tennisbot.catcher import WeekSlot, match_candidates, run_catcher_loop
from tennisbot.models import RunResult, Slot
from tennisbot.prefs import Prefs, Rule
from tennisbot.window_source import RulesSource

LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _calendar_url(monkeypatch):
    # A configured (dummy) URL for every test here: the fetch is FAKED to avoid
    # the network, but the URL still legitimately comes from the env (§9.3), so
    # CalendarSource needs one to hand the injected fetch. The URL-unset failure
    # is covered separately in test_window_source.py.
    monkeypatch.setenv("TENNISBOT_CALENDAR_ICS_URL", "https://example/cal.ics")


def _at(iso):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


def _ics(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            + "".join(events) + "END:VCALENDAR\r\n")


def _ev(uid, start, end):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
            "END:VEVENT\r\n")


def _fetch(text):
    def f(url):
        return text
    return f


def _raising_fetch(url):
    raise CalendarReadError("calendar fetch failed: HTTP 503")


# ==========================================================================
# Direct parity: rules-mode ordering is identical with/without the source
# ==========================================================================

def test_match_candidates_rules_mode_identical_with_and_without_source():
    # The catcher's money-path parity: passing a RulesSource must yield EXACTLY
    # the same candidate order as the pre-feature `source=None` path.
    prefs = Prefs(rules=(Rule(("Sun",), "18:00", None),
                         Rule(("Sat",), "10:00", "15:00")))
    slots = {"paddington": [
        WeekSlot("2026-08-01", "10:00", "available", "T"),   # Sat → rule 1
        WeekSlot("2026-08-02", "18:00", "available", "T"),   # Sun eve → rule 0
        WeekSlot("2026-08-01", "16:00", "available", "T"),   # Sat late → no rule
        WeekSlot("2026-08-02", "20:00", "my_booking", "T"),  # held → excluded
    ]}
    now = _at("2026-07-25T09:00:00")
    without = match_candidates(slots, prefs, now)
    with_src = match_candidates(slots, prefs, now, source=RulesSource(prefs))
    assert without == with_src
    # And it is the expected priority order (rule 0 = Sun eve first).
    assert [(c.date, c.time) for c in with_src] == [
        ("2026-08-02", "18:00"), ("2026-08-01", "10:00")]


# ==========================================================================
# Catcher — calendar mode through a fake fetch
# ==========================================================================

class FakeScanner:
    def __init__(self, slots_by_centre=None, bookings=()):
        self.slots_by_centre = slots_by_centre or {}
        self.bookings = list(bookings)
        self.booked = []

    def scan(self, prefs):
        return self.slots_by_centre

    def get_bookings(self):
        return list(self.bookings)

    def book(self, centre, date, time, prefs):
        self.booked.append((centre, date, time))
        return RunResult(ok=True, dry_run=not prefs.catcher_live,
                         message="would book",
                         chosen=Slot(date=date, time=time, court="Court 1",
                                     available=True, selector="#x"))

    def teardown(self):
        pass


class Capture:
    def __init__(self):
        self.sends = []

    def send(self, text):
        self.sends.append(text)

    def send_photo(self, *a, **k):
        pass


def _grid(*specs):
    return {"paddington": [WeekSlot(d, t, st, "T") for (d, t, st) in specs]}


def _run_catcher(tmp_path, scanner, fetch, prefs, now="2026-07-20T08:00:00"):
    from tennisbot.prefs import save_prefs
    save_prefs(prefs, tmp_path)
    tg = Capture()
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at(now), sleep_fn=lambda *a, **k: None,
                     calendar_fetch=fetch)
    return tg


def test_catcher_calendar_event_books_the_matching_window(tmp_path):
    # A free 18:00 slot on Sat 25 Jul is covered by a calendar event 18:00–20:00;
    # a free 10:00 slot the same day is NOT in any event → not booked.
    scanner = FakeScanner(_grid(("2026-07-25", "18:00", "available"),
                                ("2026-07-25", "10:00", "available")))
    fetch = _fetch(_ics(_ev("s", "20260725T180000", "20260725T200000")))
    _run_catcher(tmp_path, scanner, fetch, Prefs(mode="calendar"))
    assert scanner.booked == [("paddington", "2026-07-25", "18:00")]


def test_catcher_calendar_empty_read_is_silent_and_books_nothing(tmp_path):
    scanner = FakeScanner(_grid(("2026-07-25", "18:00", "available")))
    tg = _run_catcher(tmp_path, scanner, _fetch(_ics()), Prefs(mode="calendar"))
    assert scanner.booked == []
    assert tg.sends == []                       # read OK but empty ⇒ silence


def test_catcher_unreadable_calendar_books_nothing_and_alerts_loud(tmp_path):
    scanner = FakeScanner(_grid(("2026-07-25", "18:00", "available")))
    tg = _run_catcher(tmp_path, scanner, _raising_fetch, Prefs(mode="calendar"))
    assert scanner.booked == []                 # NEVER falls back to rules
    assert any("Calendar unreadable" in s for s in tg.sends)


def test_catcher_calendar_alert_fires_at_most_once_per_day(tmp_path):
    from tennisbot.prefs import save_prefs
    save_prefs(Prefs(mode="calendar"), tmp_path)
    scanner = FakeScanner(_grid(("2026-07-25", "18:00", "available")))
    tg = Capture()
    run_catcher_loop(max_cycles=3, notify=False, scanner=scanner, notifier=tg,
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T08:00:00"),
                     sleep_fn=lambda *a, **k: None, calendar_fetch=_raising_fetch)
    alerts = [s for s in tg.sends if "Calendar unreadable" in s]
    assert len(alerts) == 1                     # rate-limited, not once-per-cycle


def test_catcher_rules_mode_ignores_calendar_fetch(tmp_path):
    # mode=rules ⇒ the calendar is never read; a raising fetch must be harmless.
    scanner = FakeScanner(_grid(("2026-07-25", "18:00", "available")))
    _run_catcher(tmp_path, scanner, _raising_fetch, Prefs(mode="rules"))
    assert scanner.booked == [("paddington", "2026-07-25", "18:00")]


# ==========================================================================
# Drop — calendar mode through a fake fetch
# ==========================================================================

def _drop_env(monkeypatch, prefs, fetch):
    import time as _t

    from tennisbot import runner
    monkeypatch.setattr(runner, "load_targets",
                        lambda: {"paddington": _fake_target()})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(runner, "load_prefs", lambda *a, **k: prefs)
    fired = []
    monkeypatch.setattr(runner, "run_drop", lambda **k: fired.append(k))
    monkeypatch.setattr(runner, "_next_drop",
                        lambda *a, **k: (1_000_000_000.0, "2026-08-01"))  # a Sat
    return runner, fired


def _fake_target():
    from tennisbot.config import (CourtConfig, Drop, Preference, Surface,
                                   Target)
    courts = CourtConfig(group="G", surfaces={"Synth": Surface("Synth", "Synth")},
                         preferred="Synth", enabled=["Synth"], two_hours=False)
    return Target(key="paddington", name="Paddington", provider="everyoneactive",
                  site="0", drop=Drop(7, "00:00", "Europe/London"),
                  max_holds_per_run=2, courts=courts,
                  want=[Preference("Sat", "10:00")])


def test_drop_calendar_event_sets_the_window(monkeypatch):
    # A calendar event 10:00–12:00 on the drop date (Sat 1 Aug) hands the drop
    # eff_after=10:00, eff_before=12:00 — the event IS the window.
    fetch = _fetch(_ics(_ev("s", "20260801T100000", "20260801T120000")))
    runner, fired = _drop_env(monkeypatch, Prefs(mode="calendar"), fetch)
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=1,
                         calendar_fetch=fetch)
    assert len(fired) == 1
    assert fired[0]["after_time"] == "10:00"
    assert fired[0]["before_time"] == "12:00"


def test_drop_calendar_all_day_event_books_from_midnight(monkeypatch):
    # An all-day event ⇒ "any time that day": floor 00:00, no ceiling.
    fetch = _fetch(_ics("BEGIN:VEVENT\r\nUID:a\r\n"
                        "DTSTART;VALUE=DATE:20260801\r\n"
                        "DTEND;VALUE=DATE:20260802\r\nEND:VEVENT\r\n"))
    runner, fired = _drop_env(monkeypatch, Prefs(mode="calendar"), fetch)
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=1,
                         calendar_fetch=fetch)
    assert len(fired) == 1
    assert fired[0]["after_time"] == "00:00" and fired[0]["before_time"] is None


def test_drop_calendar_no_event_skips_the_night(monkeypatch):
    # No event on the drop date ⇒ book nothing (like the no-preferred-day skip).
    fetch = _fetch(_ics(_ev("x", "20260803T100000", "20260803T110000")))  # Mon
    runner, fired = _drop_env(monkeypatch, Prefs(mode="calendar"), fetch)
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=1,
                         calendar_fetch=fetch)
    assert fired == []


def test_drop_unreadable_calendar_skips_and_alerts(monkeypatch):
    # A fetch failure ⇒ book NOTHING + a loud alert on the loop notifier.
    from tennisbot import runner
    captured = Capture()
    monkeypatch.setattr(runner, "Telegram", lambda *a, **k: captured)

    class _Secrets:
        telegram_bot_token = "t"
        telegram_chat_id = "c"

    monkeypatch.setattr(runner.Secrets, "from_env", staticmethod(lambda: _Secrets))
    runner2, fired = _drop_env(monkeypatch, Prefs(mode="calendar"),
                               _raising_fetch)
    runner2.run_drop_loop(target_key="paddington", notify=True, max_iters=1)
    assert fired == []
    assert any("Calendar unreadable" in s for s in captured.sends)
