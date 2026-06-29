"""Everyone Active (Gladstone 'Connect') provider — Paddington tennis.

Flow: account login -> SSO landing -> Connect advanced search -> open the tennis
timetable -> (dry-run) report available slots / (live) click the target slot to
create an unpaid hold, stopping at the 'Pay with card' page.
"""

from __future__ import annotations

import structlog
from playwright.sync_api import Page, sync_playwright

from ..config import Secrets, Target

log = structlog.get_logger()

ACCOUNT_LOGIN = "https://account.everyoneactive.com/login"
ACCOUNT_BOOK = "https://account.everyoneactive.com/book"
ACCOUNT_MEMBERSHIPS = "https://account.everyoneactive.com/memberships"

# Connect advanced-search control ids (discovered live + from recon HAR).
SEL = "ctl00_MainContent__advanceSearchUserControl_"
ID_SITE_SIMPLE = f"#{SEL}SitesSimple"
ID_SITE_ADV = f"#{SEL}SitesAdvanced"
ID_GROUP = f"#{SEL}ActivityGroups"
ID_ACTIVITY = f"#{SEL}Activities"
ID_START_DATE = f"#{SEL}startDate"
ID_END_DATE = f"#{SEL}endDate"
ID_START_TIME = f"#{SEL}StartTime"
ID_END_TIME = f"#{SEL}EndTime"
ID_SEARCH_BTN = f"#{SEL}_searchBtn"


