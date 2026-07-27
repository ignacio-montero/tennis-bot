"""iCloud ``.ics`` calendar reader — the calendar booking source's data layer.

Part 1 of the calendar-driven booking feature (ARCHITECTURE §9). Every event in
the owner's dedicated "Tennis" calendar is a booking request whose time range is
the search window for that date; this module turns the published ``.ics``
subscription document into per-date `Window`s the engine already understands.

**Functional core / imperative shell** — the two halves are split so the
interesting logic is pure and unit-testable with no network:

- `fetch_ics(url)` — the ONLY IO (an httpx GET). Raises a typed
  `CalendarReadError` on a non-200 or a network failure, with the URL **redacted**
  from every message (it is a "secret link" — CWE-532, same discipline as
  `telegram_poll`'s token redaction).
- `parse_ics(text, d0, d7, tz)` — PURE. Uses `icalendar` to parse and
  `dateutil` to expand `RRULE` recurrences within ``[d0, d7]`` (icalendar parses
  a recurrence rule but does not expand it), mapping each occurrence to a
  `Window`. Unit-tested against saved `.ics` fixture strings — never the network.

`Window` lives here (the lowest layer that constructs one) and is re-exported by
`window_source.py`, which owns the Strategy abstraction over it.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import httpx
import structlog

log = structlog.get_logger()

LONDON = ZoneInfo("Europe/London")

# The subscription URL is a SECRET (ARCHITECTURE §9.3) — it lives ONLY in the
# box's untracked .env, never in prefs.json (which is Telegram-writable and
# echoed in /status). Read at call time, like every other env secret.
CALENDAR_ICS_URL_ENV = "TENNISBOT_CALENDAR_ICS_URL"

DEFAULT_FETCH_TIMEOUT = 15.0
# Untrusted-input guards (critic C1/W1/W2). The `.ics` is fetched over the network,
# so its content controls our work — and a `try/except` bounds EXCEPTIONS, not TIME
# or MEMORY. A hang, an output explosion, and an OOM-SIGKILL don't raise, so we cap
# each explicitly. A real tennis `.ics` is kilobytes and recurs at most daily.
MAX_ICS_BYTES = 5 * 1024 * 1024        # W1: refuse a giant body (OOM crash-loop)
TOTAL_FETCH_DEADLINE = 30.0            # W2: wall-clock cap vs a slow-drip (slowloris)
_SUBDAILY_FREQ = {"SECONDLY", "MINUTELY", "HOURLY"}   # C1: never legit for booking
_MAX_OCCURRENCES = 366                 # C1 belt-and-braces: 8-day window can't need more

# Weekend-first priority (ARCHITECTURE §9.5): Fri/Sat/Sun rank before weekdays.
# Python's weekday(): Mon=0 … Sun=6, so Fri/Sat/Sun == {4, 5, 6}.
_WEEKEND = {4, 5, 6}


class CalendarReadError(RuntimeError):
    """A calendar could not be READ (fetch non-200, network failure, or an
    unparseable document). The caller must book NOTHING and alert LOUD
    (ARCHITECTURE §9.6) — never fall back to rules or "book everything". Every
    message is URL-redacted, since the URL is a secret (CWE-532)."""


@dataclass(frozen=True)
class Window:
    """One per-date booking window, source-agnostic (ARCHITECTURE §9.1).

    A calendar event "Sat 2 Aug 10:00–12:00" is exactly the same shape as a rule
    `Sat 10:00-15:00` scoped to one date — so both the calendar and the existing
    `/rule`s produce `Window`s and everything downstream (matcher, cap, holds
    ceiling, live gates) consumes them without caring where they came from.

    - `earliest` / `latest` are zero-padded `HH:MM`; `earliest` is INCLUSIVE and
      `latest` EXCLUSIVE, matching `prefs._window_admits`. `None` = unbounded on
      that side (an all-day event → both `None` = "any time that day").
    - `priority_key` orders windows within one source. For the calendar it is
      `(0 if weekend else 1, date, earliest)` (weekend-first, then earliest date,
      then earliest time, §9.5); for the rules source it is the rule index.
    """

    date: str                 # ISO yyyy-mm-dd
    earliest: str | None      # "HH:MM" inclusive floor, or None
    latest: str | None        # "HH:MM" exclusive ceiling, or None
    priority_key: object      # tuple (calendar) | int (rules) — sortable in-source


# --------------------------------------------------------------------------
# Thin fetch — the ONLY IO in this module
# --------------------------------------------------------------------------

def fetch_ics(url: str, *, timeout: float = DEFAULT_FETCH_TIMEOUT,
              client: httpx.Client | None = None,
              max_bytes: int = MAX_ICS_BYTES,
              total_deadline: float = TOTAL_FETCH_DEADLINE) -> str:
    """GET the `.ics` subscription document and return its text.

    The only side-effecting function here. On a non-200, any network failure, an
    over-size body, or a too-slow transfer it raises `CalendarReadError`.

    Hardened against a hostile/pathological feed (critic W1/W2/W3):
    - **Streamed with a hard byte cap** (`max_bytes`): `httpx` imposes no body-size
      limit and `r.text` would materialise the whole response, so a giant `.ics`
      could OOM the container — and the cgroup `mem_limit` OOM-kill is an
      uncatchable SIGKILL that `restart: unless-stopped` turns into a crash-loop.
    - **Wall-clock deadline** (`total_deadline`): `httpx`'s `timeout` is PER
      OPERATION, so a slow-drip server (one byte per near-timeout) can hold a cycle
      open indefinitely; the deadline bounds total transfer time.
    - **URL redacted from EVERY error** (the URL is the one true secret — CWE-532):
      a catch-all runs each message through `_redact`, because `httpx.InvalidURL` /
      `UnsupportedProtocol` are NOT `HTTPError` and would otherwise leak the URL.
    """
    def _redact(text: str) -> str:
        return text.replace(url, "***") if url else text

    owns = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    started = time.monotonic()
    try:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise CalendarReadError(
                        f"calendar too large (> {max_bytes} bytes) — refusing to read")
                if time.monotonic() - started > total_deadline:
                    raise CalendarReadError(
                        f"calendar fetch exceeded {total_deadline:.0f}s — aborting")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except CalendarReadError:
        raise                                   # our own size/deadline signal — pass through
    except httpx.HTTPStatusError as e:
        # Build our own message (no URL) rather than str(e), which embeds it.
        raise CalendarReadError(
            f"calendar fetch failed: HTTP {e.response.status_code}") from None
    except Exception as e:                       # incl. InvalidURL/UnsupportedProtocol (NOT HTTPError)
        raise CalendarReadError(
            f"calendar fetch failed: {_redact(str(e))}") from None
    finally:
        if owns:
            client.close()


# --------------------------------------------------------------------------
# Pure parse — icalendar for structure, dateutil for recurrence
# --------------------------------------------------------------------------

def _priority_key(d: dt.date, earliest: str | None) -> tuple:
    """Weekend-first, then earliest date, then earliest time (§9.5). An all-day
    event (`earliest is None`) sorts as "00:00" so it ranks with the day's start."""
    tier = 0 if d.weekday() in _WEEKEND else 1
    return (tier, d.isoformat(), earliest or "00:00")


