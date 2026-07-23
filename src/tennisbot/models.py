"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass


from datetime import date as _date

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Slot:
    """A court slot found in the Connect single-day 'Select Slot' grid."""
    date: str          # ISO date of the searched day, e.g. "2026-07-04"
    time: str          # 24h start, e.g. "18:00"
    court: str         # column header, e.g. "Tennis Court 2"
    available: bool    # green/bookable vs disabled/booked
    selector: str      # Playwright selector to click this exact cell (live mode)

    @property
    def weekday_abbr(self) -> str:
        y, m, d = (int(x) for x in self.date.split("-"))
        return _DOW[_date(y, m, d).weekday()]

    def matches(self, day_abbr: str, time: str) -> bool:
        return self.available and self.weekday_abbr == day_abbr and self.time == time


@dataclass
class RunResult:
    ok: bool
    dry_run: bool
    message: str
    chosen: Slot | None = None
    screenshot_path: str | None = None
    # Why a run found nothing. `row_full` is ambiguous ("not released yet" AND
    # "sold out"), so a failed drop can't be diagnosed from a single read — we
    # carry forward whether we ever got INTO the grid, and how much was in it.
    grid_seen: bool = False      # did we read a real timetable (row not Full)?
    n_avail: int = 0             # most available slots seen on any surface