class EveryoneActiveProvider:
    def __init__(self, secrets: Secrets, target: Target):
        self.secrets = secrets
        self.target = target

    # ── session ──────────────────────────────────────────────────────────────
    def start_session(self, ctx, page: Page) -> None:
        """Reuse a saved session if valid; otherwise log in and persist it."""
        from .. import session as sess
        page.goto(ACCOUNT_MEMBERSHIPS, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        if "login" in page.url:
            log.info("session.stale_relogin")
            self.login(page)
            sess.save(ctx)
        else:
            log.info("session.reused", url=page.url)

    def login(self, page: Page) -> None:
        log.info("login.start")
        page.goto(ACCOUNT_LOGIN, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        for label in ("Deny", "Allow all"):
            try:
                page.get_by_role("button", name=label, exact=True).click(timeout=3000)
                break
            except Exception:
                pass
        page.fill("#emailAddress", self.secrets.ea_email)
        page.fill("#password", self.secrets.ea_password)
        # Wait for the authenticated account/me call — proves the session is fully
        # established, not just an intermediate redirect off /login.
        try:
            with page.expect_response(
                lambda r: "v1/account/me" in r.url and r.status == 200,
                timeout=45000,
            ):
                page.get_by_role("button", name="Log in", exact=False).click()
        except Exception:
            # Fall back to URL-based wait if the response race is missed.
            page.wait_for_url(lambda u: "login" not in u, timeout=45000)
        page.wait_for_load_state("networkidle", timeout=60000)
        log.info("login.ok", url=page.url)

    def _harvest_token(self, page: Page) -> str | None:
        """The SSO token is embedded in many authed links (connect/landing,
        profile.everyoneactive.com/*). Harvest it from whichever page renders."""
        from urllib.parse import parse_qs, urlparse
        authed_pages = [
            "https://account.everyoneactive.com/memberships",
            ACCOUNT_BOOK,
            "https://account.everyoneactive.com/",
        ]
        for url in authed_pages:
            for attempt in range(2):                 # reload once if SPA empty
                page.goto(url, wait_until="networkidle", timeout=30000)
                for _ in range(8):
                    hrefs = page.eval_on_selector_all("a", "els=>els.map(e=>e.href)")
                    land = next((h for h in hrefs
                                 if "connect/landing.aspx" in (h or "").lower()), None)
                    if land:
                        return parse_qs(urlparse(land).query).get("token", [None])[0]
                    tok = next((h for h in hrefs if "token=" in (h or "")), None)
                    if tok:
                        return parse_qs(urlparse(tok).query).get("token", [None])[0]
                    page.wait_for_timeout(1500)
                page.reload(wait_until="networkidle")
        return None

    HOME_URL = "https://book.everyoneactive.com/Connect/memberHomePage.aspx"

    def enter_connect(self, page: Page, ctx=None) -> None:
        log.info("connect.enter")
        # 1) Fast path: a still-valid Connect session loads the search home.
        page.goto(self.HOME_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)

        # 2) Bounced to Connect's own login? Log in there directly (most robust —
        #    no dependency on the flaky Next.js SSO / token harvest).
        if "memberHomePage" not in page.url and \
                page.locator("#ctl00_MainContent_InputLogin").count():
            log.info("connect.mrmlogin")
            page.fill("#ctl00_MainContent_InputLogin", self.secrets.ea_email)
            page.fill("#ctl00_MainContent_InputPassword", self.secrets.ea_password)
            # Submit via JS so Playwright doesn't block on the navigation auto-wait
            # (that wait intermittently timed out and crashed scheduled runs).
            page.eval_on_selector("#ctl00_MainContent_btnLogin", "el => el.click()")
            try:
                page.wait_for_url("**/memberHomePage.aspx", timeout=45000)
            except Exception:
                page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            if "memberHomePage" not in page.url:
                page.goto(self.HOME_URL, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1000)

        # 3) Last resort: SSO token harvest from the account SPA.
        if "memberHomePage" not in page.url:
            token = self._harvest_token(page)
            if not token:
                raise RuntimeError("Could not enter Connect (MRMLogin + SSO failed)")
            page.goto(f"https://book.everyoneactive.com/connect/landing.aspx?token={token}",
                      wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)

        if "memberHomePage" not in page.url:
            raise RuntimeError(f"Connect entry failed, at: {page.url}")
        log.info("connect.ok", url=page.url)
        if ctx is not None:                          # persist Connect cookies too
            from .. import session as sess
            sess.save(ctx)

    # ── advanced-search helpers ───────────────────────────────────────────────
    def _settle(self, page: Page) -> None:
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(800)

    def expand_advanced(self, page: Page) -> None:
        """Expose the Advanced Search controls (site/group/activity dropdowns)."""
        if not page.locator(ID_ACTIVITY).is_visible():
            page.get_by_text("Advanced Search", exact=False).first.click()
            page.wait_for_timeout(800)

    def _options(self, page: Page, sel: str) -> list[dict]:
        return page.eval_on_selector_all(
            f"{sel} option",
            "els=>els.map(e=>({code:e.value, name:(e.text||'').trim()}))")

    def list_sites(self, page: Page) -> list[dict]:
        self.expand_advanced(page)
        return self._options(page, ID_SITE_ADV)

    def list_groups(self, page: Page, site: str) -> list[dict]:
        self.expand_advanced(page)
        page.select_option(ID_SITE_ADV, site); self._settle(page)
        return self._options(page, ID_GROUP)

    def list_activities(self, page: Page, site: str,
                        group: str | None = None) -> list[dict]:
        self.expand_advanced(page)
        page.select_option(ID_SITE_ADV, site); self._settle(page)
        if group:
            page.select_option(ID_GROUP, group); self._settle(page)
        return self._options(page, ID_ACTIVITY)

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, page: Page, site: str, group: str, activity: str,
               start_date: str, end_date: str) -> None:
        """Fill the advanced-search form and submit. Dates are ISO yyyy-mm-dd.
        Parameterized so it serves any centre / court surface / activity."""
        log.info("search.fill", site=site, group=group, activity=activity,
                 start=start_date, end=end_date)
        self.expand_advanced(page)
        # Site -> Activity group -> Activity. Each select triggers a postback
        # that repopulates the dependent dropdown, so settle after each.
        page.select_option(ID_SITE_ADV, site)
        self._settle(page)
        page.select_option(ID_GROUP, group)
        self._settle(page)
        page.select_option(ID_ACTIVITY, activity)
        self._settle(page)
        page.fill(ID_START_DATE, start_date)
        page.fill(ID_END_DATE, end_date)
        log.info("search.submit")
        page.click(ID_SEARCH_BTN)
        self._settle(page)
        page.wait_for_timeout(1500)
        log.info("search.done", url=page.url)


    # ── results / timetable ───────────────────────────────────────────────────
    def go_home(self, page: Page) -> None:
        """Return to the Connect search home (to run another search)."""
        page.goto("https://book.everyoneactive.com/Connect/memberHomePage.aspx",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        if "memberHomePage" not in page.url:
            raise RuntimeError(f"go_home landed off search page: {page.url}")

    class RowFull(RuntimeError):
        """The matched results row exists but is Full."""

    class RowMissing(RuntimeError):
        """No results row matched the requested name."""

    def open_timetable(self, page: Page, row_match: str) -> None:
        """From the search results, open the 'Select Slot' grid for the row whose
        activity name contains `row_match`. Climbs from each availability button
        to its activity-name link (ignores the home-centre sidebar). Raises
        RowFull / RowMissing so the caller can fall back to another surface.

        Triggers the ASP.NET __doPostBack directly rather than a physical click —
        a loading overlay intercepts clicks and the button detaches mid-postback."""
        # Locate the matching row WITHOUT clicking (climb each availability button
        # to its activity name). Returns {full,name} | {name} | None.
        scan_js = """(match) => {
            const btns = [...document.querySelectorAll('.availabilitybutton')];
            for (const b of btns) {
                if (b.offsetParent === null) continue;          // visible only
                let el = b, name = '', hops = 0;
                while (el && hops < 8) {
                    const l = el.querySelector(
                        "a[id*='lnkActivitySelect'], a.BookingLinkButton");
                    if (l) { name = (l.innerText || '').trim(); break; }
                    el = el.parentElement; hops++;
                }
                if (name.includes(match)) {
                    const full = /full/i.test(b.className) ||
                                 /full/i.test(b.innerText || '');
                    return full ? {full: true, name} : {name};
                }
            }
            return null;
        }"""
        # Results render via an async postback — poll for the row to appear.
        found = None
        for _ in range(8):
            found = page.evaluate(scan_js, row_match)
            if found is not None:
                break
            page.wait_for_timeout(800)
        if found is None:
            raise self.RowMissing(f"No results row matching '{row_match}'")
        if found.get("full"):
            raise self.RowFull(f"Row '{row_match}' is Full")

        def find_and_click():
            # Fire the row's javascript:__doPostBack via a programmatic .click()
            # in page context (avoids strict-mode eval + the intercept overlay).
            return page.evaluate(
                """(match) => {
                    const btns = [...document.querySelectorAll('.availabilitybutton')];
                    for (const b of btns) {
                        if (b.offsetParent === null) continue;
                        let el = b, name = '', hops = 0;
                        while (el && hops < 8) {
                            const l = el.querySelector(
                                "a[id*='lnkActivitySelect'], a.BookingLinkButton");
                            if (l) { name = (l.innerText || '').trim(); break; }
                            el = el.parentElement; hops++;
                        }
                        if (name.includes(match)) { b.click(); return {name}; }
                    }
                    return null;
                }""",
                row_match,
            )

        find_and_click()

        for attempt in range(3):
            try:
                page.wait_for_url(lambda u: "mrmProductStatus" in u
                                  or "mrmResourceStatus" in u, timeout=12000)
                break
            except Exception:
                page.wait_for_timeout(1000)
                if page.locator("text=Select Slot").count():
                    break
                log.warn("timetable.retry", attempt=attempt, url=page.url)
                find_and_click()       # re-fire (results may have re-rendered)
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        log.info("timetable.open", matched=found["name"], url=page.url)

    def parse_class(self, page: Page, date: str, time: str) -> list["Slot"]:
        """Parse an activity/class page (mrmClassStatus.aspx). Each session has a
        'Book' button + an 'N spaces remaining' indicator. Tags the Book button
        for clicking. 'court' carries the spaces text for the notification."""
        from ..models import Slot
        raw = page.evaluate(
            """() => {
                const out = [];
                const books = [...document.querySelectorAll("[id*='btnBook']")]
                    .filter(b => /^(A|BUTTON|INPUT)$/.test(b.tagName));
                books.forEach((b, i) => {
                    if (b.offsetParent === null) return;          // visible only
                    const tag = 'cb' + i;
                    b.setAttribute('data-cb', tag);
                    const row = b.closest('.div-row, tr, .panel, div') || document;
                    const sp = row.querySelector(
                        "[id*='btnAvaliable'], [id*='Available'], [id*='Avaliable']");
                    const spaces = sp ? (sp.innerText || '').trim() : '';
                    const full = /full|no space|sold out/i.test(row.innerText || '');
                    out.push({spaces, full, tag});
                });
                return out;
            }"""
        )
        slots = [Slot(date=date, time=time, court=(r["spaces"] or "session"),
                      available=not r["full"], selector=f'[data-cb="{r["tag"]}"]')
                 for r in raw]
        log.info("class.parsed", sessions=len(slots),
                 available=sum(s.available for s in slots))
        return slots

    _MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def my_bookings(self, page: Page) -> list[dict]:
        """Parse Connect 'Manage Bookings' (mrmViewMyBookings) into rows with
        activity text, date (day/mon), and paid flag."""
        import re
        page.goto("https://book.everyoneactive.com/Connect/"
                  "mrmViewMyBookings.aspx?showOption=1",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        rows = page.evaluate(
            """() => {
                const out = [];
                for (const tr of document.querySelectorAll('tr')) {
                    const cells = [...tr.children].map(c => (c.innerText || '').trim());
                    const joined = cells.join(' | ');
                    if (/\\b(Paid|Unpaid)\\b/.test(joined) && cells.length >= 4)
                        out.push(cells);
                }
                return out;
            }"""
        )
        parsed = []
        for cells in rows:
            text = " | ".join(cells)
            m = re.search(r"(\d{1,2})\s+([A-Z][a-z]{2})", text)
            parsed.append({
                "text": text,
                "activity": cells[0] if cells else "",
                "paid": "Unpaid" not in text,
                "day": int(m.group(1)) if m else None,
                "mon": m.group(2) if m else None,
            })
        return parsed

    def has_booking(self, page: Page, iso_date: str, match: str) -> str | None:
        """Return 'paid' / 'unpaid' if a booking matching `match` exists on
        `iso_date` (Manage Bookings), else None."""
        y, mo, d = (int(x) for x in iso_date.split("-"))
        mon = self._MON[mo - 1]
        for b in self.my_bookings(page):
            if match in b["text"] and b["day"] == d and b["mon"] == mon:
                return "paid" if b["paid"] else "unpaid"
        return None

    def confirm_hold(self, page: Page) -> str | None:
        """From the 'Complete Your Booking' page, click 'Book & Checkout' to
        commit the unpaid hold (lands on the basket / 'Pay with card' page).
        STOPS before any payment — never clicks 'Pay with card'.
        Returns the booking reference if found."""
        import re
        page.get_by_text("Book & Checkout", exact=False).first.click(timeout=10000)
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(2500)
        body = page.inner_text("body")
        # Safety: confirm we're at an unpaid 'pay' state, not mid-payment.
        if "pay" not in body.lower():
            log.warn("hold.no_pay_marker", url=page.url)
        m = re.search(r"Ref[:\s]+(\d+)", body)
        ref = m.group(1) if m else None
        log.info("hold.committed", url=page.url, ref=ref)
        return ref

    def parse_timetable(self, page: Page, date: str) -> list["Slot"]:
        """Parse the single-day per-court grid. Buttons have no id, so we tag
        each with a data-tb attribute for reliable clicking, map it to its
        court (column header) and time, and record availability."""
        from ..models import Slot
        raw = page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('table tr')];
                // Map column index -> court name from the header row.
                const courts = {};
                for (const tr of rows) {
                    const cells = [...tr.children];
                    if (cells.some(c => /Court/i.test(c.innerText))) {
                        cells.forEach((c,i) => {
                            if (/Court/i.test(c.innerText))
                                courts[i] = c.innerText.trim();
                        });
                        break;
                    }
                }
                const out = [];
                let n = 0;
                for (const tr of rows) {
                    const cells = [...tr.children];
                    cells.forEach((c,i) => {
                        const b = c.querySelector('input,button,a');
                        if (!b) return;
                        const txt = (b.value || b.innerText || '').trim();
                        if (!/^\\d\\d:\\d\\d$/.test(txt)) return;
                        const cls = b.className || '';
                        const tag = 'tb' + (n++);
                        b.setAttribute('data-tb', tag);
                        out.push({time: txt.slice(0,5), court: courts[i] || ('col'+i),
                                  available: /success/i.test(cls), tag});
                    });
                }
                return out;
            }"""
        )
        slots = [Slot(date=date, time=r["time"], court=r["court"],
                      available=r["available"],
                      selector=f'[data-tb="{r["tag"]}"]') for r in raw]
        log.info("timetable.parsed", total=len(slots),
                 available=sum(s.available for s in slots))
        return slots


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _parse_day_header(text: str) -> str | None:
    """'Sat 27 Jun' -> '2026-06-27' (infers year, rolling into next year)."""
    import datetime as dt
    parts = text.replace(",", "").split()
    if len(parts) < 3:
        return None
    try:
        day = int(parts[1]); mon = _MONTHS[parts[2][:3]]
    except (ValueError, KeyError):
        return None
    today = dt.date.today()
    year = today.year if mon >= today.month else today.year + 1
    try:
        return dt.date(year, mon, day).isoformat()
    except ValueError:
        return None


def _norm_time(text: str) -> str | None:
    text = text.strip()
    if ":" in text and text[:2].isdigit():
        return text[:5]
    return None


def make_context(p, headless: bool = True, use_session: bool = True):
    """Launch Chromium. If a fresh saved session exists, load it so we skip login."""
    from .. import session as sess
    browser = p.chromium.launch(headless=headless)
    kwargs = dict(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0.0.0 Safari/537.36"),
        viewport={"width": 1400, "height": 950},
        locale="en-GB",
    )
    if use_session and sess.is_fresh():
        kwargs["storage_state"] = str(sess.state_path())
    ctx = browser.new_context(**kwargs)
    return browser, ctx
