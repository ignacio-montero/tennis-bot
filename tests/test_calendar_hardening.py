"""QA hardening for calendar-driven booking (ARCHITECTURE §9).

Written by the Tester to harden thin spots the feature's own tests left open.
Every test here targets a stated priority of the review brief:

1. RULES-MODE PARITY — the money path must be byte-for-byte unchanged in
   `mode="rules"`. The dev proved it for one multi-rule scenario; here we make it
   a *property*: over a battery of prefs shapes (empty, day-restricted, floor-only,
   ceiling-only, multi-rule, any-day-windowed), `match_candidates` AND `plan_cycle`
   must return IDENTICAL results with `source=None` (the pre-feature path) and with
   an explicit `RulesSource`. This is parity by equivalence-partitioning: one
   assertion per rule *shape*, not per value.
2. FAIL-SAFE — the alert must RE-ARM after a good read (fire again on a fresh
   failure the same day), and a `degraded` doc must still force dry-run even when
   the calendar reads fine and a matching event exists.
3. .ics PARSING — a non-London TZID (the brief's explicit gap) must convert to
   Europe/London.
5. WEEKEND-FIRST — the ordering must actually reach the booking decision: the
   catcher books the weekend slot before an earlier-dated weekday slot.

Plus a regression guard on the drop's new loop-level notifier: it must not emit a
calendar alert in rules mode.
"""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.calendar_source import CalendarReadError, parse_ics
from tennisbot.catcher import (WeekSlot, match_candidates, plan_cycle,
                               run_catcher_loop)
from tennisbot.models import RunResult, Slot
from tennisbot.prefs import Prefs, Rule, load_prefs, save_prefs
from tennisbot.window_source import RulesSource

LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _calendar_url(monkeypatch):
    # Calendar mode legitimately needs a URL in the env (§9.3); the fetch itself is
    # always FAKED here so nothing hits the network. Harmless for rules-mode tests.
    monkeypatch.setenv("TENNISBOT_CALENDAR_ICS_URL", "https://example/cal.ics")


def _at(iso):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


def _ics(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            + "".join(events) + "END:VCALENDAR\r\n")


def _ev(uid, start, end, *extra):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
            + "".join(l + "\r\n" for l in extra) + "END:VEVENT\r\n")


def _fetch(text):
    def f(url):
        return text
    return f


def _raising_fetch(url):
    raise CalendarReadError("calendar fetch failed: HTTP 503")


# ==========================================================================
# 1. RULES-MODE PARITY — property over prefs shapes (money path, non-negotiable)
# ==========================================================================

# A rich, fixed week grid spanning a weekend boundary, with a held cell (excluded
# by grid-idempotency) and past/other times, so the ranking has something to sort.
# 2026-08-01 Sat, 08-02 Sun, 08-03 Mon, 08-04 Tue.
_GRID = {"paddington": [
    WeekSlot("2026-08-01", "10:00", "available", "Tennis"),   # Sat morning
    WeekSlot("2026-08-01", "16:00", "available", "Tennis"),   # Sat afternoon
    WeekSlot("2026-08-02", "18:00", "available", "Tennis"),   # Sun evening
    WeekSlot("2026-08-03", "09:00", "available", "Tennis"),   # Mon morning
    WeekSlot("2026-08-03", "20:00", "available", "Tennis"),   # Mon evening
    WeekSlot("2026-08-04", "12:00", "available", "Tennis"),   # Tue midday
    WeekSlot("2026-08-02", "19:00", "my_booking", "Tennis"),  # held ⇒ excluded
]}
_NOW = _at("2026-07-25T09:00:00")

# One prefs config per RULE SHAPE (equivalence partitioning), each a distinct
# branch of `Prefs.rule_priority` / the drop window logic.
_PARITY_CONFIGS = {
    "empty_rules_permissive": Prefs(),
    "day_restricted_flat": Prefs(days=("Sat", "Sun")),
    "floor_only_flat": Prefs(earliest="18:00"),
    "ceiling_only_rule": Prefs(rules=(Rule(("Sat",), None, "12:00"),)),
    "multi_rule_priority": Prefs(rules=(Rule(("Sun",), "18:00", None),
                                        Rule(("Sat",), "10:00", "15:00"))),
    "any_day_windowed": Prefs(rules=(Rule((), "09:00", "21:00"),)),
    "multi_centre_priority": Prefs(centres=("paddington",),
                                   rules=(Rule(("Mon",), None, None),
                                          Rule(("Sat",), None, None))),
}


