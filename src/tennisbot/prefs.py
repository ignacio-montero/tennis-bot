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

SCHEMA_VERSION = 1
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


class PrefsError(ValueError):
    """An invalid preference update. The message is shown to the owner verbatim
    in Telegram, so it must say what was wrong AND what a good value looks
    like."""


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Prefs:
    """The active preferences (API_SPEC §1.2). Frozen + tuples: an update is a
    new object (`dataclasses.replace`), never a mutation of the live one, so a
    rejected update cannot half-apply."""

    centres: tuple[str, ...] = ("paddington",)
    days: tuple[str, ...] = ()        # () = any day
    earliest: str | None = None       # "HH:MM" inclusive floor (None = none)
    latest: str | None = None         # "HH:MM" exclusive ceiling (None = none)
    slot_length_hours: int = 1
    weekly_cap: int = 3
    live: bool = False
    updated_at: str | None = None
    updated_by: str = "default"
    version: int = SCHEMA_VERSION
    # Fields that failed to parse on read. NON-EMPTY ⇒ this document is only
    # partially understood, so `live` is forced False (API_SPEC §1.4): every
    # constraint field's default is the PERMISSIVE value (no days filter = any
    # day, no earliest = any time, cap back to 3), so falling back to defaults
    # on a *configured* box silently WIDENS what the bot may do. Refusing to
    # book is the only safe direction when we can't read the owner's intent.
    degraded: tuple[str, ...] = ()

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

        live = raw.get("live", d.live)
        if not isinstance(live, bool):
            _bad("live", live)
            live = d.live          # unreadable `live` ⇒ dry-run: fail SAFE

        # Cross-field invariant: an inverted window can't come from the command
        # path (_parse_window always replaces BOTH ends) but a hand-edit can,
        # and it would make allows_time() reject everything, silently.
        if (times["earliest"] is not None and times["latest"] is not None
                and times["earliest"] >= times["latest"]):
            _bad("window", f"{times['earliest']}-{times['latest']}")
            times["earliest"] = times["latest"] = None

        # A newer schema we don't understand: fields may have changed meaning,
        # so treat the whole document as degraded rather than guessing.
        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            _bad("version", version)
            version = SCHEMA_VERSION
        elif version > SCHEMA_VERSION:
            _bad("version", version)

        degraded = tuple(dict.fromkeys(bad_fields))       # de-dup, keep order
        if degraded:
            live = False               # refuse to book on a half-read document

        return cls(
            centres=centres, days=days,
            earliest=times["earliest"], latest=times["latest"],
            slot_length_hours=length, weekly_cap=cap, live=live,
            updated_at=raw.get("updated_at") or None,
            updated_by=raw.get("updated_by") or d.updated_by,
            version=version, degraded=degraded,
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "centres": list(self.centres),
            "days": list(self.days),
            "earliest": self.earliest,
            "latest": self.latest,
            "slot_length_hours": self.slot_length_hours,
            "weekly_cap": self.weekly_cap,
            "live": self.live,
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

    def allows_date(self, iso_date: str) -> bool:
        d = dt.date.fromisoformat(iso_date)
        return self.allows_day(DAYS[d.weekday()])

    def allows_time(self, hhmm: str) -> bool:
        """`earliest` inclusive, `latest` exclusive.

        Comparison is lexicographic, which is only correct for ZERO-PADDED
        HH:MM — "9:00" > "18:00" as strings, so an unpadded time would sail
        through an evening-only filter and book an 08:00 court. That was an
        unenforced precondition on the caller; since the catcher needs a NEW
        week-grid parser (ARCHITECTURE §8.2) that may well emit "9:00", we
        normalise here and FAIL CLOSED on anything we can't parse.
        """
        t = (hhmm or "").strip()
        if len(t) == 4 and t[1] == ":":            # "9:00" -> "09:00"
            t = "0" + t
        if not _HHMM.match(t):
            log.warning("prefs.allows_time_malformed", value=hhmm)
            return False                            # unparseable ⇒ don't book
        if self.earliest is not None and t < self.earliest:
            return False
        if self.latest is not None and t >= self.latest:
            return False
        return True

    def window_text(self) -> str:
        if self.earliest is None and self.latest is None:
            return "any time"
        return f"{self.earliest or 'any'}–{self.latest or 'any'}"

    def summary(self) -> str:
        """One-line "whole picture" line appended to every config change reply
        (API_SPEC §2.2), leading with the mode (§8.8: mode always surfaced)."""
        base = (f"{self.mode} · {'+'.join(self.centres)} · "
                f"{','.join(self.days) if self.days else 'any day'} · "
                f"{self.window_text()} · {self.slot_length_hours}h · "
                f"cap {self.weekly_cap}")
        if self.degraded:
            # Must be loud: a degraded document has already forced DRY-RUN, and
            # the owner needs to know booking is paused and why (§8.9).
            base += f"  ⚠️ UNREADABLE: {','.join(self.degraded)} — booking paused"
        return base


def _canonical_days(days: Iterable[str]) -> tuple[str, ...]:
    """Normalise to canonical Mon..Sun order, de-duplicated."""
    wanted = {_DAY_LOOKUP[str(x).strip().lower()] for x in days}
    return tuple(d for d in DAYS if d in wanted)


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
    if not isinstance(prefs.live, bool):
        raise PrefsError("live must be true or false.")


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
