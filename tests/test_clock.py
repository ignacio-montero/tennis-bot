"""Tests for clock timing logic (no network)."""

import datetime as dt
import time

from tennisbot import clock


def test_drop_instant_is_correct_local_time():
    epoch = clock.drop_instant("21:45", "Europe/London", on=dt.date(2026, 7, 5))
    # 2026-07-05 is BST (UTC+1) -> 21:45 local == 20:45 UTC.
    assert dt.datetime.utcfromtimestamp(epoch).strftime("%H:%M") == "20:45"


def test_wait_until_fires_at_target_not_early():
    target = time.time() + 0.5
    clock.wait_until(target, skew=0.0, epsilon=0.0, spin_lead=0.2)
    assert time.time() >= target          # never fires early


def test_wait_until_applies_skew_and_epsilon():
    # If server is 1s BEHIND us (skew -1), we fire 1s later in local time.
    target = time.time() + 0.2
    start = time.time()
    clock.wait_until(target, skew=-1.0, epsilon=0.0, spin_lead=0.2)
    assert time.time() - start >= 1.2 - 0.05
