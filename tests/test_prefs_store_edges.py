"""Edge-case suite for the prefs store (API_SPEC §1) — the cases the happy-path
suite in `test_prefs_store.py` doesn't reach.

Three risks this file exists to buy down, in priority order:

1. **Fail-safe direction.** `live` is the one field where being wrong is
   expensive (real money, real courts). A corrupt document must NEVER read as
   `live: True`. That is asymmetric: reading `False` when the file said `True`
   is a nuisance; the reverse is an incident. Tested exhaustively below.
2. **Tolerant read.** §1.1 promises "never a hard error". The happy-path suite
   tests one corrupt file; unattended bookers meet many shapes of corruption,
   so this file sweeps the *classes* of malformed input (not-JSON, JSON that
   isn't an object, every field individually poisoned).
3. **Atomicity for real.** The existing test asserts the *fingerprints* of an
   atomic write (no temp litter). This file asserts the *mechanism*: that the
   visible file still holds the whole old document at the instant of the swap,
   and that the swap is a same-directory rename.

Offline and deterministic: `tmp_path` per test, no network, no clock reliance.
"""

import datetime as dt
import json
import os
import threading
from dataclasses import replace

import pytest

from tennisbot.prefs import (CONFIG_DIR_ENV, LONDON, Prefs, PrefsError,
                             config_dir, load_prefs, parse_days, parse_time,
                             save_prefs, validate)

skip_if_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permission bits, so the unwritable-dir case "
           "cannot be simulated")


def write(tmp_path, text):
    (tmp_path / "prefs.json").write_text(text)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. FAIL-SAFE: a corrupt / malformed `live` must never read as True
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc", [
    '{"live": "true"}',        # string, not bool — the classic JSON hand-edit
    '{"live": "True"}',
    '{"live": "yes"}',
    '{"live": 1}',             # truthy int — must NOT be coerced
    '{"live": 1.0}',
    '{"live": [true]}',
    '{"live": {"value": true}}',
    '{"live": null}',
    '{"live": "on"}',
    '{"live": " true "}',
])
def test_a_malformed_live_flag_always_reads_as_dry_run(tmp_path, doc):
    # The one asymmetric field in the schema. Anything we cannot read as a
    # genuine JSON `true` degrades to DRY-RUN (§1.4 "safe-by-default"), because
    # the cost of the two failure directions is wildly different.
    p = load_prefs(write(tmp_path, doc))
    assert p.live is False
    assert p.mode == "DRY-RUN"


@pytest.mark.parametrize("doc", [
    "",                         # empty file (e.g. a truncated write from a
                                # crash mid-save, or a freshly-created volume)
    "   \n\t ",
    "{not json at all",
    '{"live": tr',              # truncated *while* it said true
    "null",                     # valid JSON, not an object
    "[]",
    '["live", true]',
    '"live"',
    "42",
    "true",                     # top-level bare `true` — the nastiest one
])
def test_unparseable_documents_degrade_to_safe_defaults(tmp_path, doc):
    # §1.1: absent/corrupt ⇒ safe values, never an exception. Note one case:
    # a file containing only `true` parses as valid JSON but isn't an object;
    # `from_dict` must reject it wholesale rather than truthily believing it.
    # Since 2026-07-24 an unreadable document is ALSO marked degraded, which
    # forces DRY-RUN — the values match defaults but the document is flagged
    # as not-understood, so the readers refuse to book (API_SPEC §1.4).
    p = load_prefs(write(tmp_path, doc))
    assert p.live is False
    assert p.degraded, "an unreadable document must be flagged degraded"
    assert replace(p, degraded=()) == Prefs.defaults()


def test_top_level_json_that_is_not_an_object_is_ignored(tmp_path):
    # Covers prefs.py:104-105 — the `isinstance(raw, dict)` guard. A JSON list
    # would otherwise blow up on `.get`, which is exactly the hard error §1.1
    # forbids.
    p = load_prefs(write(tmp_path, '[{"live": true}]'))
    assert p.live is False and p.degraded
    assert replace(p, degraded=()) == Prefs.defaults()