@pytest.mark.parametrize("name", list(_PARITY_CONFIGS))
def test_match_candidates_identical_source_none_vs_rules_source(name):
    # PARITY: the candidate list (order included) must be identical whether the
    # catcher uses the legacy inline predicate (source=None) or the new
    # RulesSource façade. Any divergence is a money-path regression.
    prefs = _PARITY_CONFIGS[name]
    baseline = match_candidates(_GRID, prefs, _NOW)
    via_source = match_candidates(_GRID, prefs, _NOW, source=RulesSource(prefs))
    assert baseline == via_source


@pytest.mark.parametrize("name", list(_PARITY_CONFIGS))
def test_plan_cycle_books_identical_candidate_source_none_vs_rules_source(name):
    # PARITY at the BOOKER level, not just the ranker: plan_cycle must pick the
    # same slot (and reason) through RulesSource as through the legacy path. This
    # is what actually determines what gets held.
    prefs = _PARITY_CONFIGS[name]
    kw = dict(memory={}, bookings=[], activity_matches=(), now=_NOW)
    baseline = plan_cycle(_GRID, prefs, **kw)
    via_source = plan_cycle(_GRID, prefs, source=RulesSource(prefs), **kw)
    assert baseline == via_source


def test_multi_rule_priority_orders_by_rule_index_not_date():
    # Guards the PRIMARY sort key: rule 0 (Sun eve) outranks rule 1 (Sat) even
    # though Saturday is the earlier date — the owner's rule ORDER wins.
    prefs = _PARITY_CONFIGS["multi_rule_priority"]
    got = [(c.date, c.time) for c in match_candidates(_GRID, prefs, _NOW)]
    assert got == [("2026-08-02", "18:00"),   # rule 0 first (Sun 18:00)
                   ("2026-08-01", "10:00")]   # rule 1 (Sat 10:00–15:00)


def test_empty_rules_falls_back_to_permissive_all_slots():
    # CLI/permissive fallback: empty rules ⇒ every non-held, non-past slot is a
    # candidate at priority 0, ordered by date then time (today's exact behaviour).
    got = [(c.date, c.time) for c in match_candidates(_GRID, Prefs(), _NOW)]
    assert got == [("2026-08-01", "10:00"), ("2026-08-01", "16:00"),
                   ("2026-08-02", "18:00"), ("2026-08-03", "09:00"),
                   ("2026-08-03", "20:00"), ("2026-08-04", "12:00")]


def test_day_skip_excludes_unpreferred_weekdays():
    # A Sat/Sun-only config admits ONLY weekend slots — the catcher's day filter.
    prefs = _PARITY_CONFIGS["day_restricted_flat"]
    dates = {c.date for c in match_candidates(_GRID, prefs, _NOW)}
    assert dates == {"2026-08-01", "2026-08-02"}     # no Mon/Tue


def test_ceiling_only_rule_admits_below_ceiling_only():
    # Sat -12:00 (no floor) admits the 10:00 Sat slot but not the 16:00 one, and
    # no non-Saturday slot at all.
    prefs = _PARITY_CONFIGS["ceiling_only_rule"]
    got = {(c.date, c.time) for c in match_candidates(_GRID, prefs, _NOW)}
    assert got == {("2026-08-01", "10:00")}


# ==========================================================================
# 2. FAIL-SAFE — re-arm after a good read, and degraded still forces dry-run
# ==========================================================================

class _Scanner:
    """Records (centre, date, time, dry_run) for every book so a test can assert
    BOTH that a slot was pursued AND whether it was a real hold."""

    def __init__(self, slots_by_centre=None, bookings=()):
        self.slots_by_centre = slots_by_centre or {}
        self.bookings = list(bookings)
        self.booked = []

    def scan(self, prefs):
        return self.slots_by_centre

    def get_bookings(self):
        return list(self.bookings)

    def book(self, centre, date, time, prefs):
        dry = not prefs.catcher_live
        self.booked.append((centre, date, time, dry))
        return RunResult(ok=True, dry_run=dry, message="would book",
                         chosen=Slot(date=date, time=time, court="C1",
                                     available=True, selector="#x"))

    def teardown(self):
        pass


