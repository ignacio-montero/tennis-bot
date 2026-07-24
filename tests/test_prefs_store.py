"""Deterministic tests for the shared prefs store (API_SPEC §1). No network,
no real config dir — every test gets its own tmp_path."""

import json

from dataclasses import replace

import pytest

from tennisbot.prefs import (CONFIG_DIR_ENV, Prefs, PrefsError, config_dir,
                             load_prefs, parse_days, parse_time, prefs_path,
                             save_prefs, validate)


# -- defaults & degradation --------------------------------------------------

def test_absent_file_yields_defaults(tmp_path):
    # §1.1: "absent file ⇒ defaults, never a hard error" — a fresh box (or a
    # brand-new volume) must boot usable, not crash the booker on first read.
    p = load_prefs(tmp_path)
    assert p == Prefs.defaults()
    assert p.centres == ("paddington",) and p.days == ()
    assert p.slot_length_hours == 1 and p.weekly_cap == 3
    assert p.live is False              # safe-by-default: dry-run


def test_absent_directory_yields_defaults(tmp_path):
    # Reading must not even require the directory to exist (volume not yet
    # mounted / first boot).
    assert load_prefs(tmp_path / "never-created") == Prefs.defaults()


def test_corrupt_json_yields_defaults_not_an_exception(tmp_path):
    # A truncated or hand-edited file must not wedge an unattended booker.
    # It IS marked degraded though (a present-but-unreadable file means someone
    # configured something we can't read), which forces DRY-RUN — see
    # test_live_true_does_NOT_survive_a_corrupt_sibling_field for the why.
    (tmp_path / "prefs.json").write_text("{not json at all")
    p = load_prefs(tmp_path)
    assert p.live is False and p.degraded
    assert replace(p, degraded=()) == Prefs.defaults()


def test_missing_fields_fall_back_per_field(tmp_path):
    # §1.4: a partial document keeps what it states and defaults the rest —
    # this is what lets the schema grow without a migration step.
    (tmp_path / "prefs.json").write_text(json.dumps({"weekly_cap": 5}))
    p = load_prefs(tmp_path)
    assert p.weekly_cap == 5
    assert p.centres == ("paddington",) and p.slot_length_hours == 1


def test_garbage_field_defaults_only_that_field(tmp_path):
    (tmp_path / "prefs.json").write_text(json.dumps(
        {"centres": ["westway"], "slot_length_hours": 7, "earliest": "26:99",
         "weekly_cap": -4, "live": "yes please"}))
    p = load_prefs(tmp_path)
    assert p.centres == ("westway",)     # the good field survives
    assert p.slot_length_hours == 1      # bad ones fall back
    assert p.earliest is None
    assert p.weekly_cap == 3
    assert p.live is False               # a non-bool `live` must FAIL SAFE


# -- persistence -------------------------------------------------------------

def test_settings_survive_a_restart(tmp_path):
    # PRD §7: "settings survive a container restart (persisted, not
    # in-memory)". A fresh load_prefs() is exactly what a restarted process
    # does, so this is the acceptance criterion in miniature.
    saved = save_prefs(Prefs(centres=("westway",), days=("Tue", "Thu"),
                             earliest="18:00", latest="22:00",
                             slot_length_hours=2, weekly_cap=1, live=True),
                       tmp_path)
    reloaded = load_prefs(tmp_path)
    assert reloaded == saved
    assert reloaded.days == ("Tue", "Thu") and reloaded.live is True


def test_save_stamps_provenance(tmp_path):
    saved = save_prefs(Prefs.defaults(), tmp_path, updated_by="telegram")
    assert saved.updated_by == "telegram" and saved.updated_at
    assert saved.version == 1
    on_disk = json.loads((tmp_path / "prefs.json").read_text())
    assert on_disk["updated_by"] == "telegram" and on_disk["version"] == 1


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    # Atomicity comes from temp-file + os.replace. We can't easily observe a
    # torn read, but we CAN assert the mechanism's fingerprints: exactly one
    # file left behind, and the previous content fully replaced (never merged).
    save_prefs(Prefs(weekly_cap=1), tmp_path)
    save_prefs(Prefs(weekly_cap=9), tmp_path)
    assert [f.name for f in tmp_path.iterdir()] == ["prefs.json"]
    assert load_prefs(tmp_path).weekly_cap == 9