def test_live_true_does_NOT_survive_a_corrupt_sibling_field(tmp_path):
    # DECIDED 2026-07-24 (this test previously asserted the opposite, pinning
    # the old trade-off for the owner to rule on). A corrupt sibling means we
    # only partially understand the document — and EVERY constraint field's
    # default is the PERMISSIVE value, so falling back silently WIDENS what the
    # bot may do. Concretely: the owner sets `/cap 0` to pause before a
    # holiday, one field corrupts, the cap springs back to 3 and a LIVE bot
    # resumes holding courts on an account they deliberately paused.
    # So: refuse to book on a half-read document (API_SPEC §1.4).
    p = load_prefs(write(tmp_path, '{"live": true, "weekly_cap": "lots"}'))
    assert p.live is False, "a degraded document must force DRY-RUN"
    assert "weekly_cap" in p.degraded
    assert "booking paused" in p.summary()   # and the owner is told why


# ---------------------------------------------------------------------------
# 2. TOLERANT READ: every field individually corrupt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc, field, expected", [
    ('{"centres": "paddington"}', "centres", ("paddington",)),   # str not list
    ('{"centres": []}', "centres", ("paddington",)),             # empty
    ('{"centres": [""]}', "centres", ("paddington",)),           # blank entry
    ('{"centres": ["", "westway"]}', "centres", ("paddington",)),
    ('{"centres": [1, 2]}', "centres", ("paddington",)),
    ('{"centres": null}', "centres", ("paddington",)),
    ('{"centres": ["  westway  "]}', "centres", ("westway",)),   # trimmed, kept
    ('{"days": "Tue"}', "days", ()),                             # str not list
    ('{"days": ["Funday"]}', "days", ()),
    ('{"days": [1]}', "days", ()),
    ('{"days": ["Tue","tuesday","TUE"]}', "days", ("Tue",)),     # de-duped
    ('{"days": ["Sun","Mon"]}', "days", ("Mon", "Sun")),         # re-ordered
    ('{"earliest": "24:00"}', "earliest", None),                 # 24h ends 23:59
    ('{"earliest": "7pm"}', "earliest", None),
    ('{"earliest": "9:00"}', "earliest", None),                  # must be padded
    ('{"earliest": 1800}', "earliest", None),
    ('{"latest": "23:60"}', "latest", None),
    ('{"weekly_cap": -1}', "weekly_cap", 3),
    ('{"weekly_cap": true}', "weekly_cap", 3),                   # bool-is-int trap
    ('{"weekly_cap": 3.5}', "weekly_cap", 3),
    ('{"weekly_cap": "3"}', "weekly_cap", 3),
    ('{"slot_length_hours": 0}', "slot_length_hours", 1),
    ('{"slot_length_hours": 3}', "slot_length_hours", 1),
    ('{"slot_length_hours": "2"}', "slot_length_hours", 1),
])
def test_each_field_degrades_independently(tmp_path, doc, field, expected):
    # Boundary-value analysis over the schema: for every field, one value just
    # outside its legal set. The point is *isolation* — a bad field must not
    # take the rest of the document with it.
    assert getattr(load_prefs(write(tmp_path, doc)), field) == expected


def test_good_fields_survive_alongside_bad_ones(tmp_path):
    p = load_prefs(write(tmp_path, json.dumps({
        "centres": ["westway"], "days": ["Nonsense"], "earliest": "18:00",
        "latest": "99:99", "slot_length_hours": 9, "weekly_cap": 5,
        "live": "maybe"})))
    assert p.centres == ("westway",)                          # kept
    assert p.earliest == "18:00" and p.weekly_cap == 5        # kept
    assert p.days == () and p.latest is None                  # defaulted
    assert p.slot_length_hours == 1 and p.live is False       # defaulted


