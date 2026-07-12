"""Tennis-Bot CLI.

Examples:
    python -m tennisbot run-now --date 2026-07-04            # dry-run (safe)
    python -m tennisbot run-now --date 2026-07-04 --live     # creates a hold
    python -m tennisbot run-now --date 2026-07-04 --headed   # watch the browser
"""

from __future__ import annotations

import argparse
import logging
import sys

import structlog

from .runner import run_once


def main(argv: list[str] | None = None) -> int:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    ap = argparse.ArgumentParser(prog="tennisbot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rn = sub.add_parser("run-now", help="Attempt a booking immediately.")
    g = rn.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="Target play date, YYYY-MM-DD")
    g.add_argument("--days-ahead", type=int,
                   help="Target date = today + N days (for scheduled runs).")
    rn.add_argument("--centre", default="paddington",
                    help="Target key from targets.yaml (paddington|westway).")
    rn.add_argument("--mode", default="court", choices=["court", "activity"],
                    help="Book a court (default) or an activity.")
    rn.add_argument("--activity", help="Activity label (activity mode).")
    rn.add_argument("--time", help="Override prefs: book this HH:MM, any court.")
    rn.add_argument("--live", action="store_true",
                    help="Actually create the hold (default is dry-run).")
    rn.add_argument("--headed", action="store_true",
                    help="Show the browser window.")
    rn.add_argument("--no-notify", action="store_true",
                    help="Suppress Telegram messages (for dev/testing).")

    dc = sub.add_parser("discover", help="Find centre/activity codes.")
    dc.add_argument("--centre", help="Find site code(s) by name substring.")
    dc.add_argument("--site", help="List groups + activities for a site code.")
    dc.add_argument("--group", help="Filter activities to this group code.")
    dc.add_argument("--headed", action="store_true")

    dr = sub.add_parser("drop", help="Spin-wait to the court drop, then book.")
    dr.add_argument("--centre", default="paddington")
    dr.add_argument("--time", help="Override drop time HH:MM (for testing).")
    dr.add_argument("--want-time", help="Book this HH:MM (else ranked prefs).")
    dr.add_argument("--live", action="store_true",
                    help="Create the hold (default dry-run).")
    dr.add_argument("--epsilon", type=float, default=0.15,
                    help="Seconds after the instant to fire (avoid early reject).")
    dr.add_argument("--retry-window", type=float, default=90.0,
                    help="Seconds to keep camping/retrying through overload.")
    dr.add_argument("--retry-gap", type=float, default=1.0,
                    help="Seconds between retry attempts.")
    dr.add_argument("--no-notify", action="store_true")
    dr.add_argument("--headed", action="store_true")

    wt = sub.add_parser("watch", help="Watch for the court drop (read-only).")
    wt.add_argument("--centre", default="paddington")
    wt.add_argument("--date", action="append",
                    help="Date(s) to watch (repeatable). Default: today+7 and +8.")
    wt.add_argument("--surface", help="Surface label (default: preferred).")
    wt.add_argument("--poll", type=int, default=15, help="Seconds between polls.")
    wt.add_argument("--until", default="22:20", help="Stop time HH:MM.")
    wt.add_argument("--max-polls", type=int, help="Stop after N polls (testing).")
    wt.add_argument("--headed", action="store_true")

    wd = sub.add_parser("watchd", help="24/7 drop-time hunter daemon "
                        "(read-only; for the always-on host).")
    wd.add_argument("--centre", default="paddington")
    wd.add_argument("--surface", help="Surface label (default: preferred).")
    wd.add_argument("--coarse", type=int, default=1200,
                    help="Seconds between polls outside hot windows.")
    wd.add_argument("--fine", type=int, default=20,
                    help="Seconds between polls inside hot windows.")
    wd.add_argument("--hot", action="append",
                    help="Hot window HH:MM-HH:MM (repeatable; default "
                         "21:35-22:15 + auto-bracket window).")
    wd.add_argument("--max-cycles", type=int, help="Stop after N cycles (testing).")
    wd.add_argument("--no-notify", action="store_true")
    wd.add_argument("--headed", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "discover":
        from .discover import run_discover
        run_discover(centre=args.centre, site=args.site, group=args.group,
                     headless=not args.headed)
        return 0

    if args.cmd == "drop":
        from .runner import run_drop
        res = run_drop(target_key=args.centre, dry_run=not args.live,
                       headless=not args.headed, want_time=args.want_time,
                       time_override=args.time, notify=not args.no_notify,
                       epsilon=args.epsilon, retry_window_s=args.retry_window,
                       retry_gap_s=args.retry_gap)
        print(f"\nRESULT: ok={res.ok} dry_run={res.dry_run} "
              f"chosen={res.chosen} :: {res.message}")
        return 0 if res.ok else 1

    if args.cmd == "watch":
        from .watch import watch_drop
        watch_drop(target_key=args.centre, dates=args.date,
                   surface_label=args.surface, poll_secs=args.poll,
                   until=args.until, max_polls=args.max_polls,
                   headless=not args.headed)
        return 0

    if args.cmd == "watchd":
        from .watchd import run_watchd
        run_watchd(target_key=args.centre, surface_label=args.surface,
                   coarse_secs=args.coarse, fine_secs=args.fine,
                   hot_windows=args.hot, notify=not args.no_notify,
                   headless=not args.headed, max_cycles=args.max_cycles)
        return 0

    if args.cmd == "run-now":
        import datetime as _dt
        target_date = (args.date if args.date else
                       (_dt.date.today() + _dt.timedelta(days=args.days_ahead))
                       .isoformat())
        res = run_once(target_date=target_date, dry_run=not args.live,
                       headless=not args.headed, want_time=args.time,
                       target_key=args.centre, mode=args.mode,
                       activity_label=args.activity, notify=not args.no_notify)
        print(f"\nRESULT: ok={res.ok} dry_run={res.dry_run} "
              f"chosen={res.chosen} :: {res.message}")
        return 0 if res.ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
