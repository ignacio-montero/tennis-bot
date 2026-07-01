# CLAUDE.md — read me first

Orientation for any Claude Code session working in this repo. (Claude Code loads
this file automatically at session start.)

## What this is
A personal bot that books public tennis courts / activities in London via
Everyone Active's Gladstone "Connect" platform, using a **hold-and-notify**
pattern: it secures an *unpaid 1-hour hold* and pings Telegram; the user pays in
the Everyone Active app. **Payment / 3DS is intentionally out of scope.**

## Docs map
- `ARCHITECTURE.md` — design, decisions, connectivity strategy.
- `recon/FINDINGS.md` — reverse-engineering notes (IDs, flow, surfaces, layouts).
- `BACKLOG.md` — future ideas + status.
- `CLAUDE.md` (this file) — current state + how to run + gotchas.

## Current state — WORKING (verified in dry-run) ✅
Single-shot booking via `run-now`, for **two centres**, **courts + activities**,
with **surface preference** and **two-consecutive-hours** mode:
- **Centres:** Paddington (`0156`), Westway Sport & Fitness (`0162`).
- **Court surfaces** (matched by results-row name): Paddington Synth/Tarmac;
  Westway Outdoor (Indoor/EarlyBird supported but not enabled). Bot tries
  enabled surfaces in preference order, skipping full ones.
- **Activities:** Paddington "Tennis (adv) Sun 1300" / "Wed 1900" (Adult
  Activities). Land on a class page (`mrmClassStatus.aspx`), parsed separately.