def _to_london(value: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """A timed event's start/end in London civil time. A FLOATING (tz-less) time
    is assumed to already be London (§9.4); an aware time is converted."""
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)         # floating ⇒ assume London
    return value.astimezone(tz)


def _timed_window(start: dt.datetime, end: dt.datetime,
                  d0: dt.date, d7: dt.date, tz: ZoneInfo) -> Window | None:
    """Map a timed occurrence to a `Window` on its (London) start date, or None if
    that date falls outside ``[d0, d7]``."""
    ls, le = _to_london(start, tz), _to_london(end, tz)
    d = ls.date()
    if not (d0 <= d <= d7):
        return None
    earliest = ls.strftime("%H:%M")
    # Edge — event crossing midnight (rare, §9.4): map to the START date and drop
    # the ceiling for the rest of that day. `latest=None` (not "24:00", which
    # isn't a valid HH:MM, nor "00:00", which as an EXCLUSIVE bound would admit
    # nothing) means "no upper bound today", the correct reading of "clamp to
    # end-of-day". Also covers an event ending exactly at 00:00 (e.g. 22:00–00:00).
    latest = None if le.date() > ls.date() else le.strftime("%H:%M")
    return Window(d.isoformat(), earliest, latest, _priority_key(d, earliest))


def _allday_window(d: dt.date, d0: dt.date, d7: dt.date) -> Window | None:
    """Map an all-day (DATE-valued DTSTART) event to a full-day window: "any time
    that day" — `earliest=None, latest=None` (§9.4, equivalent to a `<day> any`
    rule)."""
    if not (d0 <= d <= d7):
        return None
    return Window(d.isoformat(), None, None, _priority_key(d, None))


