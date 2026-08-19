# 🎾 Tennis-Bot

Automated booking for public tennis courts in London. The bot secures a slot the
moment it's released (courts open on a rolling 7-day window) and pings Telegram
so I can pay in the operator's app — a **hold-and-notify** design that keeps
payment/3-D Secure entirely out of scope.

> Personal automation project. Built to book *my own* court under *my own*
> account, at human scale and respecting the site's rate limits. Shared as a
> portfolio piece. See [Disclaimer](#disclaimer).

---

## What it does

- **Books courts** at multiple centres of one booking provider,
  with a configurable **surface preference** (e.g. Synth → Tarmac fallback) and
  an optional **two-consecutive-hours, same-court** mode.
- **Books activities** (e.g. weekly "Tennis (adv)" sessions) released exactly a
  week ahead.
- **Times the "drop"** — measures the booking server's own clock and spin-waits
  to fire at the release instant.
- **Hold-and-notify** — creates an unpaid hold and sends a Telegram message (with
  a screenshot) to pay in the app. Idempotent: it never double-books.
- **Runs itself** — scheduled via `launchd`, with a deliberately thin trigger
  layer so it can move to a cloud cron later without touching booking logic.

## Why it's interesting (engineering highlights)

- **Reverse-engineered two stacked systems:** a modern Next.js + JSON auth API
  bridged via SSO into a legacy ASP.NET **WebForms** booking engine
  (`__VIEWSTATE`, async postbacks, MicrosoftAjax deltas). HAR captures →
  a request catalogue → a robust automation strategy.
- **Hybrid session-harvest:** a real browser (Playwright) clears login/anti-bot
  and the session is reused across runs (`storage_state`) to stay polite and
  avoid throttling.
- **Sub-second timing:** fires against the *server's* clock by measuring skew
  from the HTTP `Date` header, then a coarse-sleep + busy-wait
  (verified ~0.1 ms accuracy) — being right by your own watch loses the court.
- **Idempotency & safety rails:** target-seeking (never "book everything"),
  one-hold-per-run cap, and a "skip if already booked" check against the
  account's bookings.
- **Portable by design:** booking logic is a self-contained CLI; scheduling is a
  swappable trigger (`launchd` now → AWS later).

## Tech stack

Python · [Playwright](https://playwright.dev/python/) · httpx · PyYAML ·
structlog · pytest · macOS `launchd`. Browser automation + targeted HTTP.

## How it works

```
account login (Next.js/JSON) ──SSO token──▶ Connect WebForms booking engine
                                                   │
   advanced search (site · activity group · activity · date)
                                                   │
        results row match (surface / activity by name)
                                                   │
     court grid  ─or─  class page  ──▶  pick slot(s) by ranked preference
                                                   │
   click → "Book & Checkout" → unpaid hold ──▶ Telegram notify (pay in app)
```

For the full design, decisions, and roadmap see
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — plus
[docs/PRD.md](docs/PRD.md) (goal & scope),
[docs/DECISIONS.md](docs/DECISIONS.md) (decision log),
[docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) (current status), and
[docs/BACKLOG.md](docs/BACKLOG.md) (future ideas).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env          # then fill in your credentials (never committed)
```

`.env` (git-ignored) holds the operator login and Telegram bot token. Targets,
surfaces, activities and preferences live in
[`config/targets.yaml`](config/targets.yaml).

## Usage

```bash
# DRY-RUN by default (no hold). --live creates a real hold.
python -m tennisbot run-now --centre paddington --date 2026-07-04 --time 18:00
python -m tennisbot run-now --centre paddington --mode activity --date 2026-07-05
python -m tennisbot drop    --centre paddington --live      # spin-wait to the drop, then book
python -m tennisbot watch                                   # read-only: find the drop time
python -m tennisbot discover --centre "Swiss Cottage"       # find a new centre's codes

# Scheduling (macOS):
bash scripts/install_schedule.sh      # load the launchd jobs
bash scripts/uninstall_schedule.sh    # remove them
```

## Project structure

```
src/tennisbot/
  providers/everyoneactive.py  # login, SSO, search, parsers, hold
  runner.py                    # orchestration: court / activity / drop modes
  clock.py                     # server-skew + spin-wait
  watch.py  discover.py        # drop-time finder · code discovery
  config.py  models.py  notify/telegram.py  cli.py
deploy/launchd/                # schedule templates (rendered at install)
scripts/                       # wrappers + install/uninstall
tests/                         # selection logic + clock timing
```

## Testing

```bash
.venv/bin/python -m pytest -q
```

Deterministic unit tests cover slot-selection logic and the timing math (no network).

## Disclaimer

**This repository is published as an engineering artefact — a portfolio piece —
and is not intended for use against any third-party service.** The booking
provider is deliberately not named, and the concrete integration details
(hostnames, endpoints, control ids, activity codes) have been removed.

It automates booking *my own* court under *my own* account. It does not bypass
payment or authentication — 3-D Secure is intentionally out of scope and a human
pays in the provider's own app. It implements no anti-fingerprinting, no
challenge-solving, no CAPTCHA bypass and no proxy rotation, and it creates no
more load than a person clicking the same buttons would.

If you fork or adapt it, complying with the target service's terms is entirely
your responsibility.
