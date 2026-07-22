"""Deterministic tests for slot-selection logic (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.config import (CourtConfig, Drop, Preference, Surface, Target)
from tennisbot.models import Slot
from tennisbot.runner import choose_court_slots


def _target(two_hours: bool) -> Target:
    courts = CourtConfig(
        group="G", surfaces={"Synth": Surface("Synth", "Synth")},
        preferred="Synth", enabled=["Synth"], two_hours=two_hours)
    return Target(key="t", name="T", provider="everyoneactive", site="0",
                  drop=Drop(7, "21:45", "Europe/London"), max_holds_per_run=2,
                  courts=courts, want=[Preference("Wed", "18:00")])


def _slot(time, court, avail=True):
    # 2026-07-01 is a Wednesday.
    return Slot(date="2026-07-01", time=time, court=court,
                available=avail, selector=f"#{court}-{time}")


def test_single_hour_picks_preferred_time():
    slots = [_slot("18:00", "Court 1"), _slot("19:00", "Court 2")]
    chosen = choose_court_slots(slots, _target(False), want_time=None)
    assert len(chosen) == 1 and chosen[0].time == "18:00"


def test_two_hours_same_court():
    slots = [_slot("18:00", "Court 1"), _slot("19:00", "Court 1"),
             _slot("19:00", "Court 2")]
    chosen = choose_court_slots(slots, _target(True), want_time=None)
    assert [s.time for s in chosen] == ["18:00", "19:00"]
    assert chosen[0].court == chosen[1].court == "Court 1"


def test_two_hours_falls_back_to_single_when_no_adjacent():
    # 18:00 exists but no 19:00 on the SAME court -> book single hour.
    slots = [_slot("18:00", "Court 1"), _slot("19:00", "Court 2")]
    chosen = choose_court_slots(slots, _target(True), want_time=None)
    assert len(chosen) == 1 and chosen[0].time == "18:00"


def test_two_hours_requires_same_court_not_cross_court():
    # 18:00 on Court 1, 19:00 only on Court 2 -> must NOT pair across courts.
    slots = [_slot("18:00", "Court 1"), _slot("19:00", "Court 2")]
    chosen = choose_court_slots(slots, _target(True), want_time=None)
    assert not (len(chosen) == 2)


def test_want_time_override():
    slots = [_slot("20:00", "Court 5")]
    chosen = choose_court_slots(slots, _target(False), want_time="20:00")
    assert len(chosen) == 1 and chosen[0].time == "20:00"


def test_no_match_returns_empty():
    slots = [_slot("08:00", "Court 1", avail=True)]
    chosen = choose_court_slots(slots, _target(False), want_time=None)
    assert chosen == []


# ── after_time (dry-run rehearsal: "any court at/after HH:MM") ──────────────
def test_after_time_picks_earliest_at_or_after():
    slots = [_slot("18:00", "Court 1"), _slot("19:00", "Court 2"),
             _slot("20:00", "Court 3")]
    chosen = choose_court_slots(slots, _target(False), want_time=None,
                                after_time="19:00")
    assert len(chosen) == 1 and chosen[0].time == "19:00"  # 18:00 excluded


def test_after_time_skips_earlier_and_taken_slots():
    # 18:00 is before the cutoff; 19:00 exists but is taken -> earliest is 20:00.
    slots = [_slot("18:00", "Court 1"), _slot("19:00", "Court 2", avail=False),
             _slot("20:00", "Court 3")]
    chosen = choose_court_slots(slots, _target(False), want_time=None,
                                after_time="19:00")
    assert len(chosen) == 1 and chosen[0].time == "20:00"


def test_after_time_none_available_returns_empty():
    slots = [_slot("18:00", "Court 1")]  # only before the cutoff
    chosen = choose_court_slots(slots, _target(False), want_time=None,
                                after_time="19:00")
    assert chosen == []


# ── drop-loop sidecar (self-scheduling trigger) ─────────────────────────────
def test_drop_loop_advances_one_night_and_never_refires(monkeypatch):
    # The sidecar must book once per night and always schedule strictly forward
    # (a re-fire would hammer EA at the same instant). Reuses runner._next_drop.
    import time as _t
    from tennisbot import runner
    monkeypatch.setattr(runner, "load_targets", lambda: {"paddington": _target(False)})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    fired = []
    monkeypatch.setattr(runner, "run_drop", lambda **k: fired.append(k))
    scheduled = []
    real = runner._next_drop
    monkeypatch.setattr(runner, "_next_drop",
                        lambda *a, **k: scheduled.append(real(*a, **k)) or scheduled[-1])

    runner.run_drop_loop(target_key="paddington", max_iters=3, notify=False)

    assert len(fired) == 3
    dates = [target_date for (_instant, target_date) in scheduled]
    assert dates[0] < dates[1] < dates[2]        # strictly forward, no re-fire


def test_drop_loop_survives_a_failing_night(monkeypatch):
    # A raised booking must be caught so the loop keeps scheduling later nights.
    import time as _t
    from tennisbot import runner
    monkeypatch.setattr(runner, "load_targets", lambda: {"paddington": _target(False)})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    calls = {"n": 0}
    def boom(**k):
        calls["n"] += 1
        raise RuntimeError("EA login flaked")
    monkeypatch.setattr(runner, "run_drop", boom)
    runner.run_drop_loop(target_key="paddington", max_iters=2, notify=False)
    assert calls["n"] == 2          # did not abort after the first failure
