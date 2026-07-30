"""Cancellation catcher — the D0–D+7 polling booker (ARCHITECTURE §8).

A second booking pathway alongside the midnight drop sprinter: instead of racing
at a known instant, poll the whole open window every 30 min and grab
cancellations that match the owner's shared prefs. Runs as a long-lived,
self-scheduling loop (same shape as `runner.run_drop_loop` / `watchd.run_watchd`).

The module is split hard down the middle so the interesting decisions are pure
and unit-testable with no browser, no network, no clock:

- **Pure logic** (top of the file): the week-grid *parser* (DOM cell dicts →
  `WeekSlot`s), the prefs *matcher* (§8.2 Stage-2 fine filter), the weekly-cap
  *counter* (§4.3), the lapsed-hold *policy* (§4.4), and `plan_cycle`, which
  folds all of them into one decision ("book this / blocked by cap / nothing").
- **IO shell** (bottom): `_PlaywrightScanner` (the only browser code) and
  `run_catcher_loop` (scheduling, blackouts, notifications, state). The loop
  takes an injectable `scanner`/`notifier`, so tests drive the whole cycle with a
  FAKE scanner and never start Chromium.

Detect→book seam (§8.2, verified by the 2026-07-25 spike): the week grid is used
for **detection only**; booking a matched (centre, date, time) re-uses the
EXISTING single-date engine (`runner._run_court` → court grid → `_commit_hold`)
via a targeted `want_time` re-search — no click-through, no re-implemented hold.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from .config import ROOT
from .prefs import load_prefs
from .watchd import in_blackout, next_blackout_end

log = structlog.get_logger()

LONDON = ZoneInfo("Europe/London")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

DEFAULT_INTERVAL_MIN = 30.0
# Don't START a scan this close to a blackout — a full centre scan takes a few
# minutes and must never run INTO the sprinter's / activity jobs' session window
# (§8.4). Cheaper than a mid-scan collision: skip the cycle, resume after.
SCAN_BUDGET_MIN = 10
HEARTBEAT_AT = "09:00"                 # daily "alive" ping, like watchd

CATCHER_STATE_DIR_ENV = "CATCHER_STATE_DIR"
DEFAULT_STATE_DIR = ROOT / ".catcher"
STATE_FILENAME = "catcher-state.json"


def _hm(s: str) -> dt.time:
    h, m = (int(x) for x in s.split(":"))
    return dt.time(h, m)


def _weekday(iso_date: str) -> str:
    return DAYS[dt.date.fromisoformat(iso_date).weekday()]


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ==========================================================================
# 1. Week-grid parser (DETECTION) — pure, no Playwright
# ==========================================================================

@dataclass(frozen=True)
class WeekSlot:
    """One cell of the `mrmResourceStatus.aspx` week grid (recon/FINDINGS.md →
    "Week-view grid"). `state` is the third-state-aware availability."""
    date: str          # ISO yyyy-mm-dd (reformatted from the grid's DD/MM/YYYY)
    time: str          # zero-padded HH:MM
    state: str         # "available" | "unavailable" | "my_booking"
    activity_id: str   # e.g. "156TENNIS2" — the surface, from data-qa-id


# The sturdiest signal per the recon spike: `data-qa-id` encodes ActivityID +
# date + time + availability independently of column position, so no fragile
# header-index→date mapping. Parse THAT (not innerText — the cell text is empty).
_QA_RE = re.compile(
    r"ActivityID=(?P<act>\S+)\s+"
    r"Date=(?P<d>\d{2})/(?P<m>\d{2})/(?P<y>\d{4})\s+"
    r"(?P<H>\d{2}):(?P<M>\d{2}):\d{2}\s+"
    r"Availability=\s*(?P<av>.*)$"
)


def _classify_state(availability: str, cell: dict) -> str:
    """Map a cell to one of three states. Order matters: "Not Available" contains
    the substring "available", so the negative is tested first."""
    v = (availability or "").strip().lower()
    if "my booking" in v:
        return "my_booking"
    if v.startswith("not available"):
        return "unavailable"
    if v.startswith("available"):
        return "available"
    # Fallback to the four agreeing signals when data-qa-id lacks a clear value
    # (the recon spike could not capture the exact "My Booking" markup, so cross-
    # check the input value / td class / disabled attr).
    val = (cell.get("value") or "").strip().lower()
    tdcls = (cell.get("tdcls") or "").lower()
    if "my booking" in val:
        return "my_booking"
    if cell.get("disabled") or "itemnotavailable" in tdcls \
            or val.startswith("not available"):
        return "unavailable"
    if "itemavailable" in tdcls or val.startswith("available"):
        return "available"
    return "unavailable"                # fail closed: unknown ⇒ not bookable


def parse_week_cells(cells) -> list[WeekSlot]:
    """Parse the raw cell dicts extracted from the week grid into `WeekSlot`s.

    Each `cell` is a dict with keys `qa` (the data-qa-id), and optionally
    `value` / `tdcls` / `disabled` (the corroborating signals). Cells whose
    data-qa-id doesn't parse are skipped, not fatal — a stray control never
    wedges a scan."""
    out: list[WeekSlot] = []
    for cell in cells:
        qa = (cell.get("qa") if isinstance(cell, dict) else "") or ""
        m = _QA_RE.search(qa)
        if not m:
            continue
        iso = f"{m['y']}-{m['m']}-{m['d']}"          # DD/MM/YYYY → ISO
        hhmm = f"{m['H']}:{m['M']}"
        state = _classify_state(m["av"], cell if isinstance(cell, dict) else {})
        out.append(WeekSlot(date=iso, time=hhmm, state=state,
                            activity_id=m["act"]))
    return out


# ==========================================================================
# 2. Matcher (§8.2 Stage-2 fine filter) — pure
# ==========================================================================

@dataclass(frozen=True)
class Candidate:
    centre: str
    date: str
    time: str


def slot_key(centre: str, date: str, time: str) -> str:
    return f"{centre}|{date}|{time}"


def _rule_priority(prefs, date: str, time: str) -> int | None:
    """Delegates to `Prefs.rule_priority` — kept as a module name for readers,
    but the computation now lives on `Prefs` so `window_source.RulesSource` shares
    it (the parity guarantee for rules mode)."""
    return prefs.rule_priority(date, time)


def match_candidates(slots_by_centre: dict[str, list[WeekSlot]],
                     prefs, now: dt.datetime, *, source=None) -> list[Candidate]:
    """The exact per-window predicate over the week grid, after EA's coarse
    server-side cut. Returns bookable (free) candidates ordered by **priority
    key** (rule index in rules mode; weekend-first in calendar mode), then
    earliest date, then earliest time, with centre priority as a final tiebreak —
    so the loop books the highest-priority wanted slot first.

    `source` is the `WindowSource` (ARCHITECTURE §9.1) that answers "is this
    (date, time) wanted, and at what priority". `None` ⇒ the rules predicate
    (`Prefs.rule_priority`) — the DEFAULT, so rules mode is byte-for-byte today's
    ordering. A calendar source supplies calendar windows + weekend-first keys.
    WHERE (centres) always comes from prefs, never the source (§9.5).

    Only `state == "available"` cells are candidates: a `my_booking` cell is
    already held/paid, so excluding it here is the grid-level idempotency check
    (§8.2 — the grid surfaces existing holds directly)."""
    priority_for = (source.priority_for if source is not None
                    else (lambda d, t: prefs.rule_priority(d, t)))
    centre_order = list(prefs.centres)
    centre_prio = {c: i for i, c in enumerate(centre_order)}
    today = now.date().isoformat()
    now_hhmm = now.strftime("%H:%M")
    ranked: list[tuple] = []
    for centre in centre_order:
        for ws in slots_by_centre.get(centre, []):
            if ws.state != "available":
                continue
            rp = priority_for(ws.date, ws.time)
            if rp is None:               # no window admits this (day AND time)
                continue
            if ws.date == today and ws.time <= now_hhmm:
                continue                 # already in the past today
            ranked.append((rp, ws.date, ws.time,
                           centre_prio.get(centre, 999),
                           Candidate(centre, ws.date, ws.time)))
    ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    return [t[4] for t in ranked]


# ==========================================================================
# 3. Weekly cap (§4.3) — pure. Counts PAID court bookings, Mon-reset week,
#    activity jobs excluded.
# ==========================================================================

def week_dates_for(d: dt.date) -> list[dt.date]:
    """The Mon…Sun of the week containing `d` (Monday reset, not rolling 7)."""
    monday = d - dt.timedelta(days=d.weekday())
    return [monday + dt.timedelta(days=i) for i in range(7)]


def _is_activity(text: str, activity_matches) -> bool:
    """Is this Manage-Bookings row a coaching-activity commitment (Wed/Sun) —
    which lives in a SEPARATE budget and is never a court? Same `match in text`
    idiom `has_booking` uses."""
    return any(m and m in text for m in activity_matches)


# Court identification differs BY PURPOSE, because the safe fail-direction does
# (C1). Idempotency (`held_court_date_keys`) and the holds ceiling
# (`count_unpaid_holds`) are safe when they OVER-count — a spuriously-counted
# booking only makes the bot SKIP a date / hold off, never double-book — so they
# use the generous NEGATIVE rule (any non-activity booking is a "court"). Only
# the PAID weekly cap wants precision (don't let a paid swim eat the court
# budget, Q2c), so it alone uses positive court-ID.

def held_court_date_keys(bookings, activity_matches) -> set:
    """(day, month-abbr) keys that ALREADY have a court booking — held OR paid —
    per Manage Bookings. The AUTHORITATIVE idempotency source, used instead of
    trusting the week grid's `my_booking` cell (whose exact markup the recon
    spike could not confirm — if EA renders a self-held slot as `Available`, a
    grid-only guard would re-book it every cycle: a hold storm).

    Per-date because `my_bookings` carries no time (one court per day is the
    intent). **Fail-SAFE by OVER-counting:** any non-activity booking is treated
    as a court, because the failure direction here is to SKIP a date (never
    double-book). Positive court-ID is deliberately NOT used — a surface-token
    miss would UNDER-count and re-book a slot we already hold (a hold storm),
    the unsafe direction."""
    keys = set()
    for b in bookings:
        text = b.get("text", "") or ""
        if _is_activity(text, activity_matches):
            continue                     # an activity commitment, not a court
        day, mon = b.get("day"), b.get("mon")
        if day is not None and mon:
            keys.add((day, mon))
    return keys


def count_paid_court_bookings(bookings, week_dates, activity_matches,
                              court_matches=()) -> int:
    """Paid COURT bookings whose play date falls in `week_dates` (§4.3).

    `bookings` is `EveryoneActiveProvider.my_bookings` output (dicts with
    `paid`/`day`/`mon`/`text`). Here PRECISION is the goal (Q2c): a paid swim/gym
    must NOT eat the weekly court budget, so court hire is identified POSITIVELY
    by a configured surface token. Coaching activities are always excluded.

    Fallback: when the court-token set is empty/unreadable we can't positively
    ID a court, so we use the negative rule (paid non-activity) — which
    OVER-counts (a false "cap reached" ⇒ skip a winnable court, never over-book),
    the safe direction. A positive < negative divergence is logged loudly, since
    on the box it flags a surface-token drift (a real court whose text lost its
    token) — the one case where positive-ID could under-count the cap."""
    wanted = {(d.day, _MON[d.month - 1]) for d in week_dates}
    in_week = [b for b in bookings
               if b.get("paid")
               and (b.get("day"), b.get("mon")) in wanted
               and not _is_activity(b.get("text", "") or "", activity_matches)]
    negative = len(in_week)
    if not court_matches:
        return negative                  # safe fallback: over-count
    positive = sum(
        1 for b in in_week
        if any(m and m in (b.get("text", "") or "") for m in court_matches))
    if positive < negative:
        log.warning("catcher.court_token_divergence",
                    positive=positive, negative=negative,
                    detail="a paid non-activity booking lacked a known court "
                           "surface token — surface-token drift on the box, or a "
                           "genuine non-court paid booking (swim/gym)")
    return positive


def count_unpaid_holds(bookings, activity_matches) -> int:
    """Current UNPAID court holds across the whole Manage-Bookings view (Q2a).

    The concurrent-holds ceiling (`max_holds`) caps how many unpaid holds the
    catcher may have outstanding at once, SEPARATE from the paid weekly cap.
    **Fail-SAFE by OVER-counting:** any unpaid non-activity booking counts,
    because over-counting only makes the ceiling trip SOONER (hold off), never
    over-book. Positive court-ID is deliberately NOT used — a token miss would
    UNDER-count and yield a ceiling that never trips (the unsafe direction)."""
    n = 0
    for b in bookings:
        if b.get("paid"):
            continue
        if _is_activity(b.get("text", "") or "", activity_matches):
            continue
        n += 1
    return n


# ==========================================================================
# 4. Lapsed-hold policy (§4.4) — pure. Per-slot memory across cycles.
# ==========================================================================

@dataclass(frozen=True)
class RebookDecision:
    should_book: bool
    phase: str        # "first" | "overnight" | "daytime" | "released"
    reason: str


def classify_hold(first_held: dt.datetime) -> str:
    """Daytime = FIRST held 09:00–23:00 London; else overnight (§4.4)."""
    return "daytime" if 9 <= first_held.hour < 23 else "overnight"


def _next_9am(first_held: dt.datetime) -> dt.datetime:
    """The first 09:00 strictly after an overnight first-hold — the moment the
    'persist every cycle' rule hands over to the daytime rule."""
    base = first_held.replace(hour=9, minute=0, second=0, microsecond=0)
    if first_held.hour >= 9:             # e.g. first held 23:30 → next day 09:00
        base += dt.timedelta(days=1)
    return base


def should_rebook(entry: dict | None, now: dt.datetime) -> RebookDecision:
    """Should we (re-)book this slot now, given its memory? (§4.4)

    - No memory  → book it (this is the first hold, not a re-book).
    - Daytime hold (first held 09:00–23:00) → re-book at most once, then release.
    - Overnight hold (first held after 23:00) → re-book every cycle until the
      next 09:00, then the daytime rule takes over (one more, then release).

    Only ever consulted when the grid shows the slot FREE again (a My-Booking
    cell is skipped upstream), so 'should we re-book' == 'our earlier hold
    lapsed and we haven't paid'."""
    if entry is None:
        return RebookDecision(True, "first", "never held before")
    first = dt.datetime.fromisoformat(entry["first_held"])
    daytime_rebooks = int(entry.get("daytime_rebooks", 0))
    if classify_hold(first) == "overnight" and now < _next_9am(first):
        return RebookDecision(True, "overnight", "overnight persist until 09:00")
    # daytime rule (native daytime, or an overnight hold now past 09:00)
    if daytime_rebooks < 1:
        return RebookDecision(True, "daytime", "daytime re-book (at most once)")
    return RebookDecision(False, "released", "released for the day")


def record_hold(entry: dict | None, now: dt.datetime, phase: str) -> dict:
    """Update per-slot memory after a (would-)book. Pure: returns a new dict.

    `overnight_rebooks` and `daytime_rebooks` are tracked separately because the
    overnight→daytime handover needs to allow exactly one daytime re-book
    regardless of how many overnight re-books preceded it."""
    stamp = now.isoformat(timespec="seconds")
    if phase == "first" or entry is None:
        return {"first_held": stamp, "overnight_rebooks": 0,
                "daytime_rebooks": 0, "last_held": stamp}
    e = dict(entry)
    if phase == "overnight":
        e["overnight_rebooks"] = int(e.get("overnight_rebooks", 0)) + 1
    elif phase == "daytime":
        e["daytime_rebooks"] = int(e.get("daytime_rebooks", 0)) + 1
    e["last_held"] = stamp
    return e


# ==========================================================================
# 5. plan_cycle — fold everything into one decision. Pure.
# ==========================================================================

@dataclass(frozen=True)
class CyclePlan:
    to_book: Candidate | None      # the slot to (would-)book this cycle, or None
    candidate: Candidate | None    # the matched bookable slot (set even if blocked)
    phase: str                     # lapsed-hold phase for `to_book`
    reason: str                    # "book" | "cap_reached" | "hold_ceiling_reached"
                                   #  | "no_bookable"
    paid: int = 0                  # paid count for the candidate's week
    holds: int = 0                 # concurrent unpaid holds at decision time


def plan_cycle(slots_by_centre, prefs, memory, bookings, activity_matches,
               now: dt.datetime, court_matches=(), *, source=None) -> CyclePlan:
    """One cycle's decision, from grid + prefs + memory + bookings + clock.

    Walks candidates in priority order and books the first one that is (a) not
    already held/paid on that date, (b) permitted by the lapsed-hold policy,
    (c) below the concurrent-UNPAID-holds ceiling (`max_holds`), and (d) in a
    week under its paid cap. A capped candidate does NOT stop the walk — a LATER
    candidate in a different, un-capped week must still be reachable (the D0–D+7
    scan straddles Monday). The holds ceiling, by contrast, is a global stop: it
    is checked at the FIRST genuinely-bookable candidate, so the top-priority
    slot is the one held off (candidates are already in priority order).
    `cap_reached`/`hold_ceiling_reached` are reported only when the block was the
    sole reason nothing was booked.

    `court_matches` is used ONLY by the PAID cap (positive court-ID, Q2c);
    idempotency and the holds ceiling use the fail-SAFE negative rule (C1)."""
    held = held_court_date_keys(bookings, activity_matches)
    holds = count_unpaid_holds(bookings, activity_matches)
    capped_example: Candidate | None = None
    capped_paid = 0
    for c in match_candidates(slots_by_centre, prefs, now, source=source):
        cdate = dt.date.fromisoformat(c.date)
        if (cdate.day, _MON[cdate.month - 1]) in held:
            continue                     # authoritative idempotency: already ours
        dec = should_rebook(memory.get(slot_key(c.centre, c.date, c.time)), now)
        if not dec.should_book:
            continue                     # released for the day — try the next
        # Concurrent-holds ceiling (Q2a): a global stop, checked before the cap
        # so the highest-priority bookable slot is the one we hold off on.
        if holds >= prefs.max_holds:
            paid = count_paid_court_bookings(bookings, week_dates_for(cdate),
                                             activity_matches, court_matches)
            return CyclePlan(None, c, "", "hold_ceiling_reached", paid, holds)
        paid = count_paid_court_bookings(bookings, week_dates_for(cdate),
                                         activity_matches, court_matches)
        if paid >= prefs.weekly_cap:
            if capped_example is None:   # remember the first, for the notice
                capped_example, capped_paid = c, paid
            continue                     # a later week may be un-capped
        return CyclePlan(c, c, dec.phase, dec.reason, paid, holds)
    if capped_example is not None:
        return CyclePlan(None, capped_example, "", "cap_reached", capped_paid,
                         holds)
    return CyclePlan(None, None, "", "no_bookable", 0, holds)


# ==========================================================================
# 6. State store (§8.5/§8.7) — mutable JSON, bracket.json idiom, single writer.
# ==========================================================================

def state_dir(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get(CATCHER_STATE_DIR_ENV, "").strip()
    return Path(env) if env else DEFAULT_STATE_DIR


def load_state(override: str | Path | None = None) -> dict:
    """Never raises — an unattended loop must always get a usable doc."""
    p = state_dir(override) / STATE_FILENAME
    try:
        state = json.loads(p.read_text())
    except FileNotFoundError:
        return {"slots": {}}
    except Exception as e:
        log.warning("catcher.state_unreadable", path=str(p), error=str(e))
        return {"slots": {}}
    state.setdefault("slots", {})
    return state


def save_state(state: dict, override: str | Path | None = None) -> None:
    d = state_dir(override)
    d.mkdir(parents=True, exist_ok=True)
    (d / STATE_FILENAME).write_text(json.dumps(state, indent=2) + "\n")


def prune_expired_slots(state: dict, today_iso: str) -> None:
    """Drop per-slot memory for past dates. Slot keys are `centre|date|time`
    with ISO dates (which sort lexicographically), and a slot only matters
    inside the D0–D+7 window — so past entries are dead weight that would grow
    catcher-state.json without bound. Pure (takes `today`) so it's testable."""
    slots = state.get("slots", {})
    for key in list(slots):
        parts = key.split("|")
        if len(parts) == 3 and parts[1] < today_iso:
            del slots[key]


