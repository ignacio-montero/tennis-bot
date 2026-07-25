"""The money-safety gate + loop-level idempotency / race handling, driven
entirely through `run_catcher_loop` with a FAKE scanner (no browser, no network,
no Telegram, no Chromium — per the hard constraint).

The single most important property the catcher must never violate:
**no real hold unless the owner's prefs say `live` AND are fully readable.**
`prefs.live is False` OR a degraded (partially-unreadable) prefs document must
BOTH resolve to a dry-run "would book", never an actual hold. These tests prove
that end-to-end from a prefs.json on disk through load_prefs into the loop's
book() call.
"""

import json
import datetime as dt
from zoneinfo import ZoneInfo

from tennisbot.catcher import load_state, run_catcher_loop, slot_key
from tennisbot.models import RunResult, Slot
from tennisbot.prefs import Prefs, save_prefs

LONDON = ZoneInfo("Europe/London")


def _at(iso: str):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


class CapturingNotifier:
    def __init__(self):
        self.sends = []
        self.photos = []

    def send(self, text):
        self.sends.append(text)

    def send_photo(self, path, caption=None):
        self.photos.append((path, caption))


class FakeScanner:
    """Mirrors `_PlaywrightScanner.book`'s money-safety derivation exactly:
    `dry_run = not prefs.live`. This is what makes a loop-level test a valid
    check of the gate — the fake is dry-run/live sensitive the same way the real
    scanner is, so if the loop ever fed it a `live` prefs when it shouldn't, the
    fake would report dry_run=False."""

    def __init__(self, slots_by_centre=None, bookings=(), chosen=True):
        self.slots_by_centre = slots_by_centre or {}
        self.bookings = list(bookings)
        self.chosen = chosen
        self.scanned = 0
        self.booked = []
        self.book_dry_runs = []
        self.torn = 0

    def scan(self, prefs):
        self.scanned += 1
        return self.slots_by_centre

    def get_bookings(self):
        return list(self.bookings)

    def book(self, centre, date, time, prefs):
        dry = not prefs.live
        self.booked.append((centre, date, time))
        self.book_dry_runs.append(dry)
        chosen = Slot(date=date, time=time, court="Court 1", available=True,
                      selector="#x") if self.chosen else None
        return RunResult(ok=self.chosen, dry_run=dry, message="x", chosen=chosen)

    def teardown(self):
        self.torn += 1


def _slots(*specs):
    from tennisbot.catcher import WeekSlot
    return {"paddington": [WeekSlot(d, t, st, "T") for (d, t, st) in specs]}


def _run(scanner, tg, tmp_path, at="2026-07-20T08:00:00", cycles=1):
    run_catcher_loop(max_cycles=cycles, notify=False, scanner=scanner,
                     notifier=tg, config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at(at), sleep_fn=lambda *_a, **_k: None)


# ==========================================================================
# The dry-run / live gate (money safety) — priority 1
# ==========================================================================

def test_default_prefs_are_dry_run_never_a_hold(tmp_path):
    """No prefs.json ⇒ defaults ⇒ live=False. The book must be a dry-run and the
    notice must be 'Would book', NEVER a 'HELD' message (which is only sent for a
    real hold)."""
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.book_dry_runs == [True]
    assert any("Would book" in s for s in tg.sends)
    assert not any("HELD" in s for s in tg.sends)


def test_live_prefs_do_flip_to_a_real_hold(tmp_path):
    """The gate is NOT vacuous: a fully-valid `live: true` prefs.json makes the
    (fake) scanner book live and the loop emit a 'HELD' message. This is the
    control that proves the dry-run assertions elsewhere actually mean
    something."""
    save_prefs(Prefs(centres=("paddington",), live=True), tmp_path)
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.book_dry_runs == [False]
    assert any("HELD" in s for s in tg.sends)


def test_degraded_prefs_force_dry_run_even_with_live_true(tmp_path):
    """THE money-safety property. A prefs.json that says `live: true` but carries
    an unreadable field (slot_length_hours=3) is degraded ⇒ live is forced False
    on read ⇒ the catcher still scans but only ever dry-runs. A real hold here
    would book against a config we cannot fully trust."""
    (tmp_path / "prefs.json").write_text(json.dumps({
        "version": 1, "centres": ["paddington"], "live": True,
        "slot_length_hours": 3,          # invalid ⇒ document degraded
    }))
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.book_dry_runs == [True]          # never a live hold
    assert not any("HELD" in s for s in tg.sends)
    # And the owner is told booking is paused + why (loud, per §8.9).
    assert any("UNREADABLE" in s or "booking paused" in s for s in tg.sends)


def test_unreadable_prefs_file_forces_dry_run(tmp_path):
    """Corrupt JSON (not the absent-file path) ⇒ defaults marked degraded ⇒
    live forced False. A half-written file can never escalate to a live hold."""
    (tmp_path / "prefs.json").write_text("{ this is not json")
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.book_dry_runs == [True]
    assert not any("HELD" in s for s in tg.sends)


# ==========================================================================
# Idempotency + race handling at the loop level
# ==========================================================================

def test_already_held_slot_is_not_rebooked(tmp_path):
    """A wanted slot that the grid reports as 'my_booking' (already held) must
    never be booked again — grid-level idempotency, exercised through the loop."""
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "my_booking")))
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.booked == []
    assert tg.sends == []


def test_race_loss_touches_no_memory_and_stays_silent(tmp_path):
    """If the slot vanishes between detect and hold (book returns chosen=None),
    the catcher must NOT record hold-memory (so the lapsed-hold clock doesn't
    start on a hold that never happened) and must stay silent (an empty result
    is not news)."""
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")),
                          chosen=False)          # race loss: nothing held
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.booked == [("paddington", "2026-07-25", "18:00")]  # attempted
    state = load_state(tmp_path)
    assert state["slots"] == {}                  # no memory written
    assert tg.sends == []                        # silent


def test_cap_notice_does_not_mask_a_booking_when_uncapped(tmp_path):
    """Sanity pairing for the straddle bug (see test_catcher_edges): with a fresh
    week and cap 3, a single match books and there is NO cap notice — confirms
    the cap-notice path is only taken when genuinely capped."""
    scanner = FakeScanner(_slots(("2026-07-25", "18:00", "available")))
    tg = CapturingNotifier()
    _run(scanner, tg, tmp_path)
    assert scanner.booked == [("paddington", "2026-07-25", "18:00")]
    assert not any(s.startswith("🧢") for s in tg.sends)
    assert slot_key("paddington", "2026-07-25", "18:00") in load_state(tmp_path)["slots"]