def test_unknown_extra_fields_are_ignored_not_fatal(tmp_path):
    # Forward compatibility: a NEWER writer (schema v2) must not brick an older
    # reader. This is why the parse is allow-list, not "reject unknown keys".
    p = load_prefs(write(tmp_path, json.dumps({
        "weekly_cap": 5, "future_field": {"deep": [1, 2]}, "version": 99})))
    assert p.weekly_cap == 5
    assert p.version == 99          # recorded, not enforced — v2 readers decide


# KNOWN BUG (see report): `length not in (1, 2)` is a *value* test, and in
# Python `True == 1` and `2.0 == 2`, so a JSON `true`/`2.0` sails through as a
# bool/float. The identical trap IS guarded for weekly_cap
# (`isinstance(cap, bool)` on prefs.py:142), so this is an inconsistency rather
# than a considered choice. `xfail(strict=True)` makes this an *executable bug
# report*: the suite stays green today, and the moment prefs.py is fixed this
# test XPASSes and FAILS the build, forcing the marker to be removed.
@pytest.mark.parametrize("doc", ['{"slot_length_hours": true}',
                                 '{"slot_length_hours": 2.0}'])
def test_slot_length_must_be_a_real_int(tmp_path, doc):
    assert type(load_prefs(write(tmp_path, doc)).slot_length_hours) is int


def test_read_never_raises_even_when_the_path_is_a_directory(tmp_path):
    # A misconfigured volume mount can leave a *directory* named prefs.json.
    # IsADirectoryError must still degrade, not propagate into a booking job.
    (tmp_path / "prefs.json").mkdir()
    p = load_prefs(tmp_path)
    assert p.live is False and p.degraded      # unreadable ⇒ refuse to book
    assert replace(p, degraded=()) == Prefs.defaults()


@skip_if_root
def test_read_never_raises_when_the_file_is_unreadable(tmp_path):
    f = write(tmp_path, '{"weekly_cap": 5}') / "prefs.json"
    f.chmod(0o000)
    try:
        p = load_prefs(tmp_path)
        assert p.live is False and p.degraded  # unreadable ⇒ refuse to book
        assert replace(p, degraded=()) == Prefs.defaults()
    finally:
        f.chmod(0o600)


# ---------------------------------------------------------------------------
# 3. ROUND-TRIP & PERSISTENCE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefs", [
    Prefs.defaults(),
    Prefs(centres=("paddington", "westway"), days=("Mon", "Sun"),
          earliest="00:00", latest="23:59", slot_length_hours=2,
          weekly_cap=0, catcher_live=True, drop_live=True),
    Prefs(days=(), earliest=None, latest=None, weekly_cap=0),
])
def test_disk_round_trip_is_lossless(tmp_path, prefs):
    # Serialisation round-trip: save → load must be the identity (modulo the
    # provenance stamp save_prefs adds). Catches tuple/list, None/"" and
    # ordering drift in one assertion instead of a field-by-field checklist.
    saved = save_prefs(prefs, tmp_path)
    assert load_prefs(tmp_path) == saved
    assert Prefs.from_dict(saved.to_dict()) == saved


def test_earliest_midnight_is_a_real_floor_not_a_missing_one(tmp_path):
    # "00:00" is truthy-adjacent: a `if self.earliest:` implementation would
    # treat it as "no floor". `is not None` is the correct test and this pins
    # it. Same class of bug as `if not count:` when 0 is meaningful.
    saved = save_prefs(Prefs(earliest="00:00", latest="06:00"), tmp_path)
    p = load_prefs(tmp_path)
    assert p.earliest == "00:00" and saved.earliest == "00:00"
    assert p.allows_time("00:00") and p.allows_time("05:59")
    assert not p.allows_time("06:00")        # `latest` is exclusive


def test_cap_zero_survives_the_round_trip(tmp_path):
    # 0 = "pause booking" (§1.3), a legitimate state that a truthiness-based
    # default would silently reset to 3.
    save_prefs(Prefs(weekly_cap=0), tmp_path)
    assert load_prefs(tmp_path).weekly_cap == 0


