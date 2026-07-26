"""Schema-v2 tests for the prefs store: the per-day rule list, v1→v2 migration,
the rule predicates the catcher/drop lean on, and the two split live flags.

Pure and offline (tmp_path per test); no network, no clock reliance."""

import json

from dataclasses import replace

import pytest

from tennisbot.prefs import (Prefs, PrefsError, Rule, load_prefs, save_prefs,
                             validate)


def write(tmp_path, doc):
    (tmp_path / "prefs.json").write_text(
        doc if isinstance(doc, str) else json.dumps(doc))
    return tmp_path


# ---------------------------------------------------------------------------
# 1. v1 → v2 MIGRATION (backward compatibility is load-bearing)
# ---------------------------------------------------------------------------

def test_v1_flat_window_migrates_to_a_single_rule(tmp_path):
    # A v1 doc (no `rules`, no `version`) with a flat window must read as ONE
    # rule mirroring it, and must NOT be degraded (v1 is migrated, not rejected).
    p = load_prefs(write(tmp_path, '{"days": ["Tue","Thu"], "earliest": "18:00"}'))
    assert p.rules == (Rule(days=("Tue", "Thu"), earliest="18:00", latest=None),)
    assert p.days == ("Tue", "Thu") and p.earliest == "18:00"   # flat mirror
    assert p.degraded == () and p.version == 2


def test_v1_all_default_migrates_to_no_rules(tmp_path):
    # An all-default v1 doc synthesises NO rule (fully permissive) — identical
    # behaviour to today, so the drop never day-filter-skips.
    p = load_prefs(write(tmp_path, '{"weekly_cap": 5}'))
    assert p.rules == () and p.days == () and p.earliest is None


def test_v1_live_true_migrates_to_the_drop_only_not_the_catcher(tmp_path):
    # W1: v1 knew only the drop, so migrate `live:true` → drop_live only. The
    # catcher (a NEW booker) must NOT be silently armed — it needs /catcher on.
    p = load_prefs(write(tmp_path, '{"live": true}'))
    assert p.drop_live is True and p.catcher_live is False
    assert p.live is True                            # convenience = either armed


def test_v1_live_absent_is_both_off(tmp_path):
    p = load_prefs(write(tmp_path, '{"days": ["Sat"]}'))
    assert p.catcher_live is False and p.drop_live is False


# ---------------------------------------------------------------------------
# 2. v2 rules parsing (tolerant, fail-safe)
# ---------------------------------------------------------------------------

def test_v2_rules_parse_in_priority_order(tmp_path):
    p = load_prefs(write(tmp_path, {"version": 2, "rules": [
        {"days": ["Sun"], "earliest": "18:00"},
        {"days": ["Sat"], "earliest": "10:00", "latest": "15:00"},
    ]}))
    assert p.rules == (
        Rule(days=("Sun",), earliest="18:00", latest=None),
        Rule(days=("Sat",), earliest="10:00", latest="15:00"))
    # ≥2 rules ⇒ the flat mirror is cleared (a single window is meaningless).
    assert p.days == () and p.earliest is None


def test_a_bad_rule_is_dropped_and_degrades_but_good_ones_survive(tmp_path):
    p = load_prefs(write(tmp_path, {"rules": [
        {"days": ["Funday"]},                 # invalid day → dropped
        {"days": ["Sat"], "earliest": "10:00"}]}))
    assert p.rules == (Rule(days=("Sat",), earliest="10:00"),)
    assert p.degraded and p.live is False     # degraded ⇒ both live forced off


def test_an_inverted_rule_window_is_dropped(tmp_path):
    p = load_prefs(write(tmp_path, {"rules": [
        {"days": ["Sat"], "earliest": "20:00", "latest": "10:00"}]}))
    assert p.rules == () and p.degraded


def test_explicit_rules_take_precedence_over_flat_fields(tmp_path):
    # If BOTH keys are present, `rules` wins (the flat fields are just a mirror).
    p = load_prefs(write(tmp_path, {"days": ["Mon"], "rules": [
        {"days": ["Fri"], "earliest": "19:00"}]}))
    assert p.rules == (Rule(days=("Fri",), earliest="19:00"),)


