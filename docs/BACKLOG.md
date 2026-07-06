# Tennis-Bot — Backlog / Future Ideas

A running list of things we want to add later. Not committed work — a place to
park ideas so we can refer to them. Add freely; we'll prioritise when picking
the next build.

**Status key:** 🔵 planned · 🟡 idea/needs thought · ⚪ nice-to-have · ✅ done

Current state (done): manual `run-now`, hold-and-notify (1-hour unpaid hold +
Telegram). **Two centres** (Paddington, Westway), **courts + activities**,
**surface preference**, **two-consecutive-hours**, session persistence, discover
tool. See `../CLAUDE.md` / `ARCHITECTURE.md` / `NEXT_STEPS.md` /
`../recon/FINDINGS.md`.

✅ **Built 2026-06-28:** other EA centres (#1), activity bookings (#2), two
consecutive hours same court (#3), session persistence. Verified in dry-run;
activity & 2-hour *live* hold paths still need one `--live` confirmation.

---

## 1. Automation & scheduling
- ✅ **Activity auto-booking (scheduled)** — 4 `launchd` jobs (Wed 19:00/20:30,
  Sun 13:00/14:30) book "Tennis (adv)" 7 days ahead, LIVE, with idempotent
  re-hold. Portable trigger layer (`deploy/launchd` + `scripts/scheduled_run.sh`).
- ⏳ **Daily court-drop scheduler** — BUILT (`tennisbot drop`: server-skew +
  spin-wait, sub-ms accuracy; `clock.py`). Watcher running nightly to confirm the
  drop time; launchd job staged but disabled until then. See CLAUDE.md.
- ⚪ **Drop fast-path (latency)** — at T-0 the browser flow takes ~10-15s
  (search→parse→click). If the drop is contested, optimise: pre-warm the search
  page (pre-select site/group/surface so only date+submit remain at T-0), or
  replay the reserve postback via raw httpx with harvested cookies.
- ⏳ **Confirm exact drop time** (21:45 vs 22:00) — watcher built + scheduled
  nightly (`com.tennisbot.dropwatch` @ 21:25). Awaiting first night's
  `logs/dropwatch-*.log`, then enable the court-drop job.
- ⚪ **Self-recovery / retries** — if a run fails near the drop, retry within a
  bounded deadline; alert on give-up.

## 2. Booking logic
- 🟡 **Concurrency / racing** — fire attempts for ranked alternatives at the
  drop, first-win-cancels-rest (architecture already designed for this).
- 🟡 **Court preferences** — prefer/avoid specific courts (we already capture the
  court per slot; just needs preference rules).
- ✅ **Two consecutive hours, same court** *(user idea #3)* — `courts.two_hours`;
  books both or single-if-only-one-free; same court only; cap 2. Unit-tested.
  (Live second-hour hold path needs one `--live` confirmation.)
- ✅ **Activity bookings** *(user idea #2)* — `--mode activity`; group "Adult
  Activities", matched by row name; class-page parser. Sun 1300 verified dry-run;
  Wed 1900 configured. (Live hold path needs one `--live` confirmation.)
- ✅ **Court surfaces** — Synth/Tarmac (Paddington) via `courts.surfaces` +
  `preferred`/`enabled`. Other court types addable the same way.

## 3. New venues
- ✅ **Other Everyone Active centres** *(user idea #1)* — Westway (`0162`) added;
  any centre is now config-only (`site` + group + surfaces). Use
  `python -m tennisbot discover --centre NAME` to find codes.
- 🔵 **Hyde Park & Regent's Park (Park Sports)** — recon + provider. Expected to
  be a clean JSON API (unlike Gladstone). Deferred from MVP.

## 4. Reliability & politeness
- ✅ **Session persistence (storage_state)** — reuses login + Connect session
  (`.session/`); robust 3-tier Connect entry (reuse → MRMLogin → SSO).
- ⚪ **SQLite state + audit log** — record attempts, holds, outcomes; idempotency
  keys to guarantee no accidental double-booking across runs.
- ⚪ **Structured run history / heartbeat** — daily "armed, next drop at…" ping.

## 5. Notifications / UX
- ⚪ **Interactive Telegram** — buttons (e.g. "cancel this hold", "book anyway").
- ⚪ **Richer notifications** — include direct deep-link to pay, calendar invite.

## 6. Payments (only if ever needed)
- 🟡 **Payment automation** — currently fully manual (you pay in the app). If we
  ever want auto-pay: card-on-file / frictionless flows + 3DS human-in-the-loop.
  Deliberately out of scope while hold-and-notify is enough.

## 7. Deployment / scale
- 🔵 **Host on the old Windows laptop (home server) — NEXT (when ready).**
  User has an unused Windows laptop → use it as an always-on host (keeps the
  residential UK IP = anti-bot advantage; cloud/datacenter rejected for that
  reason). Also intended to host future projects.
  **Plan: containerize with Docker** (base `mcr.microsoft.com/playwright/python`;
  mount `.env`/`.session/`/`logs/` as volumes — secrets never in the image;
  `TZ=Europe/London`). Trigger = our existing thin layer: cron / Windows Task
  Scheduler runs `docker run … tennisbot run-now|drop` (booking logic unchanged).
  Host options: **(C)** keep Windows → Docker Desktop (WSL2) + Task Scheduler;
  **(D, preferred)** wipe → Ubuntu Server + Docker + cron (most robust, best
  portfolio). To decide: laptop RAM (≥8GB ideal) + Win version; keep-Windows vs Linux.
  Portfolio artifact: `deploy/docker/Dockerfile` + `docker-compose.yml` +
  `deploy/README.md` runbook (self-hosting / DevOps showcase).
  Ops gotchas (Windows): never-sleep/AC/lid-nothing, auto-restart on power loss,
  run tasks without login, defer Update reboots. Until host exists: Mac must be awake.
- 🔵 **Remote access / self-hosting (part of the home-server plan).** Reach the
  laptop from the Mac over the internet via a **mesh VPN — Tailscale** (free,
  WireGuard, **no router ports opened**), then **SSH** for admin (Linux) and
  optional **RDP/VNC over Tailscale** for GUI. Rule: never port-forward SSH/RDP
  to the open internet. Later: Cloudflare Tunnel if a service needs public access.
  **Ops/deploy loop:** code in git → edit on Mac → push → `ssh laptop 'git pull
  && docker compose up -d --build'`. Claude Code (on the Mac) can drive all of
  this over SSH (needs key-based auth; Edit/Write are local so deploy via
  git/rsync, or SSHFS-mount the remote FS to edit it as if local).
- ⚪ **Hardware fallback if the laptop doesn't work out:** refurb x86 mini PC
  (~£70-120) or Raspberry Pi 5/4 8GB + SSD.
- ⚪ **Dockerise** — package the CLI for the cloud host.
- ⚪ **Multi-user / multi-account** support.

## 8. Dynamic & collaborative preferences
- 🟡 **Dynamic preferences via Telegram** *(user idea #4)* — instead of editing
  `targets.yaml`, tell the bot in Telegram my preferred slots for the next 7
  days; it books against those. Lets me adjust to my schedule week to week.
  Needs: a Telegram command/conversation to capture prefs, persistence of the
  current week's prefs, and the scheduler reading them at drop time.
- 🟡 **Group availability booking** *(user idea #5)* — add the bot to a Telegram
  group with friends; it collects each person's availability and books a court
  only when **≥2 people are free at the same time**. Needs: per-user availability
  capture, an overlap/matching algorithm, and clear rules for who's "in" on a
  given booking. Builds on #4.
- 🟡 **Calendar integration (long term)** *(user idea #6)* — connect my calendar
  (Google/iCal) and book courts around my actual free/busy times automatically.
  Combine with #4/#5 for fully hands-off, schedule-aware booking.

## 9. Housekeeping
- 🟡 **Rotate secrets** — Telegram token ✅ rotated; **<removed>**
  (<removed>).
- ✅ **Published to GitHub** — private repo `ignacio-montero/tennis-bot` (branch
  `main`), secrets verified excluded, README + LICENSE + genericized paths.
- ⚪ **Tests** — more provider-parsing unit tests, mocked flows (have: selection
  logic + clock timing).

---

## Inbox (unsorted new ideas)
<!-- Drop quick ideas here; we'll file them into the sections above later. -->
-