# ==========================================================================
# 7. IO shell — Playwright scanner (the ONLY browser code; never run in tests)
# ==========================================================================

class _PlaywrightScanner:
    """Owns the browser + EA Connect session for a catcher run. One session for
    the whole loop; a per-centre provider is cheap (just secrets+target).

    Built lazily by `run_catcher_loop` only when no scanner is injected — tests
    always inject a fake, so Chromium never boots in the suite."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._p = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._session_ready = False
        from .config import Secrets, load_targets
        self.secrets = Secrets.from_env()
        self.targets = load_targets()

    def _ensure_session(self):
        from playwright.sync_api import sync_playwright
        from .providers.everyoneactive import make_context
        if self._p is None:
            self._p = sync_playwright().start()
        if self._browser is None:
            self._browser, self._ctx = make_context(self._p, headless=self.headless)
            self._page = self._ctx.new_page()
            self._session_ready = False
        if not self._session_ready:
            # First establishment for this browser: account-SPA cookies + Connect.
            self._establish_session(full=True)
            return
        # Reused across cycles. The Connect cookie expires after a few hours, so a
        # daemon that logs in ONCE and then only navigates (`go_home`) bounces to
        # MRMLogin forever — the failure mode that silently killed every cycle for
        # 3 days (2026-07-27→30). Cheaply re-affirm the session each cycle and
        # re-auth when it has lapsed, exactly like the nightly drop does.
        if not self._connect_live():
            log.info("catcher.session_reauth")
            self._establish_session(full=False)

    def _establish_session(self, *, full: bool) -> None:
        """(Re)establish the account-wide EA session on the current page.

        `full=True` (first time) also seeds account-level cookies via the flaky
        account SPA (`start_session`). The heal path (`full=False`) skips it and
        relies on `enter_connect`, which — when the Connect cookie is gone — logs
        in directly on MRMLogin (email+password), independent of that SPA. That is
        the robust primary path (CLAUDE.md), so a re-auth never depends on the
        surface most likely to be throttled."""
        from .providers.everyoneactive import EveryoneActiveProvider
        any_target = next(iter(self.targets.values()))
        prov = EveryoneActiveProvider(self.secrets, any_target)
        if full:
            prov.start_session(self._ctx, self._page)
        prov.enter_connect(self._page, self._ctx)
        self._session_ready = True

    def _connect_live(self) -> bool:
        """Liveness probe: is the Connect search home still reachable, or has the
        session expired and bounced us to MRMLogin? Run once per cycle so a
        days-long daemon re-auths instead of failing forever on a stale cookie.
        A True result also leaves the page on the search home, ready to scan."""
        from .providers.everyoneactive import EveryoneActiveProvider
        try:
            self._page.goto(EveryoneActiveProvider.HOME_URL,
                            wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(1000)
            return "memberHomePage" in self._page.url
        except Exception:
            return False

    def scan(self, prefs) -> dict[str, list[WeekSlot]]:
        """~1 search + ≤2 grid opens per centre (§8.2): a ranged search of the
        open window per surface, then read the week grid via `read_week_grid`."""
        from .providers.everyoneactive import EveryoneActiveProvider
        self._ensure_session()
        now = dt.datetime.now(LONDON)
        d0 = now.date().isoformat()
        d7 = (now.date() + dt.timedelta(days=7)).isoformat()
        out: dict[str, list[WeekSlot]] = {}
        for centre in prefs.centres:
            target = self.targets.get(centre)
            if target is None or target.courts is None:
                continue
            prov = EveryoneActiveProvider(self.secrets, target)
            slots: list[WeekSlot] = []
            for surface in target.courts.ordered():
                try:
                    prov.go_home(self._page)
                    prov.search(self._page, site=target.site,
                                group=target.courts.group, activity="",
                                start_date=d0, end_date=d7)
                    prov.open_timetable(self._page, surface.match)
                except (prov.RowFull, prov.RowMissing):
                    continue
                except Exception as e:            # one bad surface ≠ dead cycle
                    log.warning("catcher.scan_surface_failed",
                                centre=centre, surface=surface.label, err=str(e))
                    continue
                slots.extend(prov.read_week_grid(self._page))
            out[centre] = slots
        return out

    def get_bookings(self) -> list[dict]:
        """Manage Bookings, once per cycle — the authoritative paid/held source
        for the weekly cap (§8.6)."""
        from .providers.everyoneactive import EveryoneActiveProvider
        self._ensure_session()
        any_target = next(iter(self.targets.values()))
        prov = EveryoneActiveProvider(self.secrets, any_target)
        return prov.my_bookings(self._page)

    def book(self, centre: str, date: str, time: str, prefs):
        """Book a detected (centre, date, time) via the EXISTING single-date
        engine (§8.2 re-search seam) — no re-implemented hold, no click-through.
        `want_time` targets the exact detected time; `slot_length_hours` is fed
        through by toggling the target's `two_hours` (behaviour-preserving —
        `_run_court`/`choose_court_slots` are unchanged)."""
        from .runner import _NullTelegram, _run_court
        from .providers.everyoneactive import EveryoneActiveProvider
        self._ensure_session()
        target = self.targets[centre]
        two = prefs.slot_length_hours == 2
        target2 = _with_two_hours(target, two)
        prov = EveryoneActiveProvider(self.secrets, target2)
        # Quiet notifier: _run_court would otherwise send its own "nothing
        # booked" message on a race-loss, breaking "empty cycles are silent".
        # The catcher notifies from the RunResult instead (same pattern as
        # runner.run_drop).
        return _run_court(self._page, self._ctx, prov, target2, date,
                          dry_run=not prefs.catcher_live, want_time=time,
                          tg=_NullTelegram())

    def teardown(self):
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._p is not None:
            try:
                self._p.stop()
            except Exception:
                pass
        self._p = self._browser = self._ctx = self._page = None
        self._session_ready = False


def _with_two_hours(target, two_hours: bool):
    """Return a copy of `target` with `courts.two_hours` set to reflect
    `prefs.slot_length_hours`. Behaviour-preserving: it only feeds the existing
    engine a target that mirrors the owner's prefs; the engine is untouched."""
    if target.courts is None or target.courts.two_hours == two_hours:
        return target
    return replace(target, courts=replace(target.courts, two_hours=two_hours))