class _Capture:
    def __init__(self):
        self.sends = []

    def send(self, text):
        self.sends.append(text)

    def send_photo(self, *a, **k):
        pass


def _grid(*specs):
    return {"paddington": [WeekSlot(d, t, st, "Tennis") for (d, t, st) in specs]}


def test_calendar_alert_rearms_after_a_good_read(tmp_path):
    # The §9.6 latch must fire on ENTERING failure and RE-ARM after a good read:
    # fail (alert) → OK (clears latch, silent) → fail again SAME DAY (alert again).
    # A once/day rate-limit that never re-armed would swallow the second outage —
    # this test is the regression guard against that class of bug.
    save_prefs(Prefs(mode="calendar"), tmp_path)
    calls = {"n": 0}

    def flaky_fetch(url):
        calls["n"] += 1
        if calls["n"] == 2:
            return _ics()                    # cycle 2: good read, zero events
        raise CalendarReadError("calendar fetch failed: HTTP 503")

    tg = _Capture()
    run_catcher_loop(max_cycles=3, notify=False, scanner=_Scanner(
        _grid(("2026-07-25", "18:00", "available"))), notifier=tg,
        config_dir=tmp_path, state_dir_override=tmp_path,
        now_fn=lambda: _at("2026-07-20T08:00:00"),
        sleep_fn=lambda *a, **k: None, calendar_fetch=flaky_fetch)
    alerts = [s for s in tg.sends if "Calendar unreadable" in s]
    assert len(alerts) == 2                  # re-armed: NOT rate-limited away


def test_calendar_degraded_prefs_forces_dry_run_even_with_a_matching_event(tmp_path):
    # A degraded doc must force dry-run regardless of the source. Here the calendar
    # reads fine AND an event matches the free slot, but a bad field degraded the
    # doc (→ catcher_live forced False), so the book must be DRY-RUN, never a real
    # hold. Proves the calendar path can't bypass the degraded → dry-run gate.
    (tmp_path / "prefs.json").write_text(
        '{"mode": "calendar", "catcher_live": true, "weekly_cap": -1}')
    loaded = load_prefs(tmp_path)
    assert "weekly_cap" in loaded.degraded and loaded.catcher_live is False
    assert loaded.mode == "calendar"          # mode itself survived

    scanner = _Scanner(_grid(("2026-07-25", "18:00", "available")))
    fetch = _fetch(_ics(_ev("s", "20260725T180000", "20260725T200000")))
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner, notifier=_Capture(),
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T08:00:00"),
                     sleep_fn=lambda *a, **k: None, calendar_fetch=fetch)
    assert scanner.booked == [("paddington", "2026-07-25", "18:00", True)]  # dry


# ==========================================================================
# 3. .ics PARSING — non-London TZID (the brief's explicit gap)
# ==========================================================================

D0 = dt.date(2026, 8, 1)
D7 = dt.date(2026, 8, 8)


def test_non_london_tzid_new_york_converts_to_london():
    # 2026-08-02 10:00 America/New_York (EDT, −4) == 15:00 Europe/London (BST, +1):
    # a 5h offset. The bot compares slot times in London, so a foreign TZID must be
    # converted, not taken literally.
    # TZID rides on the property name, so build the VEVENT literally.
    ics = _ics("BEGIN:VEVENT\r\nUID:ny\r\n"
               "DTSTART;TZID=America/New_York:20260802T100000\r\n"
               "DTEND;TZID=America/New_York:20260802T120000\r\n"
               "END:VEVENT\r\n")
    [w] = parse_ics(ics, D0, D7)
    assert w.earliest == "15:00" and w.latest == "17:00"