def _allday_windows(start: dt.date, duration: dt.timedelta,
                    d0: dt.date, d7: dt.date) -> list[Window]:
    """A (possibly MULTI-day) all-day event → one full-day window per date it
    spans. DTEND is EXCLUSIVE for DATE-valued events, so a Fri→Mon block covers
    Fri/Sat/Sun. Clamped to [d0, d7]. (Critic S1 — a multi-day block previously
    opened only its start date, silently under-booking Sat/Sun.)"""
    n_days = max(1, duration.days)
    out: list[Window] = []
    for i in range(n_days):
        w = _allday_window(start + dt.timedelta(days=i), d0, d7)
        if w is not None:
            out.append(w)
    return out


def _event_duration(comp, dtstart, all_day: bool) -> dt.timedelta:
    """The occurrence length, from DTEND, else DURATION, else a sane default."""
    dtend_prop = comp.get("DTEND")
    dur_prop = comp.get("DURATION")
    if dtend_prop is not None:
        return dtend_prop.dt - dtstart
    if dur_prop is not None:
        return dur_prop.dt
    return dt.timedelta(days=1) if all_day else dt.timedelta(hours=1)


def _expand_rrule(comp, dtstart, all_day: bool, d0: dt.date, d7: dt.date,
                  tz: ZoneInfo) -> list:
    """Expand a recurring event's occurrences whose START falls in ``[d0, d7]``.

    icalendar parses the `RRULE` but does not expand it, so we hand the rule text
    to `dateutil.rrule` and enumerate within the window. We expand in London civil
    time (timed) or naive midnight (all-day); a minor DST-recurrence edge is
    accepted for the MVP (§9.4). `EXDATE`s are honoured so a deleted occurrence
    isn't booked."""
    from dateutil.rrule import rrulestr, rruleset

    # C1 — algorithmic-complexity DoS guard. `rruleset.between(lo, hi)` does NOT
    # skip-ahead: it generates occurrences FROM the rule's DTSTART forward. A
    # sub-daily rule (`FREQ=SECONDLY`) with an old start forces ~1e8 iterations
    # before reaching the window — an effective hang of the single-threaded loop,
    # which no `try/except` catches (it doesn't raise). A tennis calendar never
    # recurs sub-daily, so treat it as a compromised read: fail LOUD (this
    # CalendarReadError is re-raised by `parse_ics`, not skipped per-event).
    rrule_prop = comp.get("RRULE")
    freqs = {str(f).upper() for f in (rrule_prop.get("FREQ") or [])}
    if freqs & _SUBDAILY_FREQ:
        raise CalendarReadError(
            f"unsupported sub-daily recurrence ({','.join(sorted(freqs))}) — refusing to read")

    if all_day:
        base = dt.datetime(dtstart.year, dtstart.month, dtstart.day)
        lo = dt.datetime.combine(d0, dt.time.min)
        hi = dt.datetime.combine(d7, dt.time.max)
    else:
        base = _to_london(dtstart, tz)
        lo = dt.datetime.combine(d0, dt.time.min, tzinfo=tz)
        hi = dt.datetime.combine(d7, dt.time.max, tzinfo=tz)

    rule_text = rrule_prop.to_ical().decode()
    rset = rruleset()
    rset.rrule(rrulestr(rule_text, dtstart=base))
    for ex in _exdates(comp, all_day, tz):
        rset.exdate(ex)
    occ = list(rset.between(lo, hi, inc=True))
    if len(occ) > _MAX_OCCURRENCES:              # belt-and-braces vs any explosion
        raise CalendarReadError(
            f"recurrence produced too many occurrences ({len(occ)}) — refusing to read")
    return occ