def _activity_matches() -> tuple[str, ...]:
    """Every configured activity's row-name, for excluding Wed/Sun commitments
    from the paid cap (§4.3). Best-effort — an unreadable config yields ()."""
    try:
        from .config import load_targets
        out: list[str] = []
        for t in load_targets().values():
            if t.activities is not None:
                out.extend(i.match for i in t.activities.items.values())
        return tuple(out)
    except Exception as e:
        log.warning("catcher.activity_matches_unreadable", error=str(e))
        return ()


def _court_matches() -> tuple[str, ...]:
    """Every configured court-surface row-name, for POSITIVELY identifying court
    hire in Manage Bookings for the PAID weekly cap only (Q2c — see
    `count_paid_court_bookings`). Best-effort: an unreadable or surface-less
    config yields (), which triggers that counter's safe negative fallback
    (over-count, not over-book)."""
    try:
        from .config import load_targets
        out: list[str] = []
        for t in load_targets().values():
            if t.courts is not None:
                out.extend(s.match for s in t.courts.surfaces.values())
        return tuple(out)
    except Exception as e:
        log.warning("catcher.court_matches_unreadable", error=str(e))
        return ()


# ==========================================================================
# 8. Notifications
# ==========================================================================

def _maybe_heartbeat(state, now, prefs, bookings, activity_matches, tg,
                     court_matches=()) -> None:
    """One 'alive' ping per day, once we're past 09:00 (§8.9). Leads with the
    mode + config summary + this week's paid count + open holds, so silence
    never means 'dead' and the persisted LIVE/DRY-RUN flags are always visible
    (§8.8)."""
    today = now.date().isoformat()
    if state.get("last_heartbeat") == today or now.time() < _hm(HEARTBEAT_AT):
        return
    state["last_heartbeat"] = today
    paid = count_paid_court_bookings(bookings, week_dates_for(now.date()),
                                     activity_matches, court_matches)
    holds = count_unpaid_holds(bookings, activity_matches)
    tg.send(f"💓 catcher alive {today}.\n{prefs.summary()}\n"
            f"Paid this week: {paid}/{prefs.weekly_cap}. "
            f"Holds: {holds}/{prefs.max_holds}.")


