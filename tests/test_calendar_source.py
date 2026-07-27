"""Pure `.ics` parser + thin-fetch tests (ARCHITECTURE §9.4) — NO network.

`parse_ics` is exercised against saved fixture strings (timed, all-day,
recurring, multi-event across the week, a midnight-crosser, a malformed event);
`fetch_ics` is exercised with a fake httpx transport so the URL-redaction and
typed-error contract are covered without a real GET.
"""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.calendar_source import (CalendarReadError, Window, fetch_ics,
                                       parse_ics)

LONDON = ZoneInfo("Europe/London")
D0 = dt.date(2026, 8, 1)          # Sat
D7 = dt.date(2026, 8, 8)          # Sat (a full week)
D14 = dt.date(2026, 8, 15)


def _ics(*events: str) -> str:
    body = "".join(events)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            + body + "END:VCALENDAR\r\n")


def _vevent(uid: str, *lines: str) -> str:
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\n" + "".join(l + "\r\n" for l in lines)
            + "END:VEVENT\r\n")


# ==========================================================================
# parse_ics — the pure core
# ==========================================================================

def test_timed_event_maps_to_start_date_and_hhmm_window():
    # 2026-08-02 is a Sunday; a floating (tz-less) time is assumed London (§9.4).
    ics = _ics(_vevent("1", "DTSTART:20260802T100000", "DTEND:20260802T120000",
                       "SUMMARY:Tennis"))
    [w] = parse_ics(ics, D0, D7)
    assert w == Window("2026-08-02", "10:00", "12:00", (0, "2026-08-02", "10:00"))


def test_all_day_event_is_any_time_that_day():
    # DATE-valued DTSTART ⇒ full-day window (earliest=None, latest=None).
    ics = _ics(_vevent("2", "DTSTART;VALUE=DATE:20260803",
                       "DTEND;VALUE=DATE:20260804", "SUMMARY:All day"))
    [w] = parse_ics(ics, D0, D7)
    assert w.date == "2026-08-03" and w.earliest is None and w.latest is None


def test_recurring_event_expands_each_occurrence_in_window():
    # Weekly from Mon 03 Aug 19:00; a two-week window catches TWO occurrences.
    ics = _ics(_vevent("3", "DTSTART:20260803T190000", "DTEND:20260803T200000",
                       "RRULE:FREQ=WEEKLY;COUNT=5", "SUMMARY:Weekly"))
    got = parse_ics(ics, D0, D14)
    assert [w.date for w in got] == ["2026-08-03", "2026-08-10"]
    assert all(w.earliest == "19:00" and w.latest == "20:00" for w in got)


def test_recurrence_honours_exdate():
    # Same weekly rule, but the 10 Aug occurrence is deleted via EXDATE.
    ics = _ics(_vevent("3", "DTSTART:20260803T190000", "DTEND:20260803T200000",
                       "RRULE:FREQ=WEEKLY;COUNT=5",
                       "EXDATE:20260810T190000", "SUMMARY:Weekly"))
    got = parse_ics(ics, D0, D14)
    assert [w.date for w in got] == ["2026-08-03"]      # 10 Aug excluded


def test_multi_event_across_the_week_all_parsed_and_sorted_weekend_first():
    # Three events; result comes back weekend-first, then earliest date/time.
    ics = _ics(
        _vevent("a", "DTSTART:20260803T180000", "DTEND:20260803T190000"),  # Mon
        _vevent("b", "DTSTART:20260802T090000", "DTEND:20260802T100000"),  # Sun
        _vevent("c", "DTSTART:20260807T100000", "DTEND:20260807T110000"),  # Fri
    )
    got = parse_ics(ics, D0, D7)
    # Sun(2) & Fri(7) are weekend tier 0 (earliest date first → Sun, then Fri);
    # Mon(3) is weekday tier 1 and sorts last despite the earlier date.
    assert [w.date for w in got] == ["2026-08-02", "2026-08-07", "2026-08-03"]