- **Two consecutive hours, same court:** `courts.two_hours: true`; books both, or
  the single hour if only one is free (per user's choice). Cap raised to 2 only
  in this mode. Selection logic unit-tested (`tests/test_runner_logic.py`).
- **Session persistence** (`.session/`, gitignored) + robust Connect entry.

✅ **Verified LIVE (real hold):** single Paddington court, and **activity booking**
(Ref 1561842712, Sun 1300). The **2-hour second-hour** live path is coded but only
dry-run tested — confirm with one `--live` run before relying on it.
Activity booking is also **scheduled/automated** — see below.

## How to run
```bash
# DRY-RUN is default (no hold created). --live creates a real hold.
.venv/bin/python -m tennisbot run-now --centre paddington --date 2026-07-04 --time 18:00
.venv/bin/python -m tennisbot run-now --centre westway   --date 2026-07-04 --time 18:00
.venv/bin/python -m tennisbot run-now --centre paddington --mode activity --date 2026-07-05
.venv/bin/python -m tennisbot run-now --centre paddington --date 2026-07-04 --live
# flags: --headed (show browser), --no-notify (suppress Telegram, for dev/testing)

# Discover codes/names for a new centre/activity:
.venv/bin/python -m tennisbot discover --centre "Swiss Cottage"
.venv/bin/python -m tennisbot discover --site 0162 --group 162TENNIS

# Court DROP: watch (read-only) to find the drop time; drop = spin-wait then book.
.venv/bin/python -m tennisbot watch                      # tonight's drop, both today+7/+8
.venv/bin/python -m tennisbot drop --time 21:46 --no-notify   # test: dry-run, spin-wait to HH:MM
.venv/bin/python -m tennisbot drop --live                # real: fire at config drop time
```
Targets/surfaces/activities are configured in `config/targets.yaml`.
`--time HH:MM` overrides ranked prefs (books that time, any court).

## Architecture (code map)
- `providers/everyoneactive.py` — login, robust Connect entry, search, results
  row-matching, court-grid + class parsers, hold.
- `runner.py` — orchestration: court mode (surface loop + 2h), activity mode,
  dry-run vs live, screenshots, Telegram.
- `discover.py` — `discover` command. `config.py` — typed config. `session.py`
  — storage_state. `notify/telegram.py` — notifications. `cli.py` — entrypoint.

## Key facts & gotchas (learned the hard way)
- **Connect entry is 3-tier** (most→least robust): reuse saved Connect session →
  **MRMLogin.aspx** direct login (`#ctl00_MainContent_InputLogin/_InputPassword/
  _btnLogin`, EA email+password) → SSO token harvest from the Next.js account SPA.
  The SPA path is flaky (renders empty / throttles), so MRMLogin is primary.
- **Surface/activity = match the results-row NAME**, not a dropdown code. Search
  site + group + "Any", then climb from each `.availabilitybutton` to its
  `lnkActivitySelect`/`BookingLinkButton` name. Ignore the left QuickBook sidebar
  (always shows the member's home/Paddington shortcuts).
- **Click availability via `b.click()` inside `page.evaluate`** — a loading
  overlay intercepts real pointer clicks, and calling `__doPostBack` from
  Playwright eval hits strict-mode `arguments` errors.
- **Poll for result rows** after a search (UpdatePanel postback renders async) —
  reading too early gives false "not offered".
- **Court grid** (`mrmProductStatus.aspx`): columns = courts, rows = times; only
  *available* cells are interactive (parse captures those). **Class page**
  (`mrmClassStatus.aspx`): a "Book" button + "N spaces remaining".
- ⚠️ **"Tennis (adv) Wed 1300" does not exist** — only Wed 1900 (in config).
- **Be polite:** heavy repeated logins throttle the account SPA. Session reuse
  mitigates this; don't hammer.
- **Drop time:** ~21:45 local, 7 days ahead — still needs empirical confirmation.

## Secrets
`.env` (gitignored): `EA_EMAIL`, `EA_PASSWORD`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. ⚠️ Shared <redacted> — **rotate the Telegram
<redacted>.**

## Automation — activity booking (LIVE, scheduled) ✅
Four `launchd` jobs book the Paddington "Tennis (adv)" activities 7 days ahead:
| Job | When | Books |
|---|---|---|
| `com.tennisbot.activity-wed-primary` | Wed 19:00 | next Wed "Tennis (adv) Wed 1900" |
| `com.tennisbot.activity-wed-backup`  | Wed 20:30 | re-hold (you're on court at 19:00) |
| `com.tennisbot.activity-sun-primary` | Sun 13:00 | next Sun "Tennis (adv) Sun 1300" |
| `com.tennisbot.activity-sun-backup`  | Sun 14:30 | re-hold |

- All call `scripts/scheduled_run.sh` → `run-now --mode activity --live --days-ahead 7`.
  Activity auto-selected by weekday; **idempotency** skips if already held/paid
  (checks Manage Bookings) — so the backup only re-holds once the primary's
  1-hour hold has lapsed.
- **Manage:** `bash scripts/install_schedule.sh` (load/refresh) /
  `bash scripts/uninstall_schedule.sh` (remove). Check: `launchctl list | grep tennisbot`.
- **Logs:** `logs/activity-YYYYMMDD.log` and `logs/launchd-*.log`.
- ⚠️ **Mac must be awake** at those times or the job won't fire (it runs at next
  wake). Optional wake schedule via `sudo pmset repeat …` (see install script).
- **Trigger layer is portable:** to move off the Mac later, point an AWS trigger
  (EventBridge→Lambda/Fargate, or cron on a micro instance) at the same
  `scheduled_run.sh` / CLI — no booking-logic changes. Code lives in `deploy/`.

## Daily court-drop scheduler (BUILT — pending drop-time confirmation) ⏳
- `tennisbot drop` pre-warms a session, measures **server-clock skew** (`clock.py`,
  via the `Date` header — we fire on *their* clock), **spin-waits** to the drop
  instant (sub-ms accuracy verified), then runs the court flow. `--time HH:MM`
  overrides the fire time for testing; `--live` to book. Dry-run pipeline verified.
- **Window = 7 days (confirmed by direct observation 2026-07-01):** watched today+7
  (07-08, open, 32 slots) vs today+8 (07-09, never >0 all evening incl. ~21:00).
  So today+8 does NOT open the evening before — it's a clean 7-day rolling window.
- **Drop TIME still unknown but EARLIER than 19:00** (today+7 already open at 19:00
  every night we've watched). Leading hypothesis: **midnight (00:00)** roll-over.
- ⏸️ **Watcher PAUSED (dropwatch launchd removed) 2026-07-01.** The 3-hour watcher
  **collided** with the Wed 19:00 activity job (two concurrent sessions on the same
  account) and the 20:30 backup then failed ("page won't load" — booking app went
  sluggish for the account). Only the **4 activity jobs remain active**. Repo
  template kept (`deploy/launchd/com.tennisbot.dropwatch.plist`).
- **When resuming the drop-time hunt:** run gently (short window, avoid overlapping
  the Wed 19:00/20:30 & Sun 13:00/14:30 activity-job times — never stack sessions
  on the account). Next test idea: a single morning read of today+7 (open ⇒ midnight).
- **court-drop launchd job is DISABLED** (`deploy/launchd/com.tennisbot.court-drop.plist.DISABLED`).
  To enable after confirming: set `targets.yaml drop.local_time`, set the plist
  launch time to ~4 min before, rename (drop `.DISABLED`), `launchctl load -w`.
- ⚠️ **Latency caveat:** at T-0 the bot still does search→parse→click (~10-15s) in
  a real browser. Fine if the drop isn't fiercely contested; tonight's watcher
  also shows how fast slots deplete. If needed, optimise later (pre-warm search /
  raw-postback fast path) — see BACKLOG.

## Recommended next steps
1. **Read `logs/dropwatch-*.log`** to confirm the drop time, then enable the
   court-drop job (above).
2. Verify a **2-hour** live booking once (activity live path already verified).
3. Migrate triggers to **AWS free tier** when ready (logic already portable).

## Conventions
- Python venv at `.venv`; deps in `requirements.txt`; Playwright Chromium installed.
- `structlog` logging. Screenshots in `screenshots/`, session in `.session/`
  (both gitignored). Temp/experimental scripts go in the session scratchpad.
- **Git:** private GitHub repo `ignacio-montero/tennis-bot`, branch `main`.
  Secrets live in gitignored `.env`; also gitignored: `*.har`, `.session/`,
  `screenshots/`, `logs/`, `.claude/settings.local.json`. **Always re-run the
  staged-secret scan before pushing** (`git ls-files -z | xargs -0 grep -l ...`).
  launchd plists are committed as `__PROJECT_DIR__` templates, rendered by
  `install_schedule.sh` at install time (no hardcoded home paths in the repo).