def _maybe_calendar_alert(state, now, source, tg, live: bool = False) -> None:
    """LOUD, rate-limited alert when calendar mode can't READ the calendar (§9.6).

    The catcher runs every 30 min, so this must NOT fire every cycle during an
    outage. It fires on ENTERING the failure state and at most once/day while it
    persists — the same once-per-day discipline as the heartbeat / hold-ceiling
    notice. `calendar_alert_date` is the latch; a successful read CLEARS it (see
    `_run_catcher_cycle`), so a fresh failure the same day re-alerts ("on
    entering"). Booking nothing is handled by the empty window list; this is only
    the shout."""
    today = now.date().isoformat()
    if state.get("calendar_alert_date") == today:
        return                            # already shouted today
    state["calendar_alert_date"] = today
    # Soften the wording in dry-run: nothing was going to be held anyway, so
    # "PAUSED" would over-alarm and train the owner to ignore it (critic S2).
    impact = ("booking is PAUSED, nothing will be held until it's readable"
              if live else
              "no windows this cycle (dry-run — nothing would be held anyway)")
    tg.send(f"🚨 <b>Calendar unreadable</b> (calendar mode) — {impact}.\n"
            f"Reason: {source.failure_reason}\n"
            "Check TENNISBOT_CALENDAR_ICS_URL / the iCloud publish link. "
            "(Never falls back to /rules or 'book everything'.)")


