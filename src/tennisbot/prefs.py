"""Shared preferences store — the `prefs.json` contract (API_SPEC §1).

The single source of truth for "what to book". Written by the Telegram command
handler (sole writer, inside the catcher process); read by **both** the catcher
and the drop sprinter. One small JSON document on the shared `tennisbot-config`
volume — same file-on-a-volume idiom as `watchd.bracket.json`, no new datastore
(ARCHITECTURE §8.5).

Two properties this module exists to guarantee:

- **Never a hard error on read.** A missing directory, missing file, corrupt
  JSON or a garbage field all degrade to the documented defaults (§1.4), so a
  fresh box boots usable and a half-written file can never wedge a booker.
- **Never a torn read.** Writes go to a temp file in the same directory and are
  swapped in with `os.replace`, which is atomic on POSIX: a concurrent reader
  sees either the whole old file or the whole new one.

Validation (§1.3) lives here too, as pure functions raising `PrefsError`, so the
Telegram handler can validate a *candidate* config and simply not save it when
it's invalid — which is what makes "an invalid update leaves the previous config
intact" structurally true rather than a thing we remember to do.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import structlog

from .config import ROOT

log = structlog.get_logger()

LONDON = ZoneInfo("Europe/London")

SCHEMA_VERSION = 2
PREFS_FILENAME = "prefs.json"

# Env-injected location, exactly like DROP_STATE_DIR / WATCHD_STATE_DIR. Read at
# call time (not import time) so a process — or a test — can point it elsewhere
# without re-importing the module.
CONFIG_DIR_ENV = "TENNISBOT_CONFIG_DIR"
DEFAULT_CONFIG_DIR = ROOT / ".tennisbot-config"

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY_LOOKUP = {}
for _i, _d in enumerate(DAYS):
    _full = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")[_i]
    _DAY_LOOKUP[_d.lower()] = _d
    _DAY_LOOKUP[_full.lower()] = _d

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _norm_hhmm(hhmm: str) -> str | None:
    """Zero-pad "9:00"→"09:00" and validate; None if unparseable.

    Comparison of HH:MM windows is lexicographic, which is only correct on
    ZERO-PADDED strings ("9:00" > "18:00" as text). Every window predicate runs
    its input through here so the padding rule lives in exactly one place and
    unparseable input FAILS CLOSED (returns None ⇒ callers refuse to book)."""
    t = (hhmm or "").strip()
    if len(t) == 4 and t[1] == ":":            # "9:00" -> "09:00"
        t = "0" + t
    return t if _HHMM.match(t) else None


def _window_admits(earliest: str | None, latest: str | None, t: str) -> bool:
    """`earliest` inclusive, `latest` exclusive. `t` must be pre-normalised."""
    if earliest is not None and t < earliest:
        return False
    if latest is not None and t >= latest:
        return False
    return True


def _window_text(earliest: str | None, latest: str | None) -> str:
    if earliest is None and latest is None:
        return "any time"
    return f"{earliest or 'any'}–{latest or 'any'}"


class PrefsError(ValueError):
    """An invalid preference update. The message is shown to the owner verbatim
    in Telegram, so it must say what was wrong AND what a good value looks
    like."""


@dataclass(frozen=True)
class Rule:
    """One per-day booking rule (schema v2). A candidate (date, time) satisfies a
    rule when the rule's `days` include that weekday AND its window admits the
    time. Empty `days` = any day; `None` bounds = unbounded on that side."""

    days: tuple[str, ...] = ()      # () = any day; canonical Mon..Sun tokens
    earliest: str | None = None     # "HH:MM" inclusive floor
    latest: str | None = None       # "HH:MM" exclusive ceiling

    def covers_day(self, day: str) -> bool:
        return (not self.days) or day in self.days

    def admits_time(self, t: str) -> bool:
        """`t` must already be zero-padded (callers pass `_norm_hhmm` output)."""
        return _window_admits(self.earliest, self.latest, t)

    def is_permissive(self) -> bool:
        return not self.days and self.earliest is None and self.latest is None


# The permissive "any day / any time" rule, returned by `matching_rule` when the
# owner has configured no rules at all (so callers get a non-None Rule to read
# earliest/latest off, and None unambiguously means "no rule admits this slot").
_ANY_RULE = Rule()


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Prefs:
    """The active preferences (API_SPEC §1.2). Frozen + tuples: an update is a
    new object (`dataclasses.replace`), never a mutation of the live one, so a
    rejected update cannot half-apply.

    **Schema v2 — the rule list is canonical.** `rules` is an ordered tuple of
    `Rule`s, index 0 = highest priority; empty `rules` = fully permissive (any
    day, any time), which is the v1 default. The flat `days`/`earliest`/`latest`
    fields are KEPT but DERIVED (see `__post_init__`): when there is exactly one
    rule they mirror it, otherwise they are empty. Keeping them (rather than
    ripping every reader out) is the lower-risk migration — `/status`, the
    summary line, and any hand-reader keep working unchanged for the common
    single-window config, and `to_dict` still emits them for a tolerant reader.
    """

    centres: tuple[str, ...] = ("paddington",)
    days: tuple[str, ...] = ()        # DERIVED mirror of a single rule (§v2)
    earliest: str | None = None       # DERIVED "HH:MM" inclusive floor
    latest: str | None = None         # DERIVED "HH:MM" exclusive ceiling
    rules: tuple[Rule, ...] = ()      # ordered, index 0 = highest priority
    slot_length_hours: int = 1
    weekly_cap: int = 3               # PAID court bookings per Mon-reset week
    max_holds: int = 5               # concurrent UNPAID holds ceiling (0 = pause)
    # Two independent live switches (schema v2). A v1 `live: true` migrates to
    # DROP-ONLY (the only booker that existed under v1); the catcher stays off
    # until an explicit `/catcher on`. `live` (below) is a read-only convenience
    # = "is anything live".
    catcher_live: bool = False
    drop_live: bool = False
    updated_at: str | None = None
    updated_by: str = "default"
    version: int = SCHEMA_VERSION
    # Fields that failed to parse on read. NON-EMPTY ⇒ this document is only
    # partially understood, so BOTH live flags are forced False (API_SPEC §1.4):
    # every constraint field's default is the PERMISSIVE value (no rules = any
    # day/time, cap back to 3), so falling back to defaults on a *configured*
    # box silently WIDENS what the bot may do. Refusing to book is the only safe
    # direction when we can't read the owner's intent.
    degraded: tuple[str, ...] = ()

    def __post_init__(self):
        """Keep the flat `days`/`earliest`/`latest` mirror in lock-step with the
        canonical `rules`, in BOTH directions, so old readers and new code never
        disagree:

        - rules set  → derive flat from the sole rule (or clear them for a
          multi-rule config, where a single flat window is meaningless);
        - rules empty but flat set (a v1-style `Prefs(days=..., earliest=...)`
          construction) → synthesise the single rule those flat fields imply.

        Runs on every construction AND every `dataclasses.replace`, so a setter
        that only touches `rules` (see `/days`/`/window`) still yields a
        consistent object without the callers having to remember to sync."""
        if self.rules:
            if len(self.rules) == 1:
                r = self.rules[0]
                object.__setattr__(self, "days", r.days)
                object.__setattr__(self, "earliest", r.earliest)
                object.__setattr__(self, "latest", r.latest)
            else:
                object.__setattr__(self, "days", ())
                object.__setattr__(self, "earliest", None)
                object.__setattr__(self, "latest", None)
        elif self.days or self.earliest is not None or self.latest is not None:
            object.__setattr__(self, "rules", (Rule(days=self.days,
                                                    earliest=self.earliest,
                                                    latest=self.latest),))

    @property
    def live(self) -> bool:
        """Read-only convenience: is EITHER booker armed? The canonical state is
        the two flags; this keeps summary/mode and legacy readers terse."""
        return self.catcher_live or self.drop_live

    # -- (de)serialisation --------------------------------------------------

    @classmethod
    def defaults(cls) -> "Prefs":
        return cls()

    @classmethod
    def from_dict(cls, raw: dict) -> "Prefs":
        """Tolerant parse: every field independently falls back to its default
        if absent or unusable. Deliberately forgiving — this file is read by
        unattended bookers on a box nobody is watching, so "boot with safe
        defaults and log" beats "crash on a stray value"."""
        d = cls.defaults()
        if not isinstance(raw, dict):
            # Not even an object — we understand nothing, so refuse to book.
            return replace(d, degraded=("<document>",))

        bad_fields: list[str] = []

        def _bad(field: str, value) -> None:
            log.warning("prefs.field_invalid", field=field, value=repr(value))
            bad_fields.append(field)

        centres = raw.get("centres", d.centres)
        if (isinstance(centres, (list, tuple)) and centres
                and all(isinstance(c, str) and c.strip() for c in centres)):
            centres = tuple(c.strip() for c in centres)
        else:
            _bad("centres", centres)
            centres = d.centres

        days = raw.get("days", d.days)
        if isinstance(days, (list, tuple)) and all(
                isinstance(x, str) and x.strip().lower() in _DAY_LOOKUP
                for x in days):
            days = _canonical_days(days)
        else:
            _bad("days", days)
            days = d.days

        times = {}
        for field in ("earliest", "latest"):
            val = raw.get(field, None)
            if val is None or (isinstance(val, str) and _HHMM.match(val)):
                times[field] = val
            else:
                _bad(field, val)
                times[field] = None

        length = raw.get("slot_length_hours", d.slot_length_hours)
        # `type(x) is int`, NOT `in (1, 2)`: bool is a subclass of int and
        # True == 1, and 2.0 == 2, so a value test silently admits both. A
        # float then reaches the booker, where range(2.0) raises TypeError.
        if type(length) is not int or length not in (1, 2):
            _bad("slot_length_hours", length)
            length = d.slot_length_hours

        cap = raw.get("weekly_cap", d.weekly_cap)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            _bad("weekly_cap", cap)
            cap = d.weekly_cap

        holds = raw.get("max_holds", d.max_holds)
        if not isinstance(holds, int) or isinstance(holds, bool) or holds < 0:
            _bad("max_holds", holds)
            holds = d.max_holds

        # Two live flags (schema v2). Prefer the canonical pair; fall back to a
        # v1 `live` (migrate → DROP-ONLY, see W1 below). A non-bool of either
        # fails SAFE (dry-run).
        def _bool(key):
            v = raw.get(key, False)
            if isinstance(v, bool):
                return v
            _bad(key, v)
            return False

        if "catcher_live" in raw or "drop_live" in raw:
            catcher_live = _bool("catcher_live")
            drop_live = _bool("drop_live")
        elif "live" in raw:
            v = raw.get("live")
            if isinstance(v, bool):
                # v1 migration (W1): the DROP is the only booker that existed
                # under v1, so a v1 `live:true` arms the drop only. The catcher is
                # a NEW booker — arming it silently would run a second live booker
                # the owner never consented to, so it stays OFF until an explicit
                # /catcher on. (The live box is `live:false`, so this is defence.)
                drop_live = v
                catcher_live = False
            else:
                _bad("live", v)
                catcher_live = drop_live = False
        else:
            catcher_live = drop_live = False

        # Cross-field invariant: an inverted flat window can't come from the
        # command path (_parse_window always replaces BOTH ends) but a hand-edit
        # can, and it would make the window admit nothing, silently.
        if (times["earliest"] is not None and times["latest"] is not None
                and times["earliest"] >= times["latest"]):
            _bad("window", f"{times['earliest']}-{times['latest']}")
            times["earliest"] = times["latest"] = None

        # Rules (schema v2). If the doc carries an explicit `rules` key, parse it
        # (tolerant — a bad rule is dropped + degrades the doc, like any field).
        # Otherwise MIGRATE the flat v1 window into a single rule iff any of the
        # three flat fields is non-default; an all-default v1 doc → () (fully
        # permissive), identical to today.
        if "rules" in raw:
            rules = _parse_rules(raw.get("rules"), _bad)
        elif days or times["earliest"] is not None or times["latest"] is not None:
            rules = (Rule(days=days, earliest=times["earliest"],
                          latest=times["latest"]),)
        else:
            rules = ()

        # Version handling. A MISSING version is not an error — it defaults
        # silently like every other absent field (a partial/hand-edited doc must
        # stay usable, §1.4). Only a version we can't honour degrades the doc:
        #  - a NEWER schema (> ours) may have changed field meanings — refuse to
        #    guess, so force dry-run.
        #  - a malformed version (non-int / bool) is corruption.
        version = raw.get("version")
        if version is None:
            version = SCHEMA_VERSION                      # absent ⇒ default, OK
        elif not isinstance(version, int) or isinstance(version, bool):
            _bad("version", version)                     # malformed ⇒ degraded
            version = SCHEMA_VERSION
        elif version > SCHEMA_VERSION:
            _bad("version", version)                     # newer ⇒ degraded

        degraded = tuple(dict.fromkeys(bad_fields))       # de-dup, keep order
        if degraded:
            catcher_live = drop_live = False   # refuse to book on a half-read doc

        return cls(
            centres=centres, rules=rules,
            slot_length_hours=length, weekly_cap=cap, max_holds=holds,
            catcher_live=catcher_live, drop_live=drop_live,
            updated_at=raw.get("updated_at") or None,
            updated_by=raw.get("updated_by") or d.updated_by,
            version=version, degraded=degraded,
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "centres": list(self.centres),
            # Flat mirror kept for a tolerant/legacy reader; DERIVED from rules.
            "days": list(self.days),
            "earliest": self.earliest,
            "latest": self.latest,
            "rules": [{"days": list(r.days), "earliest": r.earliest,
                       "latest": r.latest} for r in self.rules],
            "slot_length_hours": self.slot_length_hours,
            "weekly_cap": self.weekly_cap,
            "max_holds": self.max_holds,
            "catcher_live": self.catcher_live,
            "drop_live": self.drop_live,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    # -- helpers the readers (catcher / sprinter) need ----------------------

    @property
    def mode(self) -> str:
        return "LIVE" if self.live else "DRY-RUN"

    def allows_day(self, day: str) -> bool:
        """Empty `days` means "any day" (§1.3). Accepts 'Tue'/'Tuesday'."""
        if not self.days:
            return True
        return _DAY_LOOKUP.get(day.strip().lower()) in self.days

    def matching_rule(self, iso_date: str, hhmm: str) -> Rule | None:
        """Highest-priority rule whose days include `iso_date`'s weekday AND
        whose window admits `hhmm`. Empty `rules` ⇒ the permissive `_ANY_RULE`
        (so a non-None return always means "some rule admits this slot"). An
        unparseable time FAILS CLOSED (None), same rule as `allows_time`.

        The time is normalised/validated BEFORE the empty-rules shortcut so a
        default (permissive) config also fails closed on garbage — otherwise the
        shortcut would return `_ANY_RULE` for an unparseable time, admitting it."""
        t = _norm_hhmm(hhmm)
        if t is None:
            log.warning("prefs.matching_rule_malformed", value=hhmm)
            return None
        if not self.rules:
            return _ANY_RULE
        weekday = DAYS[dt.date.fromisoformat(iso_date).weekday()]
        for r in self.rules:
            if r.covers_day(weekday) and r.admits_time(t):
                return r
        return None

    def allows_date(self, iso_date: str) -> bool:
        """Does ANY rule's `days` include this date? Empty rules ⇒ True. The
        drop uses this to decide whether to bother with tonight's D+7 at all."""
        if not self.rules:
            return True
        weekday = DAYS[dt.date.fromisoformat(iso_date).weekday()]
        return any(r.covers_day(weekday) for r in self.rules)

    def allows_date_time(self, iso_date: str, hhmm: str) -> bool:
        """A (date, time) is bookable iff some rule admits it (day AND window)."""
        return self.matching_rule(iso_date, hhmm) is not None

    def _rule_for_date(self, iso_date: str) -> Rule | None:
        """Highest-priority rule matching `iso_date` by DAY only (window ignored),
        for the drop to pick its window."""
        if not self.rules:
            return None
        weekday = DAYS[dt.date.fromisoformat(iso_date).weekday()]
        for r in self.rules:
            if r.covers_day(weekday):
                return r
        return None

    def earliest_for_date(self, iso_date: str) -> str | None:
        r = self._rule_for_date(iso_date)
        return r.earliest if r is not None else None

    def latest_for_date(self, iso_date: str) -> str | None:
        r = self._rule_for_date(iso_date)
        return r.latest if r is not None else None

    def allows_time(self, hhmm: str) -> bool:
        """Day-agnostic window check against the flat (sole-rule) window. Kept
        for backward compatibility; the catcher uses `allows_date_time`.

        Comparison is lexicographic, correct only for ZERO-PADDED HH:MM, so we
        normalise here and FAIL CLOSED on anything we can't parse."""
        t = _norm_hhmm(hhmm)
        if t is None:
            log.warning("prefs.allows_time_malformed", value=hhmm)
            return False                            # unparseable ⇒ don't book
        return _window_admits(self.earliest, self.latest, t)

    def window_text(self) -> str:
        return _window_text(self.earliest, self.latest)

    def _rules_segment(self) -> str:
        """The days/window chunk of the summary. For 0–1 rules it reads exactly
        as v1 ("any day · any time" / "Tue,Thu · 18:00–any"); for ≥2 rules it
        renders the ordered list compactly."""
        if len(self.rules) >= 2:
            return "; ".join(rule_text(r) for r in self.rules)
        days_part = ",".join(self.days) if self.days else "any day"
        return f"{days_part} · {self.window_text()}"

    def summary(self) -> str:
        """One-line "whole picture" line appended to every config change reply
        (API_SPEC §2.2), leading with the mode (§8.8: mode always surfaced)."""
        base = (f"{self.mode} · {'+'.join(self.centres)} · "
                f"{self._rules_segment()} · {self.slot_length_hours}h · "
                f"cap {self.weekly_cap} · holds {self.max_holds} · "
                f"catcher {'LIVE' if self.catcher_live else 'DRY'} · "
                f"drop {'LIVE' if self.drop_live else 'DRY'}")
        if self.degraded:
            # Must be loud: a degraded document has already forced DRY-RUN, and
            # the owner needs to know booking is paused and why (§8.9).
            base += f"  ⚠️ UNREADABLE: {','.join(self.degraded)} — booking paused"
        return base


def _canonical_days(days: Iterable[str]) -> tuple[str, ...]:
    """Normalise to canonical Mon..Sun order, de-duplicated."""
    wanted = {_DAY_LOOKUP[str(x).strip().lower()] for x in days}
    return tuple(d for d in DAYS if d in wanted)


def rule_text(r: Rule) -> str:
    """Human-readable "Tue,Thu 18:00–any" for a single rule."""
    days_part = ",".join(r.days) if r.days else "any day"
    return f"{days_part} {_window_text(r.earliest, r.latest)}"


def _parse_one_rule(raw) -> Rule | None:
    """Tolerant parse of a single rule dict → `Rule`, or None if unusable (bad
    day token, malformed/unpadded time, or inverted window)."""
    if not isinstance(raw, dict):
        return None
    raw_days = raw.get("days", ())
    if raw_days in (None, (), []):
        days: tuple[str, ...] = ()
    elif (isinstance(raw_days, (list, tuple)) and all(
            isinstance(x, str) and x.strip().lower() in _DAY_LOOKUP
            for x in raw_days)):
        days = _canonical_days(raw_days)
    else:
        return None

    def _time(v):
        if v is None:
            return True, None
        if isinstance(v, str) and _HHMM.match(v):
            return True, v
        return False, None

    ok_e, earliest = _time(raw.get("earliest"))
    ok_l, latest = _time(raw.get("latest"))
    if not ok_e or not ok_l:
        return None
    if earliest is not None and latest is not None and earliest >= latest:
        return None                                   # inverted window
    return Rule(days=days, earliest=earliest, latest=latest)


def _parse_rules(raw_rules, _bad) -> tuple[Rule, ...]:
    """Parse the `rules` list tolerantly: each bad rule is dropped and degrades
    the document (fail-safe), but one bad rule never discards the good ones."""
    if not isinstance(raw_rules, (list, tuple)):
        _bad("rules", raw_rules)
        return ()
    out: list[Rule] = []
    for i, r in enumerate(raw_rules):
        parsed = _parse_one_rule(r)
        if parsed is None:
            _bad(f"rules[{i}]", r)
            continue
        out.append(parsed)
    return tuple(out)


# --------------------------------------------------------------------------
# Validation (API_SPEC §1.3) — pure, raises PrefsError with a helpful message
# --------------------------------------------------------------------------

def parse_time(value: str, label: str = "time") -> str:
    v = str(value).strip()
    if not _HHMM.match(v):
        raise PrefsError(f"'{value}' isn't a valid {label} — use 24h HH:MM, "
                         f"e.g. 18:00.")
    return v


def parse_days(tokens: Sequence[str]) -> tuple[str, ...]:
    """'Tue Thu' / 'tue,thu' / 'any' → canonical tuple ('' = any)."""
    flat = []
    for tok in tokens:
        flat.extend(t for t in str(tok).replace(",", " ").split() if t)
    if not flat or len(flat) == 1 and flat[0].lower() in ("any", "all", "*"):
        return ()
    unknown = [t for t in flat if t.strip().lower() not in _DAY_LOOKUP]
    if unknown:
        raise PrefsError(f"Unknown day(s): {', '.join(unknown)}. Use "
                         f"{'/'.join(DAYS)} — or 'any'.")
    return _canonical_days(flat)


def validate(prefs: Prefs, valid_centres: Iterable[str] | None = None) -> None:
    """Raise PrefsError if `prefs` violates §1.3. Called on the *candidate*
    config before it is saved, never on the live one."""
    if not prefs.centres:
        raise PrefsError("Pick at least one centre, e.g. /centres paddington.")
    if valid_centres is not None:
        known = list(valid_centres)
        unknown = [c for c in prefs.centres if c not in known]
        if unknown:
            raise PrefsError(f"Unknown centre(s): {', '.join(unknown)}. "
                             f"Configured: {', '.join(known)}.")
    for field in ("earliest", "latest"):
        val = getattr(prefs, field)
        if val is not None:
            parse_time(val, field)
    if (prefs.earliest is not None and prefs.latest is not None
            and prefs.earliest >= prefs.latest):
        raise PrefsError(f"Window start must be before end — got "
                         f"{prefs.earliest}-{prefs.latest}.")
    # `type(...) is not int` for the same bool-is-int reason as from_dict: a
    # plain `not in (1, 2)` accepts True (== 1) and 2.0 (== 2). Both paths that
    # can produce a document must enforce this, or the read path's guard is
    # just bypassed by the write path.
    if (type(prefs.slot_length_hours) is not int
            or prefs.slot_length_hours not in (1, 2)):
        raise PrefsError("Slot length must be 1 or 2 hours.")
    if (not isinstance(prefs.weekly_cap, int) or isinstance(prefs.weekly_cap, bool)
            or prefs.weekly_cap < 0):
        raise PrefsError("Weekly cap must be a whole number ≥ 0 "
                         "(0 pauses booking).")
    if (not isinstance(prefs.max_holds, int) or isinstance(prefs.max_holds, bool)
            or prefs.max_holds < 0):
        raise PrefsError("Max holds must be a whole number ≥ 0 "
                         "(0 pauses booking).")
    for flag in ("catcher_live", "drop_live"):
        if not isinstance(getattr(prefs, flag), bool):
            raise PrefsError(f"{flag} must be true or false.")
    # Each rule must have valid days and a non-inverted window (belt-and-braces:
    # from_dict drops bad rules and /rule validates before saving, but a direct
    # construction must not be able to persist a rule the readers can't honour).
    for i, r in enumerate(prefs.rules, 1):
        for d in r.days:
            if d not in DAYS:
                raise PrefsError(f"Rule #{i} has an invalid day: {d}.")
        for bound in (r.earliest, r.latest):
            if bound is not None:
                parse_time(bound, "rule window")
        if (r.earliest is not None and r.latest is not None
                and r.earliest >= r.latest):
            raise PrefsError(f"Rule #{i} window start must be before end — got "
                             f"{r.earliest}-{r.latest}.")


def known_centres() -> tuple[str, ...]:
    """Target keys from config/targets.yaml, or () if it can't be read.

    ⚠️ `()` means "unknown", NOT "anything goes". Callers must REJECT a centre
    change when this is empty rather than skipping the membership check —
    skipping it let arbitrary text be persisted into prefs.json, which then
    broke every future reply that rendered it. Fail closed."""
    try:
        from .config import load_targets
        return tuple(load_targets().keys())
    except Exception as e:                     # unreadable config must not brick
        log.warning("prefs.targets_unreadable", error=str(e))
        return ()


# --------------------------------------------------------------------------
# Persistence — atomic write, forgiving read
# --------------------------------------------------------------------------

def config_dir(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get(CONFIG_DIR_ENV, "").strip()
    return Path(env) if env else DEFAULT_CONFIG_DIR


def prefs_path(config_dir_override: str | Path | None = None) -> Path:
    return config_dir(config_dir_override) / PREFS_FILENAME


def load_prefs(config_dir_override: str | Path | None = None) -> Prefs:
    """Read the active prefs. Never raises (§1.1) — an unattended booker must
    always get a usable document.

    Note the deliberate asymmetry between the two failure paths:

    - **Absent** file ⇒ clean defaults. A fresh box has nothing to misread, and
      §1.4 promises it boots usable.
    - **Present but unreadable** ⇒ defaults *marked degraded*, which forces
      DRY-RUN. Something WAS configured and we can't read it, so we don't know
      the owner's intent — and every constraint default is the permissive one,
      so proceeding would silently widen what the bot may do.
    """
    p = prefs_path(config_dir_override)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return Prefs.defaults()                 # fresh box: nothing to misread
    except Exception as e:
        log.warning("prefs.unreadable", path=str(p), error=str(e))
        return replace(Prefs.defaults(), degraded=("<unreadable file>",))
    return Prefs.from_dict(raw)


def save_prefs(prefs: Prefs, config_dir_override: str | Path | None = None,
               updated_by: str = "telegram",
               now: dt.datetime | None = None) -> Prefs:
    """Persist atomically and return the stamped document.

    temp-file + `os.replace` in the SAME directory: `os.replace` is atomic on
    POSIX within a filesystem, so a reader mid-write sees the old file or the
    new one, never a half-written one. (A plain `write_text` truncates first —
    that is exactly the torn read we must not hand the sprinter.)
    """
    now = now or dt.datetime.now(LONDON)
    stamped = replace(prefs, updated_at=now.isoformat(timespec="seconds"),
                      updated_by=updated_by, version=SCHEMA_VERSION)
    d = config_dir(config_dir_override)
    d.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(stamped.to_dict(), indent=2) + "\n"

    fd, tmp = tempfile.mkstemp(prefix=".prefs-", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())        # data on disk before the rename
        os.replace(tmp, str(d / PREFS_FILENAME))
        # fsync the DIRECTORY too: the file's bytes are durable after the
        # fsync above, but the rename itself isn't until the directory entry
        # is flushed. Without this a power cut can resurrect the previous
        # prefs.json — and "previous" might be a document with live: true.
        try:
            dfd = os.open(str(d), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:                 # best-effort; not all FSs allow it
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("prefs.saved", path=str(d / PREFS_FILENAME),
             summary=stamped.summary())
    return stamped
