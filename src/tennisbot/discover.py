"""Discovery helper: find a centre's site code and its activity group / activity
codes, so new centres, surfaces, and activities can be added to config.

Usage:
    python -m tennisbot discover --centre Westway          # find site code(s)
    python -m tennisbot discover --site 0156               # groups + activities
    python -m tennisbot discover --site 0156 --group 156ADULT
"""

from __future__ import annotations

import structlog
from playwright.sync_api import sync_playwright

from .config import Secrets
from .providers.everyoneactive import EveryoneActiveProvider, make_context

log = structlog.get_logger()


def run_discover(centre: str | None = None, site: str | None = None,
                 group: str | None = None, headless: bool = True) -> None:
    secrets = Secrets.from_env()
    with sync_playwright() as p:
        browser, ctx = make_context(p, headless=headless)
        page = ctx.new_page()
        # A bare provider (no target needed for discovery).
        prov = EveryoneActiveProvider(secrets, target=None)
        try:
            prov.start_session(ctx, page)
            prov.enter_connect(page, ctx)
            prov.expand_advanced(page)

            if centre:
                sites = prov.list_sites(page)
                needle = centre.lower()
                hits = [s for s in sites if needle in s["name"].lower()]
                print(f"\n=== Sites matching '{centre}' ===")
                for s in hits or sites[:0]:
                    print(f"  {s['code']:>6}  {s['name']}")
                if not hits:
                    print("  (no match — showing all is suppressed; refine the name)")
                return

            if site:
                groups = prov.list_groups(page, site)
                print(f"\n=== Activity GROUPS for site {site} ===")
                for g in groups:
                    print(f"  {g['code']:>16}  {g['name']}")
                acts = prov.list_activities(page, site, group=group)
                gl = f" (group {group})" if group else " (all groups)"
                print(f"\n=== ACTIVITIES for site {site}{gl} ===")
                for a in acts:
                    print(f"  {a['code']:>20}  {a['name']}")
                return

            print("Specify --centre NAME or --site CODE [--group CODE]")
        finally:
            browser.close()