def _fmt_window(w) -> str:
    """One calendar window as 'Sat 02 Aug  18:00–20:00'. Open-ended events read
    'from …' / 'until …' / 'any time' (latest is an EXCLUSIVE ceiling, §9.1)."""
    day = dt.date.fromisoformat(w.date).strftime("%a %d %b")
    if w.earliest and w.latest:
        span = f"{w.earliest}–{w.latest}"
    elif w.earliest:
        span = f"from {w.earliest}"
    elif w.latest:
        span = f"until {w.latest}"
    else:
        span = "any time"
    return f"{day}  {span}"


def _maybe_calendar_preview(state, source, prefs, tg, d0, d7) -> None:
    """Proactively confirm what the CALENDAR maps to for the coming week, so
    calendar mode is never silent about its INTENT (the owner's original ask:
    'tell me what it would book').

    Change-triggered, not periodic. It sends only when the upcoming-week plan
    DIFFERS from the one last sent (persisted as `calendar_plan_sig`). That fires
    at exactly the informative moments — entering calendar mode, editing an event,
    or the rolling 7-day horizon admitting/dropping one — and stays quiet
    otherwise, so it never becomes noise (the daily heartbeat already proves
    liveness). An empty calendar is itself a plan worth stating ONCE ('no events'
    is a signature too), which is precisely the case that left the owner unsure
    anything was working. Only called on a SUCCESSFUL read; the unreadable case is
    the LOUD `_maybe_calendar_alert` (§9.6)."""
    windows = sorted(source.all_windows(d0, d7), key=lambda w: w.priority_key)
    lines = [_fmt_window(w) for w in windows]
    sig = "|".join(lines) if lines else "EMPTY"
    if state.get("calendar_plan_sig") == sig:
        return                                  # unchanged since last sent
    # Latch BEFORE sending (as `_maybe_calendar_alert` does): the send is wrapped
    # so a Telegram hiccup can never abort the cycle and lose booking/state — the
    # cost is at most one missed preview, re-sent when the plan next changes.
    state["calendar_plan_sig"] = sig
    if not lines:
        body = ("📅 <b>Calendar plan</b> — no tennis events in your iCloud "
                "calendar for the next 7 days, so there's nothing to book. Add "
                "events to the Tennis calendar and I'll pick them up.")
    else:
        ranked = "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines, 1))
        booking = ("ON — will book for real" if prefs.catcher_live
                   else "dry-run — /catcher on to book for real")
        body = (f"📅 <b>Calendar plan</b> — next 7 days "
                f"(weekend-first, up to {prefs.weekly_cap}/week):\n{ranked}\n"
                f"Booking: {booking}.")
    try:
        tg.send(body)
    except Exception:
        log.warning("catcher.calendar_preview_send_failed")


