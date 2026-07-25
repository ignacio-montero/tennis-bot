"""Loop-level money-safety tests (offline, fake scanner). The critic flagged two
gaps the original loop suite couldn't express: (1) an already-held slot must NOT
be re-booked next cycle — the re-book-storm guard — and (2) a degraded prefs.json
must force dry-run even if it says live:true.
"""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from tennisbot.catcher import run_catcher_loop
from tennisbot.models import RunResult, Slot
from tennisbot.prefs import Prefs, save_prefs

LONDON = ZoneInfo("Europe/London")


def _at(iso):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=LONDON)


class RecordingScanner:
    def __init__(self, slots, bookings):
        self.slots = slots
        self.bookings = list(bookings)
        self.booked = []
        self.last_dry_run = None

    def scan(self, prefs):
        return self.slots

    def get_bookings(self):
        return list(self.bookings)

    def book(self, centre, date, time, prefs):
        self.booked.append((centre, date, time))
        # Mirror the REAL scanner's gate exactly (catcher.py: dry_run=not live).
        self.last_dry_run = not prefs.live
        return RunResult(ok=True, dry_run=not prefs.live, message="ok",
                         chosen=Slot(date=date, time=time, court="C1",
                                     available=True, selector="#x"),
                         screenshot_path=None)

    def teardown(self):
        pass


def _slots(*specs):
    from tennisbot.catcher import WeekSlot
    return {"paddington": [WeekSlot(d, t, s, "T") for (d, t, s) in specs]}


def test_a_slot_already_held_on_that_date_is_never_rebooked(tmp_path):
    # THE re-book-storm guard: even though the grid shows 2026-07-23 18:00 as
    # available (e.g. EA renders a self-held slot as bookable — the DOM behaviour
    # the recon spike could not confirm), Manage Bookings authoritatively shows a
    # court already held that date, so the catcher must book NOTHING.
    save_prefs(Prefs(centres=("paddington",), earliest="18:00", weekly_cap=3),
               tmp_path)
    scanner = RecordingScanner(
        _slots(("2026-07-23", "18:00", "available")),
        bookings=[{"text": "Tennis", "paid": False, "day": 23, "mon": "Jul"}])
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner,
                     notifier=type("N", (), {"send": lambda *_: None,
                                             "send_photo": lambda *_a, **_k: None})(),
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T12:00:00"),
                     sleep_fn=lambda *_a, **_k: None)
    assert scanner.booked == []          # held that date already → no re-book


def test_degraded_prefs_force_dry_run_even_if_live_true(tmp_path):
    # A half-read config that ASKS to book live must still only dry-run
    # (prefs.from_dict forces live=False when degraded — API_SPEC §1.4a). Proven
    # end-to-end: the real prefs.json → load_prefs → loop → scanner.book gate.
    (tmp_path / "prefs.json").write_text(json.dumps(
        {"live": True, "centres": ["paddington"], "earliest": "18:00",
         "slot_length_hours": 3}))            # 3 is invalid → degraded
    scanner = RecordingScanner(
        _slots(("2026-07-23", "18:00", "available")), bookings=[])
    run_catcher_loop(max_cycles=1, notify=False, scanner=scanner,
                     notifier=type("N", (), {"send": lambda *_: None,
                                             "send_photo": lambda *_a, **_k: None})(),
                     config_dir=tmp_path, state_dir_override=tmp_path,
                     now_fn=lambda: _at("2026-07-20T12:00:00"),
                     sleep_fn=lambda *_a, **_k: None)
    assert scanner.booked == [("paddington", "2026-07-23", "18:00")]
    assert scanner.last_dry_run is True      # degraded → dry-run despite live:true
