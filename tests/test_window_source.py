"""`WindowSource` Strategy tests (ARCHITECTURE §9.1) — no network.

Covers the RulesSource==today parity (priority + drop bounds), the CalendarSource
weekend-first ordering, the `window_source_for` factory, and the three §9.6
read outcomes (OK-with-events / OK-empty-silent / FAILED).
"""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.calendar_source import CALENDAR_ICS_URL_ENV, CalendarReadError
from tennisbot.prefs import Prefs, Rule
from tennisbot.window_source import (CalendarSource, RulesSource,
                                     window_source_for)

D0 = dt.date(2026, 8, 1)
D7 = dt.date(2026, 8, 8)


def _ics(*events: str) -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            + "".join(events) + "END:VCALENDAR\r\n")


def _ev(uid, start, end):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
            "END:VEVENT\r\n")


def _fake_fetch(text):
    def fetch(url):
        return text
    return fetch


# ==========================================================================
# RulesSource — the parity guarantee
# ==========================================================================

def test_rules_source_priority_matches_prefs_rule_priority_exactly():
    # RulesSource.priority_for MUST equal Prefs.rule_priority for every slot —
    # that identity is what makes rules-mode ordering byte-for-byte unchanged.
    prefs = Prefs(rules=(Rule(("Sun",), "18:00", None),
                         Rule(("Sat",), "10:00", "15:00")))
    src = RulesSource(prefs)
    for date, time in [("2026-08-02", "18:00"), ("2026-08-01", "10:00"),
                       ("2026-08-01", "16:00"), ("2026-08-03", "09:00")]:
        assert src.priority_for(date, time) == prefs.rule_priority(date, time)


def test_rules_source_windows_for_date_matches_the_drop_branch():
    # non-empty rules, matching day → that rule's floor/ceiling.
    prefs = Prefs(rules=(Rule(("Sat",), "10:00", "12:00"),))
    [w] = RulesSource(prefs).windows_for_date("2026-08-01")   # a Saturday
    assert w.earliest == "10:00" and w.latest == "12:00"


def test_rules_source_skips_a_non_matching_day():
    prefs = Prefs(rules=(Rule(("Sat",), "10:00", "12:00"),))
    assert RulesSource(prefs).windows_for_date("2026-08-03") == []   # a Monday


def test_rules_source_empty_rules_carries_cli_after_time():
    # No rules ⇒ one window carrying the CLI --after (the pre-feature fallback).
    src = RulesSource(Prefs(), after_time="19:00")
    [w] = src.windows_for_date("2026-08-03")
    assert w.earliest == "19:00" and w.latest is None


def test_rules_source_never_read_fails():
    assert RulesSource(Prefs()).read_failed is False


# ==========================================================================
# CalendarSource — weekend-first ordering + read outcomes
# ==========================================================================

def _cal(prefs, text, **kw):
    return CalendarSource(prefs, fetch=_fake_fetch(text), url="http://x",
                          d0=D0, d7=D7, **kw)


def test_calendar_source_orders_weekend_first_then_date_then_time():
    # Mon weekday event + Sun & Fri weekend events → weekend tier first.
    text = _ics(_ev("mon", "20260803T180000", "20260803T190000"),
                _ev("sun", "20260802T090000", "20260802T100000"),
                _ev("fri", "20260807T100000", "20260807T110000"))
    src = _cal(Prefs(), text)
    got = src.all_windows(D0, D7)
    assert [w.date for w in got] == ["2026-08-02", "2026-08-07", "2026-08-03"]
    assert src.read_failed is False


def test_calendar_source_priority_for_admits_only_windowed_times():
    text = _ics(_ev("s", "20260802T100000", "20260802T120000"))   # Sun 10–12
    src = _cal(Prefs(), text)
    assert src.priority_for("2026-08-02", "10:00") is not None      # inclusive
    assert src.priority_for("2026-08-02", "12:00") is None          # exclusive
    assert src.priority_for("2026-08-02", "09:00") is None          # before
    assert src.priority_for("2026-08-03", "10:00") is None          # wrong date


def test_calendar_source_windows_for_date_highest_priority_first():
    text = _ics(_ev("a", "20260802T140000", "20260802T150000"),
                _ev("b", "20260802T090000", "20260802T100000"))
    src = _cal(Prefs(), text)
    got = src.windows_for_date("2026-08-02")
    assert [w.earliest for w in got] == ["09:00", "14:00"]     # earliest first


def test_read_ok_but_empty_is_silent_not_failed():
    # §9.6: read OK with zero events ⇒ no windows AND read_failed False (silent).
    src = _cal(Prefs(), _ics())
    assert src.all_windows(D0, D7) == [] and src.read_failed is False


def test_fetch_failure_is_a_read_failure():
    def boom(url):
        raise CalendarReadError("calendar fetch failed: HTTP 500")
    src = CalendarSource(Prefs(), fetch=boom, url="http://x", d0=D0, d7=D7)
    assert src.read_failed is True and "500" in src.failure_reason


def test_unparseable_document_is_a_read_failure():
    src = _cal(Prefs(), "not ical at all")
    assert src.read_failed is True


def test_missing_url_env_in_calendar_mode_is_a_read_failure(monkeypatch):
    # §9.6: mode=calendar but the URL secret unset ⇒ FAILED read (loud), not empty.
    monkeypatch.delenv(CALENDAR_ICS_URL_ENV, raising=False)
    src = CalendarSource(Prefs(), fetch=_fake_fetch(_ics()), url=None,
                         d0=D0, d7=D7)
    assert src.read_failed is True
    assert CALENDAR_ICS_URL_ENV in src.failure_reason


def test_url_read_from_env_when_not_passed(monkeypatch):
    monkeypatch.setenv(CALENDAR_ICS_URL_ENV, "http://from-env")
    seen = {}
    def fetch(url):
        seen["url"] = url
        return _ics(_ev("s", "20260802T100000", "20260802T120000"))
    src = CalendarSource(Prefs(), fetch=fetch, url=None, d0=D0, d7=D7)
    assert seen["url"] == "http://from-env" and src.read_failed is False


# ==========================================================================
# The factory
# ==========================================================================

def test_factory_picks_rules_by_default():
    assert isinstance(window_source_for(Prefs()), RulesSource)


def test_factory_picks_calendar_when_mode_is_calendar():
    src = window_source_for(Prefs(mode="calendar"),
                            fetch=_fake_fetch(_ics()), url="http://x",
                            d0=D0, d7=D7)
    assert isinstance(src, CalendarSource)