# ---------------------------------------------------------------------------
# 3. ROUND TRIP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefs", [
    Prefs(rules=(Rule(days=("Sun",), earliest="18:00"),
                 Rule(days=("Sat",), earliest="10:00", latest="15:00")),
          max_holds=2, catcher_live=True, drop_live=False),
    Prefs(catcher_live=False, drop_live=True, max_holds=0),
])
def test_rules_and_flags_survive_a_disk_round_trip(tmp_path, prefs):
    saved = save_prefs(prefs, tmp_path)
    assert load_prefs(tmp_path) == saved
    assert Prefs.from_dict(saved.to_dict()) == saved


# ---------------------------------------------------------------------------
# 3b. THE FLAT⇄RULES MIRROR under construction and `dataclasses.replace`
#     (the __post_init__ contract: canonical=rules, flat is a derived mirror;
#      it must never desync and must never SILENTLY WIDEN the booking window).
# ---------------------------------------------------------------------------

def _mirror_is_consistent(p: Prefs) -> bool:
    """The invariant __post_init__ must maintain for ANY Prefs: exactly-one-rule
    ⇒ flat mirrors that rule; not-one-rule ⇒ flat is empty."""
    if len(p.rules) == 1:
        r = p.rules[0]
        return (p.days, p.earliest, p.latest) == (r.days, r.earliest, r.latest)
    return (p.days, p.earliest, p.latest) == ((), None, None)


@pytest.mark.parametrize("p", [
    Prefs(),                                                  # permissive
    Prefs(days=("Tue",), earliest="18:00"),                  # flat-constructed
    Prefs(rules=(Rule(days=("Sat",), earliest="10:00", latest="15:00"),)),
    Prefs(rules=(Rule(days=("Sun",)), Rule(days=("Sat",)))),  # multi-rule
])
def test_flat_mirror_is_consistent_on_construction(p):
    assert _mirror_is_consistent(p)


@pytest.mark.parametrize("change", [
    {"catcher_live": True}, {"drop_live": True}, {"weekly_cap": 9},
    {"max_holds": 0}, {"centres": ("westway",)}, {"slot_length_hours": 2},
])
def test_replace_a_non_window_field_never_desyncs_the_mirror(change):
    # Replacing an unrelated field must not desync flat from rules — this is the
    # property every /catcher, /drop, /cap, /holds command depends on.
    for base in (Prefs(rules=(Rule(days=("Sat",), earliest="10:00",
                                   latest="15:00"),)),
                 Prefs(rules=(Rule(days=("Sun",)), Rule(days=("Mon",)))),
                 Prefs()):
        assert _mirror_is_consistent(replace(base, **change))


def test_replacing_a_flat_field_directly_does_not_widen_the_window():
    # `rules` is canonical: a naked replace() of a flat field is IGNORED (the
    # mirror is re-derived from rules), so it can never silently WIDEN the
    # effective window behind the rule's back. Callers edit via /days · /window.
    p = Prefs(rules=(Rule(days=("Tue",), earliest="18:00"),))
    widened = replace(p, earliest="09:00")            # a lower floor = wider
    assert widened.earliest == "18:00"                # rule wins; not widened
    assert _mirror_is_consistent(widened)


def test_naked_replace_rules_empty_cannot_desync_even_though_it_is_a_footgun():
    # Documented footgun: replace(prefs, rules=()) re-synthesises the old rule
    # from the stale flat mirror (that's why telegram_commands._with_rules
    # exists). The safety net is that the INVARIANT still holds afterwards — flat
    # and rules never disagree — so no reader is ever handed a torn window.
    p = Prefs(rules=(Rule(days=("Tue",), earliest="18:00"),))
    resurrected = replace(p, rules=())
    assert resurrected.rules == p.rules               # footgun: not cleared
    assert _mirror_is_consistent(resurrected)         # but still consistent


def test_v2_document_is_idempotent_across_two_to_dict_from_dict_cycles():
    # Stronger than a single round trip: a multi-rule doc must be a FIXED POINT
    # of serialise∘deserialise, or repeated saves would drift the on-disk shape.
    p = Prefs(rules=(Rule(days=("Sun",), earliest="18:00"),
                     Rule(days=("Sat",), earliest="10:00", latest="15:00")),
              max_holds=2, catcher_live=True)
    once = Prefs.from_dict(p.to_dict())
    twice = Prefs.from_dict(once.to_dict())
    assert once == twice == p