def test_a_second_save_fully_replaces_never_merges(tmp_path):
    save_prefs(Prefs(centres=("paddington", "westway"), days=("Tue",),
                     earliest="18:00"), tmp_path)
    save_prefs(Prefs(), tmp_path)
    p = load_prefs(tmp_path)
    assert p.centres == ("paddington",) and p.days == () and p.earliest is None


def test_env_var_wins_only_when_no_override_is_passed(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "from-env"))
    assert config_dir() == tmp_path / "from-env"
    assert config_dir(tmp_path / "explicit") == tmp_path / "explicit"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_env_var_falls_back_to_the_default_dir(monkeypatch, value):
    # A container started with `TENNISBOT_CONFIG_DIR=` (empty) must not resolve
    # the config path to the filesystem root.
    monkeypatch.setenv(CONFIG_DIR_ENV, value)
    assert config_dir().name == ".tennisbot-config"


# ---------------------------------------------------------------------------
# 4. ATOMICITY — assert the mechanism, not just its fingerprints
# ---------------------------------------------------------------------------

def test_the_visible_file_is_never_truncated_during_a_write(tmp_path,
                                                            monkeypatch):
    # The property that matters to the read-only sprinter: at no point does
    # prefs.json contain a partial document. We prove it by intercepting the
    # swap: just before `os.replace` runs, the *visible* file must still be the
    # complete OLD document and the complete NEW one must already exist
    # elsewhere. A naive `write_text` implementation fails this test twice over
    # (no replace call at all, and a truncated file).
    save_prefs(Prefs(weekly_cap=1), tmp_path)
    observed = {}
    real_replace = os.replace

    def spy(src, dst):
        observed["src"], observed["dst"] = str(src), str(dst)
        observed["visible_before"] = json.loads(
            (tmp_path / "prefs.json").read_text())
        observed["staged"] = json.loads(open(src).read())
        observed["calls"] = observed.get("calls", 0) + 1
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    save_prefs(Prefs(weekly_cap=9), tmp_path)

    assert observed["calls"] == 1
    assert observed["visible_before"]["weekly_cap"] == 1   # old, and WHOLE
    assert observed["staged"]["weekly_cap"] == 9           # new, fully written
    # Same-directory rename ⇒ same filesystem ⇒ POSIX-atomic. A temp file in
    # /tmp would make the "rename" a copy and reintroduce the torn read.
    assert os.path.dirname(observed["src"]) == os.path.dirname(observed["dst"])
    assert load_prefs(tmp_path).weekly_cap == 9


def test_a_failed_swap_leaves_the_old_config_and_no_litter(tmp_path,
                                                           monkeypatch):
    # Covers prefs.py:338-343 (the cleanup branch). Disk-full / EXDEV mid-write
    # must not leave the store empty *or* the directory filling with
    # `.prefs-*.tmp` orphans on a small homelab volume.
    save_prefs(Prefs(weekly_cap=1), tmp_path)

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_prefs(Prefs(weekly_cap=9), tmp_path)

    assert load_prefs(tmp_path).weekly_cap == 1
    assert sorted(f.name for f in tmp_path.iterdir()) == ["prefs.json"]


@skip_if_root
def test_an_unwritable_config_dir_raises_but_preserves_the_old_config(tmp_path):
    # ARCHITECTURE §8.5 mounts this volume read-only in the sprinter; a
    # mis-mounted catcher would hit exactly this. Documented current contract:
    # save_prefs RAISES (it does not swallow), and the previous document
    # survives untouched. The transport that wires this up must catch it —
    # see the report.
    d = tmp_path / "ro"
    d.mkdir()
    save_prefs(Prefs(weekly_cap=1), d)
    d.chmod(0o500)
    try:
        with pytest.raises(OSError):
            save_prefs(Prefs(weekly_cap=9), d)
    finally:
        d.chmod(0o700)
    assert load_prefs(d).weekly_cap == 1
    assert sorted(f.name for f in d.iterdir()) == ["prefs.json"]


