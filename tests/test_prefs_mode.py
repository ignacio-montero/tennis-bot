"""The `mode` (booking SOURCE) field on Prefs — ARCHITECTURE §9.3, API_SPEC §1.

Covers default/migrate, tolerant read (degrade on an unknown value → dry-run),
the write-path validate rejection, round-trip serialisation, and that the
renamed `live_state` property still reports LIVE/DRY-RUN.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennisbot.prefs import (DEFAULT_MODE, MODES, Prefs, PrefsError, load_prefs,
                             save_prefs, validate)


def _write(tmp_path, text):
    (tmp_path / "prefs.json").write_text(text)
    return tmp_path


# -- default / migrate ------------------------------------------------------

def test_absent_mode_defaults_to_rules():
    # A v1/v2 doc with no `mode` key upgrades silently to "rules" (no behaviour
    # change on upgrade), NOT degraded.
    p = Prefs.from_dict({"centres": ["paddington"]})
    assert p.mode == "rules" and p.degraded == ()


def test_default_prefs_mode_is_rules():
    assert Prefs().mode == DEFAULT_MODE == "rules"


def test_valid_calendar_mode_round_trips():
    p = Prefs.from_dict({"mode": "calendar"})
    assert p.mode == "calendar"
    assert Prefs.from_dict(p.to_dict()).mode == "calendar"


def test_to_dict_emits_mode():
    assert Prefs(mode="calendar").to_dict()["mode"] == "calendar"


# -- tolerant read: an unknown mode degrades (→ dry-run) --------------------

@pytest.mark.parametrize("bad", ["both", "rulez", "", 3, None, True])
def test_unknown_mode_degrades_and_forces_dry_run(bad):
    # An unreadable intent must NOT let us silently pick a source (§1.4a) — it
    # degrades the doc, which forces BOTH live flags off.
    p = Prefs.from_dict({"mode": bad, "catcher_live": True, "drop_live": True})
    assert "mode" in p.degraded
    assert p.catcher_live is False and p.drop_live is False
    assert p.mode == "rules"                       # falls back to the safe source


def test_degraded_mode_survives_a_full_load(tmp_path):
    _write(tmp_path, '{"mode": "nonsense", "drop_live": true}')
    p = load_prefs(tmp_path)
    assert "mode" in p.degraded and p.drop_live is False


# -- write path: validate rejects an unknown mode ---------------------------

def test_validate_rejects_unknown_mode():
    with pytest.raises(PrefsError):
        validate(Prefs(mode="calendarr"))


@pytest.mark.parametrize("m", MODES)
def test_validate_accepts_every_known_mode(m):
    validate(Prefs(mode=m))                          # must not raise


def test_save_round_trips_calendar_mode(tmp_path):
    saved = save_prefs(Prefs(mode="calendar"), tmp_path)
    assert saved.mode == "calendar"
    assert load_prefs(tmp_path).mode == "calendar"


# -- the renamed live-state property ---------------------------------------

def test_live_state_still_reports_live_and_dry_run():
    assert Prefs().live_state == "DRY-RUN"
    assert Prefs(drop_live=True).live_state == "LIVE"


def test_summary_shows_calendar_source_only_in_calendar_mode():
    assert "calendar" in Prefs(mode="calendar").summary()
    assert "calendar" not in Prefs(mode="rules").summary()
    # rules-mode summary still leads with the live state (unchanged contract).
    assert Prefs().summary().startswith("DRY-RUN")
    assert Prefs(mode="calendar").summary().startswith("DRY-RUN")