# ---------------------------------------------------------------------------
# 4. PREDICATES
# ---------------------------------------------------------------------------

def _p():
    # Rule 0: Sun ≥18:00. Rule 1: Sat 10:00–15:00.
    return Prefs(rules=(Rule(days=("Sun",), earliest="18:00"),
                        Rule(days=("Sat",), earliest="10:00", latest="15:00")))


# 2026-07-25 = Sat, 2026-07-26 = Sun, 2026-07-27 = Mon.

def test_matching_rule_returns_the_highest_priority_admitting_rule():
    p = _p()
    assert p.matching_rule("2026-07-26", "18:00") == p.rules[0]     # Sun eve
    assert p.matching_rule("2026-07-25", "10:00") == p.rules[1]     # Sat morn
    assert p.matching_rule("2026-07-26", "17:00") is None           # Sun too early
    assert p.matching_rule("2026-07-25", "15:00") is None           # Sat ceiling excl
    assert p.matching_rule("2026-07-27", "18:00") is None           # Mon: no rule


def test_empty_rules_is_permissive_matching_rule_is_non_none():
    p = Prefs()
    assert p.matching_rule("2026-07-27", "06:00") is not None       # any day/time
    assert p.allows_date_time("2026-07-27", "06:00")


def test_matching_rule_fails_closed_on_an_unparseable_time():
    assert _p().matching_rule("2026-07-26", "6pm") is None


def test_empty_rules_should_also_fail_closed_on_an_unparseable_time():
    p = Prefs()                                   # default, fully permissive
    assert p.matching_rule("2026-07-27", "6pm") is None
    assert p.allows_date_time("2026-07-27", "6pm") is False


def test_allows_date_is_true_if_any_rule_covers_the_weekday():
    p = _p()
    assert p.allows_date("2026-07-26") and p.allows_date("2026-07-25")
    assert not p.allows_date("2026-07-27")                          # Mon
    assert Prefs().allows_date("2026-07-27")                        # empty ⇒ any


def test_allows_date_time_needs_both_day_and_window():
    p = _p()
    assert p.allows_date_time("2026-07-26", "20:00")
    assert not p.allows_date_time("2026-07-26", "12:00")            # Sun too early
    assert not p.allows_date_time("2026-07-25", "18:00")           # Sat past ceiling


def test_earliest_and_latest_for_date_read_the_matching_rule():
    p = _p()
    assert p.earliest_for_date("2026-07-26") == "18:00"            # Sun rule
    assert p.latest_for_date("2026-07-26") is None
    assert p.earliest_for_date("2026-07-25") == "10:00"           # Sat rule
    assert p.latest_for_date("2026-07-25") == "15:00"
    assert p.earliest_for_date("2026-07-27") is None              # Mon: no rule


def test_earliest_for_date_takes_the_highest_priority_matching_rule():
    # Two rules both cover Sun; the FIRST (highest priority) wins.
    p = Prefs(rules=(Rule(days=("Sun",), earliest="18:00"),
                     Rule(days=("Sun",), earliest="08:00")))
    assert p.earliest_for_date("2026-07-26") == "18:00"


# ---------------------------------------------------------------------------
# 5. VALIDATION of the new fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    Prefs(max_holds=-1),
    replace(Prefs(), max_holds=True),                # bool-is-int trap
    replace(Prefs(), catcher_live="yes"),
    replace(Prefs(), drop_live=1),
])
def test_validate_rejects_bad_new_fields(bad):
    with pytest.raises(PrefsError):
        validate(bad, ["paddington"])


def test_max_holds_zero_is_valid_it_pauses():
    validate(Prefs(max_holds=0), ["paddington"])


def test_validate_rejects_an_inverted_rule_window():
    # A directly-constructed inverted rule must not be persistable.
    bad = replace(Prefs(), rules=(Rule(days=("Sat",), earliest="20:00",
                                       latest="10:00"),))
    with pytest.raises(PrefsError):
        validate(bad, ["paddington"])