def test_a_concurrent_reader_never_sees_a_partial_document(tmp_path):
    # The §1 contract note: "readers must tolerate a concurrent write". The
    # sprinter reads this file while the Telegram handler may be writing it.
    # A reader thread hammers load_prefs while the writer flips the cap; every
    # observation must be one of the two whole documents — never a default
    # (which is what a parse failure would produce).
    save_prefs(Prefs(weekly_cap=1), tmp_path)
    seen, errors, stop = [], [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                seen.append(load_prefs(tmp_path).weekly_cap)
            except Exception as e:          # pragma: no cover - failure path
                errors.append(e)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for i in range(200):
            save_prefs(Prefs(weekly_cap=1 if i % 2 else 9,
                             centres=("paddington", "westway")), tmp_path)
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors
    assert seen, "reader thread never got a read in"
    assert set(seen) <= {1, 9}, f"torn or defaulted read observed: {set(seen)}"


# ---------------------------------------------------------------------------
# 5. VALIDATION (§1.3) — the boundaries the happy-path suite skips
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("earliest, latest, ok", [
    ("00:00", "00:01", True),      # narrowest legal window
    ("23:58", "23:59", True),
    ("00:00", "23:59", True),
    ("18:00", "18:00", False),     # equal ⇒ zero-width ⇒ nothing bookable
    ("18:01", "18:00", False),     # inverted
    ("23:59", "00:00", False),     # a window that wraps midnight is NOT allowed
    (None, "18:00", True),         # half-open windows are fine
    ("18:00", None, True),
    (None, None, True),
])
def test_window_boundaries(earliest, latest, ok):
    # Boundary-value analysis on the one rule with an ordering constraint
    # (§1.3 "if both set, earliest < latest"). Note the wrap-midnight case:
    # it is *rejected*, which is correct because the readers compare strings.
    p = Prefs(earliest=earliest, latest=latest)
    if ok:
        validate(p, ["paddington"])
    else:
        with pytest.raises(PrefsError):
            validate(p, ["paddington"])


@pytest.mark.parametrize("bad", ["24:00", "23:60", "9:00", "099:00", "18:0",
                                 "18:00:00", "1800", "6pm", "18h00", "", " ",
                                 "18:00 ", "١٨:٠٠", "18：00"])
def test_parse_time_rejects_near_misses(bad):
    # Every one of these is something a human might actually type on a phone
    # (including the full-width colon and Arabic-Indic digits an IME can emit).
    # "18:00 " is here on purpose: parse_time strips, so it must PASS...
    if bad == "18:00 ":
        assert parse_time(bad) == "18:00"
        return
    with pytest.raises(PrefsError):
        parse_time(bad)


@pytest.mark.parametrize("hhmm", ["00:00", "09:05", "23:59", "12:00"])
def test_parse_time_accepts_the_full_legal_range(hhmm):
    assert parse_time(hhmm) == hhmm


@pytest.mark.parametrize("tokens, expected", [
    (["any"], ()), (["all"], ()), (["*"], ()), (["ANY"], ()),
    ([""], ()),                                   # nothing parseable ⇒ any
    (["  "], ()),
    (["Mon,Tue,Wed"], ("Mon", "Tue", "Wed")),
    (["sun", "sat"], ("Sat", "Sun")),             # canonical week order
    (["Monday", "mon", "MON"], ("Mon",)),         # de-duplicated
    (["Tue", "Tue"], ("Tue",)),
    (["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], tuple("Mon Tue Wed Thu "
                                                              "Fri Sat Sun"
                                                              .split())),
])
def test_parse_days_normalises(tokens, expected):
    assert parse_days(tokens) == expected


@pytest.mark.parametrize("tokens", [["Funday"], ["Tue", "Funday"],
                                    ["any", "Tue"], ["Mardi"], ["🎾"],
                                    ["Tue2"], ["T"]])
def test_parse_days_rejects_unknown_tokens(tokens):
    # ["any", "Tue"] is the interesting one: "any" is only a keyword when it is
    # the ONLY token, so a mixed message is ambiguous and must be rejected
    # rather than guessed at.
    with pytest.raises(PrefsError):
        parse_days(tokens)


def test_validate_rejects_a_non_bool_live():
    # Covers the live-flag guard. Defence in depth: from_dict already fails safe,
    # but a caller constructing Prefs directly must not be able to persist a
    # non-bool into the document the sprinter trusts. Both split flags checked.
    with pytest.raises(PrefsError):
        validate(replace(Prefs(), catcher_live="yes"), ["paddington"])
    with pytest.raises(PrefsError):
        validate(replace(Prefs(), drop_live="yes"), ["paddington"])


def test_validate_skips_the_membership_check_only_for_an_explicit_none():
    # DELIBERATE FAIL-OPEN (prefs.py:275-283 + telegram_commands.py:200-202):
    # if targets.yaml can't be read, `known_centres()` returns () and the
    # handler passes `... or None`, which makes validate skip the membership
    # check rather than locking the owner out of their own bot. Note the
    # distinction pinned here: None = "don't check", [] = "nothing is valid".
    # Passing [] straight through would reject *every* centre — which is why
    # the `or None` in the handler is load-bearing, not decoration.
    validate(Prefs(centres=("atlantis",)), None)
    with pytest.raises(PrefsError):
        validate(Prefs(centres=("atlantis",)), [])


def test_known_centres_reads_the_real_target_keys():
    # The membership check in §1.3 is only as good as its source of truth. This
    # reads the repo's own config/targets.yaml (a file, not a network call), so
    # it also fails if someone renames a target key without updating the docs.
    from tennisbot.prefs import known_centres
    assert set(known_centres()) >= {"paddington", "westway"}


def test_known_centres_returns_empty_when_targets_cannot_be_loaded(monkeypatch):
    # Covers prefs.py:278-283. An unreadable targets.yaml must degrade to "()"
    # (⇒ skip the check) rather than raising inside a Telegram command.
    import tennisbot.config as cfg

    def boom(*a, **k):
        raise OSError("targets.yaml is gone")

    monkeypatch.setattr(cfg, "load_targets", boom)
    from tennisbot.prefs import known_centres
    assert known_centres() == ()


def test_a_failure_while_cleaning_up_does_not_mask_the_real_error(tmp_path,
                                                                  monkeypatch):
    # Covers prefs.py:341-342. Error-masking is a classic: if the cleanup in
    # the `except` block raises, the ORIGINAL cause ("no space left") would be
    # replaced by a misleading secondary error and the operator would debug the
    # wrong thing. This pins that the first exception is the one that escapes.
    save_prefs(Prefs(weekly_cap=1), tmp_path)

    def no_space(src, dst):
        raise OSError(28, "No space left on device")

    def unlink_fails(path):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", no_space)
    monkeypatch.setattr(os, "unlink", unlink_fails)
    with pytest.raises(OSError) as exc:
        save_prefs(Prefs(weekly_cap=9), tmp_path)
    assert exc.value.errno == 28
    assert load_prefs(tmp_path).weekly_cap == 1


def test_validate_does_not_mutate_the_candidate():
    # `validate` is a pure predicate; if it ever normalised in place, a rejected
    # update could half-apply — the exact failure PRD §7 forbids.
    p = Prefs(centres=("paddington",), earliest="18:00", latest="22:00")
    before = p.to_dict()
    validate(p, ["paddington"])
    assert p.to_dict() == before


# ---------------------------------------------------------------------------
# 6. READER PREDICATES the catcher/sprinter will lean on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iso, day, allowed", [
    ("2026-07-27", "Mon", True), ("2026-07-26", "Sun", True),
    ("2026-07-25", "Sat", False),
])
def test_allows_date_covers_the_week_ends(iso, day, allowed):
    # Sun/Mon are the wrap-around indices in Python's weekday() (Mon=0, Sun=6);
    # an off-by-one in the DAYS tuple would show up exactly here.
    assert Prefs(days=("Mon", "Sun")).allows_date(iso) is allowed