def test_midnight_crosser_maps_to_start_date_and_drops_the_ceiling():
    # 07 Aug 23:00 → 08 Aug 01:00: mapped to the START date, latest clamped to
    # "end of day" (represented as None = no ceiling for the rest of that day).
    ics = _ics(_vevent("4", "DTSTART:20260807T230000", "DTEND:20260808T010000"))
    [w] = parse_ics(ics, D0, D7)
    assert w.date == "2026-08-07" and w.earliest == "23:00" and w.latest is None


def test_timezone_conversion_utc_to_london():
    # 10:00Z in August is 11:00 London (BST, +1) — the bot compares in London.
    ics = _ics(_vevent("7", "DTSTART:20260802T100000Z", "DTEND:20260802T110000Z"))
    [w] = parse_ics(ics, D0, D7)
    assert w.earliest == "11:00" and w.latest == "12:00"


def test_events_outside_the_window_are_dropped():
    ics = _ics(_vevent("x", "DTSTART:20260731T100000", "DTEND:20260731T110000"),
               _vevent("y", "DTSTART:20260901T100000", "DTEND:20260901T110000"))
    assert parse_ics(ics, D0, D7) == []


def test_a_malformed_event_is_skipped_not_fatal():
    # A mixed date/datetime DTEND makes the duration subtraction raise — that ONE
    # event must be skipped while the good event still parses (one bad VEVENT ≠ a
    # dead read, which would otherwise trip the loud fail-safe and pause booking).
    good = _vevent("g", "DTSTART:20260802T100000", "DTEND:20260802T120000")
    bad = _vevent("b", "DTSTART:20260803T100000", "DTEND;VALUE=DATE:20260804")
    got = parse_ics(_ics(bad, good), D0, D7)
    assert [w.date for w in got] == ["2026-08-02"]


def test_event_missing_dtstart_is_skipped():
    ics = _ics(_vevent("n", "SUMMARY:no start"),
               _vevent("g", "DTSTART:20260802T100000", "DTEND:20260802T120000"))
    assert [w.date for w in parse_ics(ics, D0, D7)] == ["2026-08-02"]


def test_unparseable_document_raises_read_error():
    # A document we cannot parse at all is a FAILED read (§9.6), not "empty".
    with pytest.raises(CalendarReadError):
        parse_ics("this is not iCalendar", D0, D7)


# ==========================================================================
# fetch_ics — the thin (and only) IO edge
# ==========================================================================

