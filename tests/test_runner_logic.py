"""Deterministic tests for slot-selection logic (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.config import (CourtConfig, Drop, Preference, Surface, Target)
from tennisbot.models import Slot
from tennisbot.runner import choose_court_slots, diagnose_drop_failure


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


# ── drop failure diagnosis ("did the drop move?" vs "did I lose?") ──────────
def test_never_opened_when_grid_never_read():
    # The row stayed Full for the whole camp window => the release did not
    # happen. This is the ONLY code that should prompt re-running the watcher.
    code, reason = diagnose_drop_failure(grid_seen=False, n_avail=0)
    assert code == "never_opened"
    assert "never opened" in reason


def test_sold_out_when_grid_read_but_empty():
    code, reason = diagnose_drop_failure(grid_seen=True, n_avail=0)
    assert code == "sold_out"
    assert "Lost the race" in reason


def test_prefs_too_narrow_names_the_filter():
    # Slots WERE free — this is a config problem, not a race loss. The filter
    # that excluded them must appear, or the message isn't actionable.
    code, reason = diagnose_drop_failure(grid_seen=True, n_avail=4,
                                         after_time="19:00")
    assert code == "prefs_too_narrow"
    assert "4 free slot(s)" in reason and "after 19:00" in reason


def test_want_time_filter_named_when_no_after_time():
    _, reason = diagnose_drop_failure(grid_seen=True, n_avail=2,
                                      want_time="18:00")
    assert "want 18:00" in reason


def test_ranked_prefs_named_when_no_explicit_filter():
    _, reason = diagnose_drop_failure(grid_seen=True, n_avail=2)
    assert "ranked prefs" in reason


def test_zero_avail_never_masquerades_as_sold_out():
    # THE point of the change: row_full is ambiguous, so n_avail==0 alone must
    # never be read as "sold out" — only a grid we actually read can say that.
    unopened, _ = diagnose_drop_failure(grid_seen=False, n_avail=0)
    opened, _ = diagnose_drop_failure(grid_seen=True, n_avail=0)
    assert unopened == "never_opened" and opened == "sold_out"


# ── pre-warm retry + no_observation (2026-07-23 cold-login incident) ────────
def test_lead_min_default_gives_room_for_retries():
    # The lead is the runway for cold logins. A 45s navigation timeout burned
    # the whole 3-min lead on 2026-07-23; the default must now afford several.
    import inspect
    from tennisbot import runner
    lead = inspect.signature(runner.run_drop_loop).parameters["lead_min"].default
    prewarm = inspect.signature(runner.run_drop).parameters["prewarm_attempts"].default
    floor = inspect.signature(runner.run_drop).parameters["prewarm_floor_s"].default
    # Worst observed cold-login attempt was ~92s; require room for them all
    # plus the floor we refuse to encroach on.
    assert lead * 60 >= prewarm * 92 + floor, (
        f"lead {lead}min too short for {prewarm} attempts")


def test_no_observation_is_not_never_opened():
    # A crash before arming says NOTHING about the drop time. Conflating it with
    # `never_opened` would wrongly conclude "the drop moved" — which is exactly
    # what the 2026-07-23 evidence disproved (the drop released 53 slots fine).
    code, _ = diagnose_drop_failure(grid_seen=False, n_avail=0)
    assert code == "never_opened"          # classifier only sees observed runs
    assert code != "no_observation"        # crash path emits that, separately


# ── sprinter reads shared prefs (ARCHITECTURE §8.6) ─────────────────────────
# The drop loop now reads the same prefs.json the catcher does, so ONE config
# governs both jobs. These tests exercise the loop offline: run_drop and
# load_prefs are stubbed (mirroring test_drop_loop_* above), so NO EA/Telegram
# network call is ever made — a live drop fires nightly on the box, so the tests
# must never risk it.

import datetime as _dt

from tennisbot.prefs import Prefs


def _abbr(iso: str) -> str:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
            "Sun"][_dt.date.fromisoformat(iso).weekday()]


def _loop_env(monkeypatch, prefs=None):
    """Wire the loop for offline testing: fake target, no-op sleep, run_drop and
    _next_drop stubbed. Returns the `fired` list capturing run_drop's kwargs.
    If `prefs` is given, load_prefs is stubbed to return it; otherwise the real
    load_prefs runs (used by the backward-compat test with an empty dir)."""
    import time as _t
    from tennisbot import runner
    monkeypatch.setattr(runner, "load_targets",
                        lambda: {"paddington": _target(False)})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    fired = []
    monkeypatch.setattr(runner, "run_drop", lambda **k: fired.append(k))
    if prefs is not None:
        monkeypatch.setattr(runner, "load_prefs", lambda *a, **k: prefs)
    return runner, fired


def _pin_next_drop(monkeypatch, runner, target_date: str):
    """Force _next_drop to a fixed (instant, target_date) so a test controls the
    weekday the drop releases — otherwise it depends on the real 'today'."""
    monkeypatch.setattr(runner, "_next_drop",
                        lambda *a, **k: (1_000_000_000.0, target_date))


def test_drop_loop_default_prefs_identical_firing(monkeypatch, tmp_path):
    # CRITICAL backward-compat: with NO prefs.json present, the loop must behave
    # EXACTLY as before — fire every night, honour the CLI --after and --live,
    # and add no two-hours override. Uses the REAL load_prefs over an empty dir
    # so we exercise the true absent-file → defaults path.
    runner, fired = _loop_env(monkeypatch)          # real load_prefs
    runner.run_drop_loop(target_key="paddington", after_time="19:00",
                         dry_run=True, notify=False, max_iters=3,
                         config_dir=str(tmp_path))
    assert len(fired) == 3                           # never day-filter-skipped
    for k in fired:
        assert k["dry_run"] is True                  # --live absent ⇒ dry-run
        assert k["after_time"] == "19:00"            # CLI --after passed through
        assert k["two_hours"] is None                # config two_hours untouched


def test_drop_loop_default_prefs_live_flag_still_books(monkeypatch, tmp_path):
    # The deploy-level --live must still work when prefs is absent/default.
    runner, fired = _loop_env(monkeypatch)
    runner.run_drop_loop(target_key="paddington", dry_run=False, notify=False,
                         max_iters=1, config_dir=str(tmp_path))
    assert len(fired) == 1 and fired[0]["dry_run"] is False


def test_drop_loop_skips_non_preferred_day(monkeypatch):
    # days=[<not the release weekday>] ⇒ skip the drop that night and reschedule,
    # WITHOUT calling run_drop (i.e. without logging in).
    target_date = "2026-08-04"
    others = tuple(d for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
                   if d != _abbr(target_date))
    runner, fired = _loop_env(monkeypatch, prefs=Prefs(days=others))
    _pin_next_drop(monkeypatch, runner, target_date)
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=2)
    assert fired == []                               # never booked / logged in


def test_drop_loop_fires_on_preferred_day(monkeypatch):
    # days=[<the release weekday>] ⇒ the drop DOES fire.
    target_date = "2026-08-04"
    runner, fired = _loop_env(monkeypatch,
                              prefs=Prefs(days=(_abbr(target_date),)))
    _pin_next_drop(monkeypatch, runner, target_date)
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=1)
    assert len(fired) == 1                            # preferred day ⇒ books


def test_drop_loop_prefs_earliest_overrides_cli_after(monkeypatch):
    # A SET prefs.earliest wins over the CLI --after (precedence rule).
    runner, fired = _loop_env(monkeypatch, prefs=Prefs(earliest="18:00"))
    _pin_next_drop(monkeypatch, runner, "2026-08-04")
    runner.run_drop_loop(target_key="paddington", after_time="20:00",
                         notify=False, max_iters=1)
    assert len(fired) == 1 and fired[0]["after_time"] == "18:00"


def test_drop_loop_slot_length_two_forces_two_hours(monkeypatch):
    # prefs.slot_length_hours==2 ⇒ two_hours override True; ==1 (default) ⇒ None.
    runner, fired = _loop_env(monkeypatch, prefs=Prefs(slot_length_hours=2))
    _pin_next_drop(monkeypatch, runner, "2026-08-04")
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=1)
    assert len(fired) == 1 and fired[0]["two_hours"] is True


def test_drop_loop_prefs_live_flips_to_live(monkeypatch):
    # prefs.live true ⇒ the loop books live even with no CLI --live (dry_run=True).
    runner, fired = _loop_env(monkeypatch, prefs=Prefs(live=True))
    _pin_next_drop(monkeypatch, runner, "2026-08-04")
    runner.run_drop_loop(target_key="paddington", dry_run=True, notify=False,
                         max_iters=1)
    assert len(fired) == 1 and fired[0]["dry_run"] is False


def test_drop_loop_degraded_prefs_forces_dry_run(monkeypatch):
    # A half-read config forces dry-run even when --live is set (§1.4a refuse to
    # book): degraded overrides the deploy-level --live. This is the safety net.
    runner, fired = _loop_env(monkeypatch,
                              prefs=Prefs(degraded=("weekly_cap",), live=False))
    _pin_next_drop(monkeypatch, runner, "2026-08-04")
    runner.run_drop_loop(target_key="paddington", dry_run=False, notify=False,
                         max_iters=1)
    assert len(fired) == 1 and fired[0]["dry_run"] is True


# ── gap-filling: added by the tester to close the live-gate truth table, the
# ── "present-but-default file" backward-compat path, and the end-to-end §1.4a
# ── wire from a real corrupt file on disk (not a hand-built Prefs). ──────────
import json as _json


def test_drop_loop_default_FILE_on_disk_fires_identically(monkeypatch, tmp_path):
    # Backward-compat, the OTHER half: the task requires identical firing with an
    # ALL-DEFAULT prefs.json *present*, not just absent. A present file takes the
    # from_dict() path (the absent case short-circuits on FileNotFoundError), so
    # this proves the parse of a fully-defaulted document is also permissive —
    # no day-skip, after_time/two_hours untouched, dry-run == the CLI flag.
    (tmp_path / "prefs.json").write_text(_json.dumps(Prefs.defaults().to_dict()))
    runner, fired = _loop_env(monkeypatch)           # REAL load_prefs, file present
    runner.run_drop_loop(target_key="paddington", after_time="19:00",
                         dry_run=True, notify=False, max_iters=2,
                         config_dir=str(tmp_path))
    assert len(fired) == 2                            # never skipped
    for k in fired:
        assert k["dry_run"] is True                   # --live absent ⇒ dry-run
        assert k["after_time"] == "19:00"             # CLI --after passed through
        assert k["two_hours"] is None                 # slot_length 1 ⇒ no override


def test_drop_loop_prefs_live_AND_cli_live_both_set_books(monkeypatch):
    # Completes the live-gate truth table: (prefs.live=T, cli --live=T, degraded=F)
    # ⇒ live. The two live sources are OR'd, so both-on must still book.
    runner, fired = _loop_env(monkeypatch, prefs=Prefs(live=True))
    _pin_next_drop(monkeypatch, runner, "2026-08-04")
    runner.run_drop_loop(target_key="paddington", dry_run=False, notify=False,
                         max_iters=1)
    assert len(fired) == 1 and fired[0]["dry_run"] is False


def test_drop_loop_degraded_FILE_on_disk_forces_dry_run_even_with_cli_live(
        monkeypatch, tmp_path):
    # END-TO-END §1.4a (the load-bearing safety net): a real corrupt prefs.json
    # on disk whose own `live:true` we can't trust, plus a deploy-level --live,
    # must STILL fire dry-run. test_drop_loop_degraded_prefs_forces_dry_run stubs
    # load_prefs with a hand-built Prefs; this instead drives the true wire
    # file → load_prefs → from_dict(degraded) → gate, so the guarantee is proven
    # from disk, not from a fabricated object. `weekly_cap:"lots"` is the poison.
    (tmp_path / "prefs.json").write_text('{"weekly_cap": "lots", "live": true}')
    runner, fired = _loop_env(monkeypatch)           # REAL load_prefs
    _pin_next_drop(monkeypatch, runner, "2026-08-04")
    runner.run_drop_loop(target_key="paddington", dry_run=False, notify=False,
                         max_iters=1, config_dir=str(tmp_path))
    assert len(fired) == 1 and fired[0]["dry_run"] is True


def test_drop_loop_empty_days_never_skips_on_any_weekday(monkeypatch):
    # Day-filter backward-compat: empty `days` (the default) ⇒ allows_date always
    # True ⇒ the loop fires on EVERY weekday, including ones a filter might drop.
    # Pin the release to a Saturday to make "no filter" explicit, not incidental.
    sat = "2026-08-01"                                # a Saturday
    assert _abbr(sat) == "Sat"
    runner, fired = _loop_env(monkeypatch, prefs=Prefs(days=()))
    _pin_next_drop(monkeypatch, runner, sat)
    runner.run_drop_loop(target_key="paddington", notify=False, max_iters=1)
    assert len(fired) == 1


# ── W1/S1: the loop's crash-guard and live-gate belt (2026-07-26 review) ────
def test_drop_loop_survives_a_raising_prefs_read(monkeypatch):
    # W1: the WHOLE iteration body is guarded, not just run_drop. If load_prefs
    # ever raised (it's engineered not to, but a future refactor might), the
    # nightly daemon must NOT die — it logs and advances to the next night.
    import time as _t
    from tennisbot import runner
    monkeypatch.setattr(runner, "load_targets", lambda: {"paddington": _target(False)})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(runner, "run_drop", lambda **k: None)
    calls = {"n": 0}
    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("prefs read blew up")
    monkeypatch.setattr(runner, "load_prefs", boom)
    runner.run_drop_loop(target_key="paddington", max_iters=2, notify=False)
    assert calls["n"] == 2          # kept looping through the raise, didn't die


def test_drop_loop_degraded_prefs_vetoes_even_telegram_live(monkeypatch):
    # S1: the gate's `and not prefs.degraded` must override a Telegram-set
    # prefs.live=True, independently of the CLI --live path. from_dict prevents
    # this state from a file, so we hand-build it to test the loop's own belt.
    import time as _t
    from tennisbot import runner
    from tennisbot.prefs import Prefs
    monkeypatch.setattr(runner, "load_targets", lambda: {"paddington": _target(False)})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(runner, "load_prefs",
                        lambda *a, **k: Prefs(degraded=("weekly_cap",), live=True))
    seen = []
    monkeypatch.setattr(runner, "run_drop", lambda **k: seen.append(k["dry_run"]))
    runner.run_drop_loop(target_key="paddington", max_iters=1, notify=False,
                         dry_run=False)          # deploy --live too
    assert seen == [True]           # degraded ⇒ dry-run, despite live=True + --live