def _notify_book(tg, c: Candidate, result, prefs) -> None:
    wd = _weekday(c.date)
    if result.dry_run:
        tg.send(f"🎾 <b>DRY-RUN</b> (catcher) — {c.centre}\n"
                f"✅ Would book <b>{wd} {c.date} {c.time}</b> "
                f"(cancellation caught, no hold).\n{prefs.summary()}")
    else:
        tg.send(f"🎾✅ <b>HELD</b> (catcher) — {c.centre}\n"
                f"<b>{wd} {c.date} {c.time}</b>\n"
                f"💳 Open the Everyone Active app to pay (1-hour hold).")
    if result.screenshot_path:
        try:
            tg.send_photo(result.screenshot_path,
                          caption=f"{c.centre} {c.date} {c.time}")
        except Exception:
            pass


# ==========================================================================
# 9. Per-cycle orchestration (thin; the pure decision is plan_cycle)
# ==========================================================================

def _run_catcher_cycle(scanner, prefs, state, now, tg, activity_matches,
                       court_matches=(), *, calendar_fetch=None) -> CyclePlan:
    bookings = scanner.get_bookings()
    slots_by_centre = scanner.scan(prefs)
    _maybe_heartbeat(state, now, prefs, bookings, activity_matches, tg,
                     court_matches)

    state.setdefault("slots", {})
    prune_expired_slots(state, now.date().isoformat())   # keep memory lean

    # Build the window source fresh each cycle (§9.7 — a phone edit lands next
    # cycle). Rules mode ⇒ `source=None` ⇒ match_candidates uses today's exact
    # rule predicate (parity). Calendar mode ⇒ read the .ics now.
    source = None
    if prefs.mode == "calendar":
        from .window_source import window_source_for
        today = now.date()
        source = window_source_for(prefs, fetch=calendar_fetch, d0=today,
                                   d7=today + dt.timedelta(days=7), now=now)
        if source.read_failed:
            # §9.6: book NOTHING and shout (rate-limited). Never fall back to
            # rules or "book everything". The empty plan below books nothing.
            _maybe_calendar_alert(state, now, source, tg, live=prefs.catcher_live)
            log.warning("catcher.calendar_unreadable",
                        reason=source.failure_reason)
            return CyclePlan(None, None, "", "no_bookable", 0, 0)
        # Read OK ⇒ clear the alert latch so a later failure re-alerts on entry.
        state.pop("calendar_alert_date", None)
        # Proactively confirm the week's plan whenever it CHANGES (§8.12) — so
        # calendar mode isn't silent about what it intends to book.
        _maybe_calendar_preview(state, source, prefs, tg,
                                today, today + dt.timedelta(days=7))

    plan = plan_cycle(slots_by_centre, prefs, state["slots"], bookings,
                      activity_matches, now, court_matches, source=source)

    if plan.reason == "cap_reached" and plan.candidate is not None:
        wk = iso_week(dt.date.fromisoformat(plan.candidate.date))
        if state.get("cap_notified_week") != wk:   # notify once per week (§4.3)
            state["cap_notified_week"] = wk
            tg.send(f"🧢 Weekly cap reached "
                    f"({plan.paid}/{prefs.weekly_cap} paid) — holding off "
                    f"{plan.candidate.centre} {plan.candidate.date} "
                    f"{plan.candidate.time} and later this week.")
        return plan

    if plan.reason == "hold_ceiling_reached" and plan.candidate is not None:
        today = now.date().isoformat()
        if state.get("holds_notified") != today:   # notify once per day (Q2a)
            state["holds_notified"] = today
            tg.send(f"✋ Hold ceiling reached "
                    f"({plan.holds}/{prefs.max_holds} unpaid holds) — holding "
                    f"off {plan.candidate.centre} {plan.candidate.date} "
                    f"{plan.candidate.time} until you pay or a hold lapses.")
        return plan

    if plan.to_book is None:
        log.info("catcher.cycle_empty", reason=plan.reason)   # silent to owner
        return plan

    c = plan.to_book
    result = scanner.book(c.centre, c.date, c.time, prefs)
    key = slot_key(c.centre, c.date, c.time)
    if result is not None and result.chosen is not None:
        state["slots"][key] = record_hold(state["slots"].get(key), now, plan.phase)
        log.info("catcher.booked", centre=c.centre, date=c.date, time=c.time,
                 phase=plan.phase, dry_run=result.dry_run)
        # Notify AFTER the memory is recorded, and never let a Telegram failure
        # propagate — otherwise the outer save_state is skipped and this hold's
        # memory is lost, so the next lapse wrongly treats it as a first hold.
        try:
            _notify_book(tg, c, result, prefs)
        except Exception as e:
            log.warning("catcher.notify_failed", err=str(e))
    else:
        # Race loss: the slot went before we could hold it. Nothing held, so
        # don't touch memory — and stay silent (an empty result is not news).
        log.info("catcher.book_missed", centre=c.centre, date=c.date, time=c.time)
    return plan


