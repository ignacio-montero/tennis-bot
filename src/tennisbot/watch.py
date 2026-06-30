"""Watch mode — pinpoint the daily court 'drop' time (read-only, no holds).

Polls Paddington court availability for the date(s) expected to drop tonight and
logs the exact moment slots first appear. Run via launchd around 21:25-22:20 to
settle whether the drop is 21:45 or 22:00 (and which day-offset releases).
"""

from __future__ import annotations

import datetime as dt
import time

import structlog
from playwright.sync_api import sync_playwright

from .config import Secrets, load_targets
from .providers.everyoneactive import EveryoneActiveProvider, make_context

log = structlog.get_logger()


def _avail_for_date(prov, page, target, surface, date: str) -> tuple[int, str]:
    """Return (available_count, status) for one date+surface."""
    prov.go_home(page)
    prov.search(page, site=target.site, group=target.courts.group,
                activity="", start_date=date, end_date=date)
    try:
        prov.open_timetable(page, surface.match)
    except prov.RowFull:
        return 0, "row_full"
    except prov.RowMissing:
        return -1, "row_missing"   # not offered yet (pre-drop)
    slots = prov.parse_timetable(page, date)
    return sum(s.available for s in slots), f"avail/{len(slots)}"


def watch_drop(target_key: str = "paddington", dates: list[str] | None = None,
               surface_label: str | None = None, poll_secs: int = 15,
               until: str = "22:20", max_polls: int | None = None,
               headless: bool = True) -> None:
    secrets = Secrets.from_env()
    target = load_targets()[target_key]
    if not dates:
        # Watch BOTH boundary dates so an off-by-one can't fool us:
        #   today+7 → opens tonight if the window is 7 days;
        #   today+8 → should stay closed unless the window is really 8 days.
        # Whichever flips closed→open (and when) settles offset AND drop time.
        today = dt.date.today()
        dates = [(today + dt.timedelta(days=n)).isoformat() for n in (7, 8)]
    surface = (target.courts.surfaces[surface_label] if surface_label
               else target.courts.ordered()[0])
    end_h, end_m = (int(x) for x in until.split(":"))
    dropped: set[str] = set()           # confirmed real drop transitions
    open_at_start: set[str] = set()     # already open before we started (too late)
    closed_streak: dict[str, int] = {}  # consecutive reliable-closed reads per date
    REQUIRE_CLOSED = 5                   # sustained-closed baseline before a drop counts

    log.info("watch.start", dates=dates, surface=surface.label,
             poll_secs=poll_secs, until=until)
    with sync_playwright() as p:
        browser, ctx = make_context(p, headless=headless)
        page = ctx.new_page()
        prov = EveryoneActiveProvider(secrets, target)
        prov.start_session(ctx, page)
        prov.enter_connect(page, ctx)

        polls = 0
        while True:
            now = dt.datetime.now()
            if (now.hour, now.minute) >= (end_h, end_m):
                log.info("watch.window_end"); break
            if max_polls is not None and polls >= max_polls:
                log.info("watch.max_polls"); break
            polls += 1

            for date in dates:
                try:
                    avail, status = _avail_for_date(prov, page, target, surface, date)
                except Exception as e:
                    avail, status = -2, f"error:{type(e).__name__}"
                    try:
                        prov.enter_connect(page, ctx)
                    except Exception:
                        pass
                stamp = now.strftime("%H:%M:%S")
                log.info("watch.poll", t=stamp, date=date,
                         surface=surface.label, avail=avail, status=status)
                # Detection (flaky reads handled): "open" (avail>0) is reliable;
                # a *real drop* only counts after a sustained-closed baseline.
                # Open before that baseline = we started too late.
                if avail > 0:
                    if date not in dropped and date not in open_at_start:
                        if closed_streak.get(date, 0) >= REQUIRE_CLOSED:
                            dropped.add(date)
                            log.warning("watch.DROP_DETECTED",
                                        t=now.strftime("%H:%M:%S.%f"), date=date,
                                        surface=surface.label, avail=avail)
                        else:
                            open_at_start.add(date)
                            log.warning("watch.ALREADY_OPEN_AT_START — start earlier",
                                        t=stamp, date=date, avail=avail)
                    closed_streak[date] = 0
                elif status in ("row_full", "row_missing"):  # reliable-closed read
                    closed_streak[date] = closed_streak.get(date, 0) + 1
                # error reads (-2) leave the streak unchanged
            time.sleep(poll_secs)
        browser.close()
    log.info("watch.done", dropped=sorted(dropped))
