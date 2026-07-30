"""Calendar plan preview (§8.12): calendar mode proactively confirms what the
upcoming week maps to, change-triggered so it's informative but never noisy.
Directly addresses the owner's ask — "tell me what it would book".
"""

import datetime as dt

from tennisbot.calendar_source import Window
from tennisbot.catcher import _fmt_window, _maybe_calendar_preview
from tennisbot.prefs import Prefs

D0 = dt.date(2026, 8, 1)      # Sat
D7 = dt.date(2026, 8, 8)


class _FakeSource:
    def __init__(self, windows):
        self._w = windows

    def all_windows(self, d0, d7):
        return list(self._w)


class _FakeTg:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def _prefs(**kw):
    return Prefs(**kw)


def _win(date, earliest, latest, pk):
    return Window(date=date, earliest=earliest, latest=latest, priority_key=pk)


# ── formatting ──────────────────────────────────────────────────────────────

def test_fmt_window_variants():
    assert _fmt_window(_win("2026-08-01", "18:00", "20:00", 0)).endswith("18:00–20:00")
    assert "from 18:00" in _fmt_window(_win("2026-08-01", "18:00", None, 0))
    assert "until 20:00" in _fmt_window(_win("2026-08-01", None, "20:00", 0))
    assert "any time" in _fmt_window(_win("2026-08-01", None, None, 0))
    assert _fmt_window(_win("2026-08-01", "18:00", "20:00", 0)).startswith("Sat 01 Aug")


# ── change-triggered sending ────────────────────────────────────────────────

def test_first_read_sends_plan_and_latches():
    state, tg = {}, _FakeTg()
    src = _FakeSource([_win("2026-08-01", "14:00", "18:00", (0, "2026-08-01", "14:00"))])
    _maybe_calendar_preview(state, src, _prefs(), tg, D0, D7)
    assert len(tg.sent) == 1
    assert "Calendar plan" in tg.sent[0]
    assert "Sat 01 Aug" in tg.sent[0]
    assert state.get("calendar_plan_sig")            # latched


def test_unchanged_plan_is_silent():
    state, tg = {}, _FakeTg()
    src = _FakeSource([_win("2026-08-01", "14:00", "18:00", (0, "2026-08-01", "14:00"))])
    _maybe_calendar_preview(state, src, _prefs(), tg, D0, D7)
    _maybe_calendar_preview(state, src, _prefs(), tg, D0, D7)   # same plan again
    assert len(tg.sent) == 1                         # only the first cycle sent


def test_changed_plan_re_sends():
    state, tg = {}, _FakeTg()
    src1 = _FakeSource([_win("2026-08-01", "14:00", "18:00", (0, "2026-08-01", "14:00"))])
    _maybe_calendar_preview(state, src1, _prefs(), tg, D0, D7)
    src2 = _FakeSource([                              # owner added a Sunday event
        _win("2026-08-01", "14:00", "18:00", (0, "2026-08-01", "14:00")),
        _win("2026-08-02", "10:00", "12:00", (0, "2026-08-02", "10:00")),
    ])
    _maybe_calendar_preview(state, src2, _prefs(), tg, D0, D7)
    assert len(tg.sent) == 2
    assert "Sun 02 Aug" in tg.sent[1]


def test_empty_calendar_states_it_once():
    state, tg = {}, _FakeTg()
    empty = _FakeSource([])
    _maybe_calendar_preview(state, empty, _prefs(), tg, D0, D7)
    _maybe_calendar_preview(state, empty, _prefs(), tg, D0, D7)
    assert len(tg.sent) == 1
    assert "no tennis events" in tg.sent[0].lower()


def test_weekend_first_ordering_in_message():
    state, tg = {}, _FakeTg()
    # A weekday (Thu, pk group 1) and a weekend day (Sat, pk group 0): Sat ranks
    # first regardless of calendar order (weekend-first, §9.5).
    src = _FakeSource([
        _win("2026-08-06", "18:00", "20:00", (1, "2026-08-06", "18:00")),   # Thu
        _win("2026-08-01", "14:00", "18:00", (0, "2026-08-01", "14:00")),   # Sat
    ])
    _maybe_calendar_preview(state, src, _prefs(), tg, D0, D7)
    msg = tg.sent[0]
    assert msg.index("Sat 01 Aug") < msg.index("Thu 06 Aug")


def test_send_failure_never_raises():
    class _BoomTg:
        def send(self, msg):
            raise RuntimeError("telegram down")

    state = {}
    src = _FakeSource([_win("2026-08-01", "14:00", "18:00", (0, "2026-08-01", "14:00"))])
    # Must swallow — a preview hiccup can never abort the booking cycle.
    _maybe_calendar_preview(state, src, _prefs(), _BoomTg(), D0, D7)
    assert state.get("calendar_plan_sig")            # latched despite the failure
