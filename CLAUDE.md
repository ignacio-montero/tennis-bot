# CLAUDE.md — read me first

Orientation for any Claude Code session working in this repo. (Claude Code loads
this file automatically at session start.)

## What this is
A personal bot that books public tennis courts / activities in London via
Everyone Active's Gladstone "Connect" platform, using a **hold-and-notify**
pattern: it secures an *unpaid 1-hour hold* and pings Telegram; the user pays in
the Everyone Active app. **Payment / 3DS is intentionally out of scope.**

## Docs map
- `docs/NEXT_STEPS.md` — **current status + recommended next steps (read first).**
- `docs/PRD.md` — goal, scope, success criteria.
- `docs/ARCHITECTURE.md` — design, connectivity strategy, roadmap.
- `docs/DECISIONS.md` — decision log with rationale.
- `docs/BACKLOG.md` — future ideas + status.
- `recon/FINDINGS.md` — reverse-engineering notes (IDs, flow, surfaces, layouts).
- `CLAUDE.md` (this file) — how to run + operational gotchas.

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
- `clock.py` — NTP/server-skew + spin-wait. `watch.py` — drop-time finder.

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
- ⚠️ **`row_full` IS AMBIGUOUS** — it means "results row shows Full", which is
  BOTH "date not released yet" AND "released but sold out". Beware when
  interpreting watcher output.
- **Be polite:** heavy repeated logins throttle the account SPA. Session reuse
  mitigates this; don't hammer. **Never stack concurrent sessions on the
  account** — avoid overlapping the activity-job times (Wed 19:00/20:30, Sun
  13:00/14:30).

## Secrets
`.env` (gitignored): `EA_EMAIL`, `EA_PASSWORD`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. Telegram token rotated ✅; ⚠️ **EA password rotation still
pending** (<removed>).

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
  (checks Manage Bookings) — the backup only re-holds once the primary's 1-hour
  hold has lapsed.
- **Manage:** `bash scripts/install_schedule.sh` (load/refresh) /
  `bash scripts/uninstall_schedule.sh` (remove). Check: `launchctl list | grep tennisbot`.
- **Logs:** `logs/activity-YYYYMMDD.log` and `logs/launchd-*.log`.
- ⚠️ **Mac must be awake** at those times or the job won't fire (it runs at next
  wake). Optional wake schedule via `sudo pmset repeat …` (see install script).
- **Trigger layer is portable:** to move off the Mac later, point another trigger
  (cron, Task Scheduler, EventBridge) at the same `scheduled_run.sh` / CLI — no
  booking-logic changes. Code lives in `deploy/`.

## Court-drop scheduler — built, disabled pending drop-time confirmation ⏳
`tennisbot drop` pre-warms a session, measures server-clock skew, spin-waits to
the drop instant, then camps through overload within `--retry-window` (90s
default). The launchd job is committed **disabled**
(`deploy/launchd/com.tennisbot.court-drop.plist.DISABLED`); the drop-time
watcher is **paused**. Full status, evidence so far, and the enable procedure:
`docs/NEXT_STEPS.md`.

## Homelab deployment (watchd) — bundle ready, not deployed 📦
`tennisbot watchd` can run 24/7 on the homelab: `deploy/docker/Dockerfile`
(base `mcr.microsoft.com/playwright/python:v1.60.0-noble`; playwright is now
**pinned 1.60.0** in requirements.txt — bump both together), `.env.example`,
and the runbook `deploy/docker/DEPLOY.md`. Compose service lives in the homelab
repo (`services/tennisbot-watchd/`, registered in its `compose.yaml`). Image
`ghcr.io/ignacio-montero/tennisbot-watchd:0.1.0` builds + smoke-tests OK locally
(amd64) but is **not pushed**; homelab-repo files are **uncommitted**.
⚠️ One-session rule still applies: mind the Mac's activity-job times if watchd
runs on the homelab.

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
