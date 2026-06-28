"""Clock discipline for the timed court drop.

We fire against the *booking server's* clock, not ours. The server exposes its
time via the HTTP `Date` header, so we measure the skew (server - local) and
wait until our local clock reaches `drop_instant - skew + epsilon`.

`Date` has 1-second resolution, so timing is good to ~±0.5s — fine combined with
a small positive epsilon (fire fractionally late-but-accepted rather than early-
and-rejected).
"""

from __future__ import annotations

import datetime as dt
import statistics
import time
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx
import structlog

log = structlog.get_logger()

SERVER_URL = "https://book.everyoneactive.com/Connect/memberHomePage.aspx"


def server_skew(url: str = SERVER_URL, samples: int = 5) -> float:
    """Median (server_time - local_time) in seconds, round-trip corrected."""
    offsets: list[float] = []
    with httpx.Client(timeout=10.0) as c:
        for _ in range(samples):
            t0 = time.time()
            try:
                r = c.head(url)
                t1 = time.time()
                date = r.headers.get("Date")
                if not date:
                    continue
                server = parsedate_to_datetime(date).timestamp()
                offsets.append(server - (t0 + t1) / 2)   # midpoint of round-trip
            except Exception as e:                          # noqa: BLE001
                log.warn("skew.sample_failed", err=str(e))
            time.sleep(0.3)
    if not offsets:
        log.warn("skew.none — assuming 0")
        return 0.0
    skew = statistics.median(offsets)
    log.info("skew.measured", skew=round(skew, 3), n=len(offsets))
    return skew


def drop_instant(local_time: str, tz: str, on: dt.date | None = None) -> float:
    """Epoch (UTC) of `local_time` (HH:MM) on `on` (default today) in `tz`."""
    on = on or dt.datetime.now(ZoneInfo(tz)).date()
    h, m = (int(x) for x in local_time.split(":"))
    return dt.datetime(on.year, on.month, on.day, h, m,
                       tzinfo=ZoneInfo(tz)).timestamp()


def wait_until(target_epoch: float, skew: float = 0.0, epsilon: float = 0.15,
               spin_lead: float = 2.0) -> None:
    """Block until the server clock reaches target_epoch.

    Fires when local clock == target_epoch - skew + epsilon. Coarse-sleeps until
    `spin_lead` seconds before, then busy-waits for sub-second accuracy.
    """
    local_fire = target_epoch - skew + epsilon
    coarse = local_fire - spin_lead
    now = time.time()
    if coarse > now:
        time.sleep(coarse - now)
    while time.time() < local_fire:
        pass  # tight spin for the final ~2s
    log.info("wait.fired", lateness_ms=round((time.time() - local_fire) * 1000, 1))
