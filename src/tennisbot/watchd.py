"""watchd — 24/7 drop-time hunter daemon (read-only, never books).

Runs forever on an always-on host. Each cycle it reads availability for the
two boundary dates (today+7, today+8) and logs the result; the moment a date
flips closed→open after a sustained-closed baseline it records the bracket
[last_closed, first_open] and pings Telegram. Because it covers the whole day
it settles the midnight-vs-evening question in a single day, then narrows:

- COARSE cadence all day (default 20 min) → day 1 gives a ≤20-min bracket.
- FINE cadence (default 20 s) inside "hot windows": the static evening window
  (~21:35–22:15, current best theory) plus an automatic window centred on the
  last recorded bracket — so each successive night tightens the estimate
  without any manual retuning.
- BLACKOUTS around the Mac's activity launchd jobs (Wed 19:00/20:30,
  Sun 13:00/14:30): the browser is closed and no polling happens — never two
  concurrent sessions on the account.

State (JSONL observations + bracket.json) persists across restarts under
WATCHD_STATE_DIR (default <repo>/.watchd). All clock decisions use
Europe/London regardless of host timezone.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from .config import ROOT, Secrets, load_targets

log = structlog.get_logger()

LONDON = ZoneInfo("Europe/London")

STATE_DIR = Path(os.environ.get("WATCHD_STATE_DIR", ROOT / ".watchd"))

# Sustained-closed reads required before an "open" counts as a real drop
# (guards against flaky reads and against starting mid-drop).
REQUIRE_CLOSED = 3

# Blackouts around the Mac activity jobs (job start ± margin). Never poll —
# and never hold a live Connect session — inside these.
DEFAULT_BLACKOUTS = [  # (weekday 0=Mon, "HH:MM", "HH:MM")
    (2, "18:50", "19:25"),   # Wed primary 19:00
    (2, "20:20", "20:55"),   # Wed backup  20:30
    (6, "12:50", "13:25"),   # Sun primary 13:00
    (6, "14:20", "14:55"),   # Sun backup  14:30
]

DEFAULT_HOT_WINDOWS = ["21:35-22:15"]  # evening ~21:50 theory
HEARTBEAT_AT = "09:00"                 # daily "I'm alive" Telegram summary


# --------------------------------------------------------------------------
# Pure logic (unit-testable, no Playwright)
# --------------------------------------------------------------------------

def _hm(s: str) -> dt.time:
    h, m = (int(x) for x in s.split(":"))
    return dt.time(h, m)


def in_blackout(now: dt.datetime, blackouts=None) -> bool:
    blackouts = DEFAULT_BLACKOUTS if blackouts is None else blackouts
    for wd, start, end in blackouts:
        if now.weekday() == wd and _hm(start) <= now.time() < _hm(end):
            return True
    return False


def next_blackout_end(now: dt.datetime, blackouts=None) -> dt.datetime:
    """End of the blackout `now` is inside (call only when in_blackout)."""
    blackouts = DEFAULT_BLACKOUTS if blackouts is None else blackouts
    for wd, start, end in blackouts:
        if now.weekday() == wd and _hm(start) <= now.time() < _hm(end):
            return now.replace(hour=_hm(end).hour, minute=_hm(end).minute,
                               second=0, microsecond=0)
    return now


def bracket_hot_window(bracket: dict | None, pad_min: int = 5) -> str | None:
    """Auto hot window from the last recorded bracket, padded both sides."""
    if not bracket:
        return None
    lo = dt.datetime.fromisoformat(bracket["last_closed"]) \
        - dt.timedelta(minutes=pad_min)
    hi = dt.datetime.fromisoformat(bracket["first_open"]) \
        + dt.timedelta(minutes=pad_min)
    return f"{lo.strftime('%H:%M')}-{hi.strftime('%H:%M')}"


def in_hot_window(now: dt.datetime, windows: list[str]) -> bool:
    for w in windows:
        start, end = w.split("-")
        if _hm(start) <= now.time() < _hm(end):
            return True
    return False


@dataclass
class FlipTracker:
    """Per-date closed-streak / flip detection (logic from `watch`, hardened
    for 24/7 use: tracks last-closed timestamps so a flip yields a bracket)."""
    closed_streak: dict[str, int]
    last_closed: dict[str, str]     # date -> ISO ts of latest reliable-closed read
    open_seen: set[str]             # dates already open (drop caught or missed)

    @classmethod
    def new(cls) -> "FlipTracker":
        return cls({}, {}, set())

    def observe(self, date: str, avail: int, status: str,
                started: str, finished: str) -> dict | None:
        """Feed one observation. Returns a flip event dict when a real drop
        is detected, else None."""
        if avail > 0:
            if date in self.open_seen:
                return None
            self.open_seen.add(date)
            if self.closed_streak.get(date, 0) >= REQUIRE_CLOSED:
                return {"event": "drop", "date": date,
                        "last_closed": self.last_closed[date],
                        "first_open": started, "avail": avail}
            return {"event": "open_at_first_read", "date": date,
                    "first_open": started, "avail": avail}
        if status in ("row_full", "row_missing"):   # reliable-closed read
            self.closed_streak[date] = self.closed_streak.get(date, 0) + 1
            self.last_closed[date] = finished
        # error reads leave the streak untouched
        return None


# --------------------------------------------------------------------------
# State persistence
# --------------------------------------------------------------------------

def _append_obs(obs: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "observations.jsonl").open("a") as f:
        f.write(json.dumps(obs) + "\n")


def load_bracket() -> dict | None:
    p = STATE_DIR / "bracket.json"
    if p.exists():
        return json.loads(p.read_text())
    return None


def save_bracket(bracket: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "bracket.json").write_text(json.dumps(bracket, indent=2))


# --------------------------------------------------------------------------
# Daemon loop (Playwright)
# --------------------------------------------------------------------------

def run_watchd(target_key: str = "paddington", surface_label: str | None = None,
               coarse_secs: int = 1200, fine_secs: int = 20,
               hot_windows: list[str] | None = None, notify: bool = True,
               headless: bool = True, max_cycles: int | None = None) -> None:
    from playwright.sync_api import sync_playwright

    from .providers.everyoneactive import EveryoneActiveProvider, make_context

    secrets = Secrets.from_env()
    target = load_targets()[target_key]
    surface = (target.courts.surfaces[surface_label] if surface_label
               else target.courts.ordered()[0])
    static_hot = hot_windows or list(DEFAULT_HOT_WINDOWS)

    notifier = None
    if notify:
        from .notify.telegram import Notifier
        notifier = Notifier(secrets.telegram_bot_token, secrets.telegram_chat_id)

    def ping(text: str) -> None:
        log.info("watchd.notify", text=text)
        if notifier:
            try:
                notifier.send(text)
            except Exception as e:
                log.warning("watchd.notify_failed", error=str(e))

    tracker = FlipTracker.new()
    bracket = load_bracket()
    last_heartbeat: str | None = None
    cycles = 0

    ping(f"🔭 watchd up — hunting the {target.name} court drop.\n"
         f"Coarse {coarse_secs}s / fine {fine_secs}s; hot {static_hot}"
         + (f"; bracket so far {bracket['last_closed']}–{bracket['first_open']}"
            if bracket else "; no bracket yet"))

    with sync_playwright() as p:
        browser = ctx = page = prov = None

        def teardown():
            nonlocal browser, ctx, page, prov
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            browser = ctx = page = prov = None

        def ensure_session():
            nonlocal browser, ctx, page, prov
            if browser is None:
                browser, ctx = make_context(p, headless=headless)
                page = ctx.new_page()
                prov = EveryoneActiveProvider(secrets, target)
                prov.start_session(ctx, page)
                prov.enter_connect(page, ctx)

        def read_date(date: str) -> tuple[int, str]:
            prov.go_home(page)
            prov.search(page, site=target.site, group=target.courts.group,
                        activity="", start_date=date, end_date=date)
            try:
                prov.open_timetable(page, surface.match)
            except prov.RowFull:
                return 0, "row_full"
            except prov.RowMissing:
                return -1, "row_missing"
            slots = prov.parse_timetable(page, date)
            return sum(s.available for s in slots), f"avail/{len(slots)}"

        consecutive_errors = 0
        while True:
            now = dt.datetime.now(LONDON)

            if in_blackout(now):
                end = next_blackout_end(now)
                log.info("watchd.blackout", until=end.isoformat())
                teardown()                       # no live session in blackout
                time.sleep(max(5, (end - now).total_seconds() + 5))
                continue

            # Daily heartbeat
            today = now.date().isoformat()
            if last_heartbeat != today and now.time() >= _hm(HEARTBEAT_AT):
                last_heartbeat = today
                ping(f"💓 watchd alive {today}. "
                     + (f"Bracket: {bracket['last_closed']} – "
                        f"{bracket['first_open']}" if bracket
                        else "No drop bracket recorded yet."))

            cycles += 1

            dates = [(now.date() + dt.timedelta(days=n)).isoformat()
                     for n in (7, 8)]
            for date in dates:
                started = dt.datetime.now(LONDON).isoformat()
                try:
                    ensure_session()
                    avail, status = read_date(date)
                    consecutive_errors = 0
                except Exception as e:
                    avail, status = -2, f"error:{type(e).__name__}"
                    consecutive_errors += 1
                    log.warning("watchd.read_error", date=date, error=str(e))
                    # rebuild the session next time; back off harder if
                    # errors persist (site overload IS the drop signal too —
                    # it's all in the JSONL)
                    teardown()
                finished = dt.datetime.now(LONDON).isoformat()
                obs = {"t": started, "t_end": finished, "date": date,
                       "surface": surface.label, "avail": avail,
                       "status": status}
                _append_obs(obs)
                log.info("watchd.poll", **obs)

                event = tracker.observe(date, avail, status, started, finished)
                if event and event["event"] == "drop":
                    bracket = {"date": event["date"],
                               "last_closed": event["last_closed"],
                               "first_open": event["first_open"]}
                    save_bracket(bracket)
                    ping(f"🎯 DROP DETECTED for {event['date']} "
                         f"({event['avail']} slots)\n"
                         f"Bracket: {event['last_closed']} → "
                         f"{event['first_open']}\n"
                         f"Tomorrow's hot window auto-tightens to this.")
                elif event and event["event"] == "open_at_first_read":
                    ping(f"⚠️ {event['date']} already OPEN at first read "
                         f"({event['avail']} slots) — drop happened before "
                         f"watchd had a closed baseline (overnight/earlier).")

            if max_cycles is not None and cycles >= max_cycles:
                log.info("watchd.max_cycles")
                break

            # Cadence: fine inside static or auto-bracket hot windows.
            hot = list(static_hot)
            auto = bracket_hot_window(bracket)
            if auto:
                hot.append(auto)
            wait = fine_secs if in_hot_window(dt.datetime.now(LONDON), hot) \
                else coarse_secs
            if consecutive_errors >= 3:
                wait = max(wait, 120)            # polite backoff when unwell
            if wait >= 300:
                teardown()   # don't hold a live Connect session across long idles
            time.sleep(wait)

        teardown()
    log.info("watchd.done", cycles=cycles)