_SECRET_URL = "https://p12-caldav.icloud.com/published/2/SECRETTOKEN123"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_returns_body_on_200():
    def handler(req):
        return httpx.Response(200, text="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    body = fetch_ics(_SECRET_URL, client=_client(handler))
    assert "VCALENDAR" in body


def test_fetch_non_200_raises_read_error_without_leaking_url():
    def handler(req):
        return httpx.Response(404, text="nope")
    with pytest.raises(CalendarReadError) as ei:
        fetch_ics(_SECRET_URL, client=_client(handler))
    assert "404" in str(ei.value)
    assert "SECRETTOKEN123" not in str(ei.value)       # CWE-532: URL is a secret


def test_fetch_network_error_redacts_the_url():
    def handler(req):
        raise httpx.ConnectError(f"failed connecting to {_SECRET_URL}")
    with pytest.raises(CalendarReadError) as ei:
        fetch_ics(_SECRET_URL, client=_client(handler))
    assert "SECRETTOKEN123" not in str(ei.value)
    assert "***" in str(ei.value)


# ==========================================================================
# Untrusted-input hardening (critic C1/W1/W2/W3, S1) — a try/except bounds
# EXCEPTIONS, not time/memory; these cap the ones that don't raise.
# ==========================================================================

def test_subdaily_recurrence_is_refused_loud_not_skipped():
    # C1: FREQ=SECONDLY would make dateutil.between() iterate ~1e8 times from an
    # old DTSTART — an effective hang (no exception, so try/except can't save us).
    # A tennis calendar never recurs sub-daily, so it's a COMPROMISED read →
    # CalendarReadError (whole read fails LOUD), not a silent per-event skip. The
    # guard is checked BEFORE any expansion, so this test cannot itself hang.
    good = _vevent("g", "DTSTART:20260802T100000", "DTEND:20260802T110000")
    evil = _vevent("e", "DTSTART:20200101T000000", "DTEND:20200101T000001",
                   "RRULE:FREQ=SECONDLY")
    with pytest.raises(CalendarReadError):
        parse_ics(_ics(good, evil), D0, D7)


@pytest.mark.parametrize("freq", ["SECONDLY", "MINUTELY", "HOURLY"])
def test_all_subdaily_frequencies_are_refused(freq):
    ics = _ics(_vevent("s", "DTSTART:20260801T100000", "DTEND:20260801T101000",
                       f"RRULE:FREQ={freq}"))
    with pytest.raises(CalendarReadError):
        parse_ics(ics, D0, D7)


def test_daily_recurrence_still_allowed():
    # The guard must not over-reach: DAILY is a legitimate booking cadence.
    ics = _ics(_vevent("d", "DTSTART:20260801T100000", "DTEND:20260801T110000",
                       "RRULE:FREQ=DAILY;COUNT=3"))
    got = parse_ics(ics, D0, D7)
    assert [w.date for w in got] == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_fetch_refuses_an_oversize_body():
    # W1: httpx has no body-size limit and r.text would materialise the whole
    # response → OOM → SIGKILL → crash-loop. The stream + byte cap must abort.
    def handler(req):
        return httpx.Response(200, text="y" * 5000)
    with pytest.raises(CalendarReadError) as ei:
        fetch_ics(_SECRET_URL, client=_client(handler), max_bytes=1000)
    assert "too large" in str(ei.value)
    assert "SECRETTOKEN123" not in str(ei.value)      # still redacted


def test_fetch_aborts_on_slow_transfer(monkeypatch):
    # W2: httpx timeout is PER-OPERATION, so a slow-drip can hold a cycle open past
    # it. The wall-clock deadline must trip. Simulate the clock jumping past it.
    import tennisbot.calendar_source as cs
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0   # start=0, every later check=100
    monkeypatch.setattr(cs.time, "monotonic", fake_monotonic)

    def handler(req):
        return httpx.Response(200, text="a" * 50)
    with pytest.raises(CalendarReadError) as ei:
        fetch_ics(_SECRET_URL, client=_client(handler), total_deadline=30.0)
    assert "exceeded" in str(ei.value)


def test_fetch_non_httperror_still_redacts_url():
    # W3: httpx.InvalidURL / UnsupportedProtocol are NOT HTTPError, so they'd skip
    # the typed except blocks. The catch-all must still redact the secret URL.
    class BoomClient:
        def stream(self, method, url):
            raise ValueError(f"boom involving {_SECRET_URL}")   # not an httpx.HTTPError
        def close(self):
            pass
    with pytest.raises(CalendarReadError) as ei:
        fetch_ics(_SECRET_URL, client=BoomClient())
    assert "SECRETTOKEN123" not in str(ei.value)
    assert "***" in str(ei.value)


def test_multiday_all_day_event_opens_every_date_it_spans():
    # S1: DTSTART Sat 01, DTEND Tue 04 (EXCLUSIVE for DATE values) ⇒ Sat/Sun/Mon
    # full-day windows — previously only the start date opened (silent under-book).
    ics = _ics(_vevent("m", "DTSTART;VALUE=DATE:20260801",
                       "DTEND;VALUE=DATE:20260804", "SUMMARY:Away weekend"))
    got = parse_ics(ics, D0, D7)
    assert [w.date for w in got] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert all(w.earliest is None and w.latest is None for w in got)