def _exdates(comp, all_day: bool, tz: ZoneInfo) -> list:
    """EXDATE values, normalised to match the rruleset's aware/naive-ness."""
    out: list = []
    ex = comp.get("EXDATE")
    if ex is None:
        return out
    groups = ex if isinstance(ex, list) else [ex]
    for g in groups:
        for d in getattr(g, "dts", []):
            v = d.dt
            if all_day:
                if isinstance(v, dt.datetime):
                    v = v.replace(tzinfo=None)
                else:                            # a date → naive midnight
                    v = dt.datetime(v.year, v.month, v.day)
            else:
                v = _to_london(v, tz)
            out.append(v)
    return out


def _expand_event(comp, d0: dt.date, d7: dt.date, tz: ZoneInfo) -> list[Window]:
    """One VEVENT → the `Window`s it contributes within ``[d0, d7]`` (0, 1, or
    many for a recurring event)."""
    dtstart_prop = comp.get("DTSTART")
    if dtstart_prop is None:
        return []
    dtstart = dtstart_prop.dt
    # All-day == DATE-valued DTSTART (a plain `date`, not a `datetime`).
    all_day = isinstance(dtstart, dt.date) and not isinstance(dtstart, dt.datetime)
    duration = _event_duration(comp, dtstart, all_day)

    windows: list[Window] = []
    if comp.get("RRULE") is not None:
        for occ in _expand_rrule(comp, dtstart, all_day, d0, d7, tz):
            if all_day:
                w = _allday_window(occ.date(), d0, d7)
            else:
                w = _timed_window(occ, occ + duration, d0, d7, tz)
            if w is not None:
                windows.append(w)
    else:
        if all_day:
            windows.extend(_allday_windows(dtstart, duration, d0, d7))
        else:
            w = _timed_window(dtstart, dtstart + duration, d0, d7, tz)
            if w is not None:
                windows.append(w)
    return windows


def parse_ics(ics_text: str, d0: dt.date, d7: dt.date,
              tz: ZoneInfo = LONDON) -> list[Window]:
    """Parse an iCalendar document into per-date `Window`s within ``[d0, d7]``
    (both ends inclusive). PURE — no network, no clock.

    - Timed event → `(date, "HH:MM" start, "HH:MM" end)`, London-converted.
    - All-day event (DATE-valued DTSTART) → `(date, None, None)` = any time.
    - Recurring event → each in-window occurrence, like a one-off (§9.4).
    - A single malformed VEVENT is skipped and logged, never fatal — one bad event
      must not blind the whole read (which would trip the §9.6 loud fail-safe and
      pause all booking).

    Windows come back sorted by `priority_key` (weekend-first), so a caller taking
    `windows_for_date(d)[0]` gets the highest-priority window for that day.
    """
    from icalendar import Calendar
    try:
        cal = Calendar.from_ical(ics_text)
    except Exception as e:
        # A document we cannot parse at all IS a failed read (§9.6): the caller
        # must book nothing and alert, not proceed on an empty list.
        raise CalendarReadError(f"calendar parse failed: {e}") from None

    windows: list[Window] = []
    for comp in cal.walk("VEVENT"):
        try:
            windows.extend(_expand_event(comp, d0, d7, tz))
        except CalendarReadError:
            # A PATHOLOGICAL event (sub-daily/occurrence-explosion, C1) makes the
            # whole read unsafe → fail loud (book nothing), not silently skip.
            raise
        except Exception as e:                   # one merely-malformed VEVENT ≠ a dead read
            log.warning("calendar.bad_event_skipped", error=str(e))
            continue
    windows.sort(key=lambda w: w.priority_key)
    return windows
