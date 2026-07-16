"""Deterministic tests for _next_drop — the drop instant + released date,
including the midnight-rollover case that pre-warming before 00:00 hits."""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.runner import _next_drop

LONDON = ZoneInfo("Europe/London")


def _epoch(y, mo, d, h, mi, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=LONDON).timestamp()


def test_after_midnight_rehearsal_targets_same_day_plus_7():
    # Launched 00:41 for a 00:45 rehearsal: on = today, target = today + 7.
    now = _epoch(2026, 7, 18, 0, 41)
    instant, target_date = _next_drop("00:45", "Europe/London", 7, now_epoch=now)
    assert target_date == "2026-07-25"
    assert instant == _epoch(2026, 7, 18, 0, 45)


def test_premidnight_launch_rolls_over_to_next_day():
    # Pre-warming 23:56 for a 00:00 drop must aim at the NEXT day's 00:00
    # (and that day + 7), not today's already-passed 00:00 / today + 7.
    now = _epoch(2026, 7, 17, 23, 56)
    instant, target_date = _next_drop("00:00", "Europe/London", 7, now_epoch=now)
    assert target_date == "2026-07-25"            # 2026-07-18 + 7
    assert instant == _epoch(2026, 7, 18, 0, 0)


def test_slightly_late_launch_stays_on_the_drop_that_just_fired():
    # Fired 30s after 00:00: within grace -> still today's drop, camp forward.
    now = _epoch(2026, 7, 18, 0, 0, 30)
    instant, target_date = _next_drop("00:00", "Europe/London", 7, now_epoch=now)
    assert target_date == "2026-07-25"
    assert instant == _epoch(2026, 7, 18, 0, 0)


def test_daytime_launch_targets_today():
    # A same-day 22:00-style drop launched in the evening: on = today.
    now = _epoch(2026, 7, 18, 21, 41)
    instant, target_date = _next_drop("22:00", "Europe/London", 7, now_epoch=now)
    assert target_date == "2026-07-25"
    assert instant == _epoch(2026, 7, 18, 22, 0)