def test_non_london_tzid_paris_converts_to_london():
    # Europe/Paris (CEST, +2) 10:00 == 09:00 London (BST, +1): a −1h shift.
    ics = _ics("BEGIN:VEVENT\r\nUID:p\r\n"
               "DTSTART;TZID=Europe/Paris:20260802T100000\r\n"
               "DTEND;TZID=Europe/Paris:20260802T120000\r\n"
               "END:VEVENT\r\n")
    [w] = parse_ics(ics, D0, D7)
    assert w.earliest == "09:00" and w.latest == "11:00"


def test_floating_time_is_taken_as_london(monkeypatch):
    # A floating (no Z, no TZID) time is assumed to ALREADY be London civil time
    # (§9.4) — it must pass through unshifted, distinct from the UTC/foreign cases.
    ics = _ics(_ev("f", "20260802T100000", "20260802T120000"))
    [w] = parse_ics(ics, D0, D7)
    assert w.earliest == "10:00" and w.latest == "12:00"


# ==========================================================================
# 5. WEEKEND-FIRST reaches the booking decision (not just the parser)
# ==========================================================================

def test_catcher_calendar_books_weekend_slot_before_weekday(tmp_path):
    # Two free evening slots — Sat 01 Aug (weekend tier 0) and Mon 03 Aug (weekday
    # tier 1) — each covered by its own calendar event. plan_cycle books ONE per
    # cycle, so weekend-first ordering DECIDES which slot is held: it must be the
    # Saturday, even though (in a naive earliest-date sort) it isn't the sole
    # driver — this proves §9.5 flows all the way to the hold.
    save_prefs(Prefs(mode="calendar", catcher_live=True), tmp_path)
    scanner = _Scanner(_grid(("2026-08-01", "18:00", "available"),   # Sat
                             ("2026-08-03", "18:00", "available")))  # Mon
    fetch = _fetch(_ics(_ev("sat", "20260801T180000", "20260801T200000"),
                        _ev("mon", "20260803T180000", "20260803T200000")))
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner, notifier=_Capture(),
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-30T08:00:00"),
                     sleep_fn=lambda *a, **k: None, calendar_fetch=fetch)
    # Exactly one hold this cycle, and it's the weekend slot (live, since armed).
    assert scanner.booked == [("paddington", "2026-08-01", "18:00", False)]


# ==========================================================================
# Regression guard — the drop's new loop-level notifier stays quiet in rules mode
# ==========================================================================

def _fake_target():
    from tennisbot.config import (CourtConfig, Drop, Preference, Surface,
                                  Target)
    courts = CourtConfig(group="G", surfaces={"Synth": Surface("Synth", "Synth")},
                         preferred="Synth", enabled=["Synth"], two_hours=False)
    return Target(key="paddington", name="Paddington", provider="everyoneactive",
                  site="0", drop=Drop(7, "00:00", "Europe/London"),
                  max_holds_per_run=2, courts=courts,
                  want=[Preference("Sat", "10:00")])


def test_drop_rules_mode_notify_on_emits_no_calendar_alert(monkeypatch):
    # run_drop_loop now builds a loop-level Telegram when notify=True (for the
    # §9.6 calendar alert). In RULES mode that notifier must never speak — a
    # regression here would spam the owner on every normal drop. We stub Telegram
    # + Secrets so no env/network is needed and capture every send.
    import time as _t

    from tennisbot import runner
    captured = _Capture()
    monkeypatch.setattr(runner, "Telegram", lambda *a, **k: captured)

    class _Secrets:
        telegram_bot_token = "t"
        telegram_chat_id = "c"

    monkeypatch.setattr(runner.Secrets, "from_env", staticmethod(lambda: _Secrets))
    monkeypatch.setattr(runner, "load_targets",
                        lambda: {"paddington": _fake_target()})
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(runner, "load_prefs", lambda *a, **k: Prefs())  # rules
    fired = []
    monkeypatch.setattr(runner, "run_drop", lambda **k: fired.append(k))
    monkeypatch.setattr(runner, "_next_drop",
                        lambda *a, **k: (1_000_000_000.0, "2026-08-01"))

    runner.run_drop_loop(target_key="paddington", after_time="19:00",
                         notify=True, max_iters=1)
    assert len(fired) == 1                                  # normal drop fired
    assert captured.sends == []                             # loop notifier silent
