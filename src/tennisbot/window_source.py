"""`WindowSource` — the Strategy abstraction over "where do booking windows come
from" (ARCHITECTURE §9.1).

A calendar event and a `/rule` are the same shape once scoped to a date: a
per-date `(earliest, latest)` window. So the calendar feature is NOT a second
booking engine — it is a **second producer of the same windows**, behind one
interface both the drop and the catcher consume. Everything downstream (the
matcher, the weekly cap, the `max_holds` ceiling, the live gates, dry-run) is
unchanged; it never learns where a window came from.

Two implementations, chosen by `prefs.mode` (`window_source_for`):

- **`RulesSource`** — delegates to the EXISTING `prefs` methods
  (`allows_date` / `earliest_for_date` / `latest_for_date` / `rule_priority`), so
  in rules mode the drop and the catcher compute **exactly** what they do today.
  This is the parity guarantee: rules mode is byte-for-byte unchanged because the
  same code produces its windows.
- **`CalendarSource`** — wraps `calendar_source.parse_ics` output; priority is
  weekend-first (§9.5). Distinguishes read-OK-but-empty (book nothing, silent)
  from read-FAILED (book nothing, alert loud) via `read_failed`.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import structlog

from .calendar_source import (CALENDAR_ICS_URL_ENV, CalendarReadError, Window,
                              fetch_ics, parse_ics)
from .prefs import _norm_hhmm, _window_admits

log = structlog.get_logger()

LONDON = ZoneInfo("Europe/London")

__all__ = ["Window", "RulesSource", "CalendarSource", "window_source_for",
           "CALENDAR_ICS_URL_ENV"]


class RulesSource:
    """The `/rule`-driven window source — a thin façade over today's `prefs`
    logic, so rules mode is unchanged.

    `after_time` is the CLI `--after` fallback the drop uses only when NO rule is
    configured (an empty `rules` list). Keeping the "a set rule wins, else the CLI
    arg" precedence INSIDE this source is what lets the drop loop consult one
    uniform interface without changing its behaviour.
    """

    read_failed = False            # rules can never be "unreadable"
    failure_reason = None

    def __init__(self, prefs, *, after_time: str | None = None):
        self.prefs = prefs
        self.centres = prefs.centres
        self.after_time = after_time

    def priority_for(self, date: str, time: str):
        """Priority of the highest-priority rule admitting (date, time), or None.
        Delegates to `Prefs.rule_priority` — the SAME computation the catcher's
        matcher does today, which is what keeps rules-mode ordering identical."""
        return self.prefs.rule_priority(date, time)

    def windows_for_date(self, date: str) -> list[Window]:
        """The drop's single window for one date. Reproduces run_drop_loop's
        original branch exactly:

        - rules configured & this weekday matches a rule → that rule's window
          (day-matched by `allows_date`, floor/ceiling from the top matching rule);
        - rules configured & no rule covers this weekday → `[]` (skip the night);
        - no rules → one window carrying the CLI `--after` (which may be None →
          "ranked prefs", the exact pre-feature fallback).
        """
        p = self.prefs
        if p.rules:
            if not p.allows_date(date):
                return []
            return [Window(date, p.earliest_for_date(date),
                           p.latest_for_date(date), 0)]
        return [Window(date, self.after_time, None, 0)]

    def all_windows(self, d0: dt.date, d7: dt.date) -> list[Window]:
        """The range view (interface completeness). The catcher actually ranks via
        `priority_for` over the AVAILABLE grid cells — a rule is a predicate over
        every matching weekday, not a finite per-date list — but enumerating the
        per-date windows here keeps the two producers interchangeable."""
        out: list[Window] = []
        cur = d0
        while cur <= d7:
            out.extend(self.windows_for_date(cur.isoformat()))
            cur += dt.timedelta(days=1)
        return out


class CalendarSource:
    """The calendar-driven window source: the parsed `.ics` (ARCHITECTURE §9).

    Built once per cycle (calendar read fresh, §9.7). It fetches + parses eagerly
    in `__init__` and then answers from memory, so the three §9.6 outcomes are
    settled up front and visible to the caller:

    - **read OK, events present** → windows; book normally.
    - **read OK, zero events** → no windows, `read_failed=False` → book nothing,
      SILENTLY.
    - **read FAILED** (fetch error, parse error, or the URL env unset in calendar
      mode) → no windows, `read_failed=True` → book nothing AND the caller alerts
      LOUD. Never a fallback to rules or "book everything".

    `fetch` is injected (default `fetch_ics`) exactly like the catcher injects its
    scanner, so tests drive calendar mode with a fake fetch and never hit the
    network.
    """

    def __init__(self, prefs, *, fetch=None, url: str | None = None,
                 d0: dt.date | None = None, d7: dt.date | None = None,
                 tz: ZoneInfo = LONDON, now: dt.datetime | None = None):
        self.prefs = prefs
        self.centres = prefs.centres        # WHERE still comes from prefs (§9.5)
        self.read_failed = False
        self.failure_reason: str | None = None
        self._windows: list[Window] = []
        self._by_date: dict[str, list[Window]] = {}

        now = now or dt.datetime.now(tz)
        d0 = d0 or now.date()
        d7 = d7 or (d0 + dt.timedelta(days=7))

        # The URL is a secret read at call time from the env, NEVER from prefs
        # (§9.3). Missing in calendar mode is a READ FAILURE, not "book nothing
        # quietly" — the owner asked for calendar mode but we can't honour it.
        url = url if url is not None else os.environ.get(
            CALENDAR_ICS_URL_ENV, "").strip()
        if not url:
            self.read_failed = True
            self.failure_reason = f"{CALENDAR_ICS_URL_ENV} is not set"
            return

        fetch = fetch or fetch_ics
        try:
            ics_text = fetch(url)
            self._windows = parse_ics(ics_text, d0, d7, tz=tz)
        except CalendarReadError as e:
            self.read_failed = True
            self.failure_reason = str(e)        # already URL-redacted
            return
        except Exception as e:                  # any surprise = failed read (§9.6)
            self.read_failed = True
            self.failure_reason = f"calendar read failed: {e}"
            return

        for w in self._windows:
            self._by_date.setdefault(w.date, []).append(w)

    def priority_for(self, date: str, time: str):
        """The best (lowest) `priority_key` among calendar windows on `date` that
        admit `time`, or None if none do. Fails closed on an unparseable time,
        same as the rules matcher."""
        t = _norm_hhmm(time)
        if t is None:
            return None
        best = None
        for w in self._by_date.get(date, []):
            if _window_admits(w.earliest, w.latest, t):
                if best is None or w.priority_key < best:
                    best = w.priority_key
        return best

    def windows_for_date(self, date: str) -> list[Window]:
        """Calendar windows on `date`, highest priority first — so the drop takes
        `[0]`."""
        return sorted(self._by_date.get(date, []), key=lambda w: w.priority_key)

    def all_windows(self, d0: dt.date, d7: dt.date) -> list[Window]:
        return list(self._windows)


def window_source_for(prefs, *, after_time: str | None = None, fetch=None,
                      url: str | None = None, d0: dt.date | None = None,
                      d7: dt.date | None = None, now: dt.datetime | None = None,
                      tz: ZoneInfo = LONDON):
    """Pick the window source from `prefs.mode` (§9.1). Default/unknown → rules
    (an unknown mode also degrades prefs → forces dry-run, so it is safe)."""
    if prefs.mode == "calendar":
        return CalendarSource(prefs, fetch=fetch, url=url, d0=d0, d7=d7,
                              now=now, tz=tz)
    return RulesSource(prefs, after_time=after_time)