def test_config_dir_comes_from_the_env_var(tmp_path, monkeypatch):
    # Mirrors DROP_STATE_DIR / WATCHD_STATE_DIR: the container injects the
    # volume path, the code has no hardcoded location.
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "vol"))
    assert config_dir() == tmp_path / "vol"
    assert prefs_path() == tmp_path / "vol" / "prefs.json"
    save_prefs(Prefs(weekly_cap=2))
    assert load_prefs().weekly_cap == 2


# -- validation (§1.3) -------------------------------------------------------

def test_validate_accepts_a_good_config():
    validate(Prefs(centres=("paddington",), earliest="18:00", latest="22:00"),
             ["paddington", "westway"])


@pytest.mark.parametrize("bad, why", [
    (Prefs(centres=()), "empty centres"),
    (Prefs(centres=("atlantis",)), "unknown centre key"),
    (Prefs(earliest="7pm"), "not HH:MM"),
    (Prefs(earliest="22:00", latest="18:00"), "start after end"),
    (Prefs(earliest="18:00", latest="18:00"), "zero-width window"),
    (Prefs(slot_length_hours=3), "only 1 or 2 hours"),
    (Prefs(weekly_cap=-1), "cap must be >= 0"),
])
def test_validate_rejects(bad, why):
    with pytest.raises(PrefsError):
        validate(bad, ["paddington", "westway"])


def test_cap_zero_is_valid_it_pauses_booking():
    # §1.3: 0 is a legitimate value — "pause all booking without going
    # non-live" — so it must not be lumped in with negatives.
    validate(Prefs(weekly_cap=0), ["paddington"])


def test_parse_time_and_days():
    assert parse_time("09:05") == "09:05"
    with pytest.raises(PrefsError):
        parse_time("24:00")             # 24h clock ends at 23:59
    assert parse_days(["Tue", "Thu"]) == ("Tue", "Thu")
    assert parse_days(["thu,tue"]) == ("Tue", "Thu")     # canonical order
    assert parse_days(["Tuesday"]) == ("Tue",)
    assert parse_days(["any"]) == ()                     # () means any day
    with pytest.raises(PrefsError):
        parse_days(["Funday"])


# -- reader-facing predicates ------------------------------------------------

def test_empty_days_means_any_day():
    assert Prefs(days=()).allows_day("Mon")
    assert not Prefs(days=("Tue",)).allows_day("Mon")
    assert Prefs(days=("Tue",)).allows_day("tuesday")


def test_allows_date_maps_iso_dates_to_weekdays():
    # 2026-07-07 is a Tuesday; the sprinter uses this to decide whether to
    # bother with tonight's D+7 drop at all (ARCHITECTURE §8.6).
    p = Prefs(days=("Tue",))
    assert p.allows_date("2026-07-07")
    assert not p.allows_date("2026-07-08")


def test_time_window_is_inclusive_start_exclusive_end():
    # Matches the API_SPEC wording: `earliest` inclusive, `latest` exclusive —
    # so a 21:00 slot is NOT inside an 18:00-21:00 window.
    p = Prefs(earliest="18:00", latest="21:00")
    assert p.allows_time("18:00") and p.allows_time("20:00")
    assert not p.allows_time("17:59") and not p.allows_time("21:00")
    assert Prefs().allows_time("06:00")          # no bounds ⇒ anything


def test_summary_leads_with_the_mode():
    assert Prefs(live=False).summary().startswith("DRY-RUN")
    assert Prefs(live=True).summary().startswith("LIVE")