def test_no_days_configured_allows_every_date_in_a_week():
    p = Prefs(days=())
    for d in range(20, 27):
        assert p.allows_date(f"2026-07-{d}")


def test_window_text_and_summary_render_every_shape():
    # These strings go straight to the owner's phone; a crash or a "None" in
    # them is a user-visible defect even though nothing booked wrongly.
    assert Prefs().window_text() == "any time"
    assert "18:00" in Prefs(earliest="18:00").window_text()
    assert "22:00" in Prefs(latest="22:00").window_text()
    for p in (Prefs(), Prefs(catcher_live=True, drop_live=True, days=("Tue",),
                             earliest="18:00", latest="22:00", weekly_cap=0,
                             centres=("paddington", "westway"))):
        s = p.summary()
        assert "None" not in s and s.strip()


def test_summary_shows_a_zero_cap_rather_than_hiding_it():
    assert "cap 0" in Prefs(weekly_cap=0).summary()


def test_save_stamps_a_timezone_aware_london_timestamp(tmp_path):
    saved = save_prefs(Prefs(), tmp_path,
                       now=dt.datetime(2026, 7, 24, 9, 0, tzinfo=LONDON))
    assert saved.updated_at == "2026-07-24T09:00:00+01:00"
    # ...and it must survive the round trip so /status can show provenance.
    assert load_prefs(tmp_path).updated_at == "2026-07-24T09:00:00+01:00"


