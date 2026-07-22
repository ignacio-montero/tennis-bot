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

# 24/7 drop-time hunter daemon (runs on the homelab; local test needs PYTHONPATH):
PYTHONPATH=src .venv/bin/python -m tennisbot watchd --max-cycles 1 --no-notify
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
- `clock.py` — NTP/server-skew + spin-wait. `watch.py` — one-evening drop
  watcher. `watchd.py` — 24/7 drop-hunter daemon (runs on the homelab).

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
`TELEGRAM_CHAT_ID`. Telegram token rotated ✅. <redacted>
<redacted> (decided 2026-07-16) — accepted as-is despite <redacted>
share; not a pending task.

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

## Court-drop booker — SIDECAR built, awaiting deploy (dry-run) ✅⏳ (2026-07-22)
`tennisbot drop` pre-warms a session, measures server-clock skew, spin-waits to
the drop instant, then camps through overload within `--retry-window` (90s
default). **Drop time confirmed: 00:00:00 London on D−7** (21 & 23 Jul flipped
open within ~20 s of midnight; see DECISIONS.md).
- **Runs as the `tennisbot-drop` sidecar** — `tennisbot drop-loop`, a
  long-running container (`restart: unless-stopped`) that sleeps to ~00:00−lead,
  books once, and loops. **Replaced the earlier cron rehearsal** (`run-drop.sh` +
  nacho's crontab): no host cron, no Docker socket, TZ-aware so no DST edits;
  deploys with the same `docker compose up -d` as watchd. The one-session rule is
  the watchd **23:53–00:07 in-image blackout** (0.3.0), not an external
  stop/start. **Dry-run** (`--after 19:00`, no `--live`) — add `--live` to the
  compose command to book. Branches `claude/drop-sidecar-trigger` (both repos),
  unpushed; handover + runbook in `docs/NEXT_STEPS.md` / `deploy/docker/DROP.md`.
- ✅ **RESOLVED (2026-07-22 clean run): evening slots DO exist and parse
  correctly.** The old "no ≥19:00 availability at 00:45" worry is closed. The
  0.2.1 full-grid log for D+7 = 29 Jul showed Synth 08:00–17:00 only but
  **Tarmac 18:00 + 20:00 still available** at 00:45; `--after 19:00` correctly
  secured `20:00 Tennis Court 5` (dry-run). Earlier "no evening" reads were
  surface-specific + the pre-0.2.1 parser blind spot, not a real absence.
  Graduating to `--live` is now a go/no-go call, not a blocker (see NEXT_STEPS).
- New drop flags: `drop --after HH:MM` (earliest available court at/after a time,
  any court) and a midnight-rollover-safe target date (`_next_drop`). ⚠️ Outcomes
  are *meant* to persist to `drop-outcomes.jsonl` under `$DROP_STATE_DIR`, but as
  of 2026-07-22 the `tennisbot-drop-state` volume is empty — the jsonl isn't
  actually written (minor, non-blocking; cron.log is the source of truth).
- The old Mac **launchd** drop job is abandoned (homelab is the target); its
  disabled plist template stays in `deploy/launchd/` for reference only.

## Homelab: watchd drop-time hunter — DEPLOYED, LIVE ✅ (2026-07-12)
`tennisbot watchd` runs 24/7 in Docker on the homelab (container
`tennisbot-watchd`, image `ghcr.io/ignacio-montero/tennisbot-watchd:0.1.3`,
no published ports). It polls today+7/+8 coarsely all day (20 min), finely
(20 s) in hot windows (static **23:40–00:30** midnight-primary + 21:35–22:15
evening fallback + an auto-tightening window around the last detected bracket;
windows may cross midnight), skips dates already seen open, has built-in
blackouts around the Mac's activity-job times, and Telegram-pings the
closed→open flip with its bracket. **Evidence 2026-07-12→13: Mon 20 Jul
flipped open between 23:59:40 and 00:19:40 → midnight-D7 theory.**
- Runbook (build/push/update/rollback): `deploy/docker/DEPLOY.md`. Base image
  `mcr.microsoft.com/playwright/python:v1.60.0-noble`; playwright **pinned
  1.60.0** in requirements.txt — bump both together.
- Compose lives in the homelab repo
  (`~/Development/homelab/services/tennisbot-watchd/`); observations at
  `/data/watchd/observations.jsonl` + `bracket.json` in volume
  `tennisbot-watchd-state`; EA session in `tennisbot-watchd-session`.
- Check on it: `ssh homelab 'docker logs tennisbot-watchd --tail 20'`.
- ⚠️ One-session rule: the daemon and the Mac's activity jobs share the EA
  account; blackouts cover the job times — don't run manual sessions during
  hot windows.
- ⚠️ On restart it re-pings "already OPEN at first read" for open dates
  (tracker state is in-memory) — expected noise, not a drop.

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