# ==========================================================================
# 10. The self-scheduling loop
# ==========================================================================

def run_catcher_loop(*, interval_min: float = DEFAULT_INTERVAL_MIN,
                     max_cycles: int | None = None, notify: bool = True,
                     headless: bool = True, config_dir=None, state_dir_override=None,
                     scanner=None, notifier=None,
                     now_fn=None, sleep_fn=None, calendar_fetch=None) -> None:
    """Poll D0–D+7 every `interval_min`, booking matched cancellations (§8.1).

    Self-scheduling like `run_drop_loop`: sleep → act → loop, with each cycle's
    body wrapped so one bad cycle logs, notifies, and continues rather than
    killing the loop (PRD §7). Skips (and yields the EA session during) blackouts
    inherited from watchd (§8.4). Reads prefs fresh every cycle, so a phone
    change lands next cycle; books only when `prefs.catcher_live` (else dry-run).

    Injection seams for tests: `scanner` (a fake replaces `_PlaywrightScanner`),
    `notifier`, `now_fn`/`sleep_fn`, and `calendar_fetch` (a fake `.ics` fetch for
    calendar mode, §9) — so the whole loop, including calendar mode, runs
    offline with no browser and no network."""
    now_fn = now_fn or (lambda: dt.datetime.now(LONDON))
    sleep_fn = sleep_fn or time.sleep

    if notifier is None:
        if notify:
            from .config import Secrets
            from .notify.telegram import Telegram
            s = Secrets.from_env()
            notifier = Telegram(s.telegram_bot_token, s.telegram_chat_id)
        else:
            from .runner import _NullTelegram
            notifier = _NullTelegram()

    owns_scanner = scanner is None
    activity_matches = _activity_matches()
    court_matches = _court_matches()
    state = load_state(state_dir_override)

    log.info("catcher.up", interval_min=interval_min, notify=notify,
             max_cycles=max_cycles)

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        now = now_fn()

        # Never START a scan in — or about to run into — a blackout (§8.4).
        if in_blackout(now) or in_blackout(now + dt.timedelta(minutes=SCAN_BUDGET_MIN)):
            end = next_blackout_end(now)
            if owns_scanner and scanner is not None:
                try:
                    scanner.teardown()
                except Exception:
                    pass
                scanner = None
            log.info("catcher.blackout", until=end.isoformat())
            if max_cycles is not None and cycles >= max_cycles:
                break
            sleep_fn(max(5.0, (end - now).total_seconds() + 5.0))
            continue

        try:
            prefs = load_prefs(config_dir)
            if scanner is None:
                scanner = _PlaywrightScanner(headless=headless)
            _run_catcher_cycle(scanner, prefs, state, now, notifier,
                               activity_matches, court_matches,
                               calendar_fetch=calendar_fetch)
            save_state(state, state_dir_override)
        except Exception as e:            # one bad cycle must never kill the loop
            log.error("catcher.cycle_failed", err=str(e))
            try:
                notifier.send(f"⚠️ catcher cycle failed — {e}")
            except Exception:
                pass
            if owns_scanner and scanner is not None:
                try:
                    scanner.teardown()
                except Exception:
                    pass
                scanner = None

        if max_cycles is not None and cycles >= max_cycles:
            break
        sleep_fn(interval_min * 60.0)

    if owns_scanner and scanner is not None:
        try:
            scanner.teardown()
        except Exception:
            pass
    log.info("catcher.done", cycles=cycles)