def test_validate_also_rejects_bool_slot_length():
    # Regression guard for a gap the FIRST fix missed: from_dict was hardened
    # against bool-is-int but validate() still used a bare `not in (1, 2)`, so
    # the write path bypassed the read path's guard. Both doors need the lock.
    for bad in (True, 2.0):
        with pytest.raises(PrefsError):
            validate(replace(Prefs.defaults(), slot_length_hours=bad),
                     ["paddington"])


def test_missing_version_defaults_silently_not_degraded(tmp_path):
    # A hand-edited/partial prefs.json without a `version` key must default the
    # version like any other absent field — NOT mark the doc degraded (which
    # would force dry-run). Only a NEWER-than-ours or malformed version degrades.
    # (Regression: a missing version briefly degraded every partial doc, so a
    # hand-edited config with live:true was silently stuck in dry-run.)
    p = load_prefs(write(tmp_path, '{"live": true, "earliest": "18:00"}'))
    assert p.degraded == () and p.live is True and p.version == 2


def test_newer_schema_version_degrades_forcing_dry_run(tmp_path):
    # A version we don't understand may have changed field meanings — refuse to
    # guess, force dry-run (the safe direction for an unknown schema).
    p = load_prefs(write(tmp_path, '{"version": 99, "live": true}'))
    assert p.live is False and "version" in p.degraded


@pytest.mark.parametrize("doc", [
    '{"version": "1", "live": true}',     # string, not int — a hand-edit
    '{"version": true, "live": true}',    # bool (True == 1, but not honourable)
    '{"version": 1.0, "live": true}',     # float — range()/schema logic wants int
    '{"version": null, "live": true}',    # explicit null is "absent"? see below
])
def test_malformed_version_forces_dry_run(doc, tmp_path):
    # A version we can't parse is corruption (not a partial/absent field), so it
    # must degrade → force dry-run — EXCEPT explicit null, which the code treats
    # as "absent" (version is None ⇒ default). This pins that boundary so a
    # future refactor can't silently start degrading a legitimately-absent key.
    p = load_prefs(write(tmp_path, doc))
    if "null" in doc:
        assert p.degraded == () and p.live is True    # null ≡ absent ⇒ OK
    else:
        assert p.live is False and "version" in p.degraded


@pytest.mark.parametrize("v", [1, 0])
def test_equal_or_older_version_is_not_degraded(v, tmp_path):
    # Only a version NEWER than ours can have changed field meanings. An exact
    # match and an older doc are both readable by a v1 reader ⇒ never degrade,
    # so a live:true survives (mirror of the newer-version test above).
    p = load_prefs(write(tmp_path, json.dumps({"version": v, "live": True})))
    assert p.degraded == () and p.live is True
