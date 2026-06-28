# Tennis-Bot — Architecture

> A fully automated, hands-off bot to book public tennis courts in London the
> moment they become available (7 days in advance).
>
> **Targets**
> - **Hyde Park** & **Regent's Park** — *Park Sports* (custom web platform)
> - **Paddington Recreation Ground** — *Everyone Active* (Gladstone MRM backend)
>
> **Status (2026-06-28):** IMPLEMENTED for Everyone Active. `run-now` CLI books
> courts (Paddington + Westway, surface preference, optional 2 consecutive hours)
> and activities, **hold-and-notify** (unpaid 1-hour hold → Telegram → user pays
> in the app; payment/3DS out of scope). **Activity booking is automated** via 4
> `launchd` jobs (Wed/Sun) with idempotent re-hold. See `CLAUDE.md` for the live
> operational guide. Park Sports (Hyde/Regent's) still deferred.
>
> **Portable trigger layer:** booking logic is a self-contained CLI; scheduling is
> a thin trigger (`deploy/launchd` → `scripts/scheduled_run.sh` → CLI). Moving to
> AWS later swaps only the trigger (EventBridge/cron → same script), not the logic.

---

## 0. Operating principles & scope

- **Personal use only.** This automates *your own* bookings under *your own*
  account. It does not scalp, resell, hoard, or deny slots to others beyond
  what you'd grab manually. Respect each platform's rate limits — be a polite
  client, not a DoS.
- **No payment-security bypass.** 3D Secure / SCA is a bank-enforced
  cryptographic step. We **do not and cannot** bypass it. We minimise its
  friction legitimately (saved cards, merchant-initiated transactions) and put
  a human in the loop for the rest. See §4.3.
- **Fail safe, fail loud.** Every run reports its outcome to Telegram. A silent
  failure is the worst failure for a time-critical bot.

---

## 1. Connectivity: Reverse-engineered API vs. Headless Browser

This is the most important architectural decision and it differs **per platform**.

### 1.1 The two approaches

| | **Reverse-engineered XHR/JSON API** (raw `httpx`) | **Headless browser** (Playwright) |
|---|---|---|
| **Speed** | ⭐⭐⭐ Milliseconds. One TCP/TLS connection, HTTP/2, no rendering. Decisive in a "drop" race. | ⭐ Hundreds of ms–seconds. Must boot Chromium, render DOM, run JS, click. |
| **Resource cost** | Tiny. Runs anywhere, even a Pi. | Heavy (~300–700MB RAM per browser). |
| **Concurrency** | ⭐⭐⭐ Fire dozens of async requests trivially. | ⭐ Each browser context is expensive; hard to parallelise many. |
| **Robustness to UI changes** | Brittle to *API* changes (rarer) but immune to CSS/DOM churn. | Brittle to DOM/selector changes (frequent). |
| **Anti-bot exposure** | High if done naively — missing headers/TLS fingerprint screams "bot". | Lower — a real browser passes most JS challenges and fingerprinting. |
| **Effort to build** | High up front (must reverse the protocol, auth, CSRF, payloads). | Lower up front (you automate what a human does). |
| **3DS / SCA handling** | Painful — payment iframes & redirects are designed for browsers. | Natural — it *is* a browser; can surface the challenge to a human. |

### 1.2 Recommendation — a **Hybrid "session-harvest" model**

Neither extreme wins outright. The staff-level answer is to **use Playwright to
do what only a browser can (defeat the initial JS/anti-bot challenge, log in,
and reach the payment surface), then harvest the authenticated session and
switch to raw `httpx` for the speed-critical booking call.**

```
Playwright (slow, robust)            httpx (fast, lean)
┌──────────────────────────┐        ┌───────────────────────────┐
│ 1. Boot browser          │        │ 4. Reuse harvested cookies │
│ 2. Pass Cloudflare/login │  ───►  │    + headers + tokens      │
│ 3. Harvest cookies,      │        │ 5. Fire booking POST at    │
│    CSRF tokens, headers,  │        │    the exact drop instant  │
│    TLS-warm session       │        │ 6. Retry/concurrency here  │
└──────────────────────────┘        └───────────────────────────┘
```

**Per-platform plan:**

- **Park Sports (Hyde / Regent's)** — *API-first.* Custom modern platforms of
  this type almost always have a clean JSON XHR layer behind the React/Vue
  front-end. Capture it (see `scripts/capture_har.py`), reverse the
  availability + reservation endpoints, and drive booking over `httpx`. Use
  Playwright only to bootstrap/refresh the session. **This is where we win the
  millisecond race.**

- **Everyone Active / Gladstone MRM (Paddington)** — *Browser-first, then
  harvest.* Gladstone's "MRM"/"Connect" stack is a legacy, server-rendered
  app (ASP.NET-style postbacks, hidden form tokens, sometimes `__VIEWSTATE`,
  stateful multi-step flows). Its protocol is fragile and tightly coupled to
  the rendered page. Start by driving it with Playwright end-to-end; once the
  exact reservation request is understood and *if* it proves stable, promote
  the final booking step to `httpx`. Expect to keep this one mostly on
  Playwright longer than Park Sports.

**Why this beats picking one:** we get browser-grade *robustness & anti-bot
survival* at the dangerous edges (login, Cloudflare, payment) and raw-API
*speed & concurrency* at the one moment that decides whether we get the court.

### 1.3 Discovery workflow (how we reverse the APIs)

1. Open the site in a real browser with DevTools → Network, filter to XHR/Fetch.
2. Perform a real booking once, manually, and **export a HAR file**.
3. `scripts/capture_har.py` parses the HAR into a request catalogue: auth flow,
   availability endpoint, slot-hold/reserve endpoint, payment init, required
   headers, CSRF/anti-forgery tokens, cookie dependencies.
4. Replay each request in isolation with `httpx` to find the *minimal* set of
   headers/cookies/tokens the server actually validates.
5. Codify that minimal contract into the provider adapter (§3.4).

---

## 2. Core Architecture

### 2.1 High-level component diagram

```
                        ┌─────────────────────────────┐
                        │        CLI / Config          │
                        │  targets.yaml, .env, secrets │
                        └──────────────┬──────────────┘
                                       │
              ┌────────────────────────▼────────────────────────┐
              │                  ORCHESTRATOR                     │
              │  - computes each target's "drop instant"          │
              │  - NTP clock sync & skew correction               │
              │  - schedules pre-warm + fire jobs                 │
              └───┬───────────────┬───────────────┬──────────────┘
                  │               │               │
        ┌─────────▼──────┐ ┌──────▼───────┐ ┌─────▼─────────┐
        │ SESSION MANAGER │ │ BOOKING ENGINE│ │  NOTIFIER     │
        │ Playwright      │ │ asyncio/httpx │ │  Telegram     │
        │ bootstrap+harvest│ │ concurrency  │ │  (HITL too)   │
        └─────────┬───────┘ └──────┬───────┘ └─────┬─────────┘
                  │                │               │
        ┌─────────▼────────────────▼───────────────▼─────────┐
        │              PROVIDER ADAPTERS (per platform)        │
        │   ParkSportsProvider   |   EveryoneActiveProvider    │
        └─────────┬────────────────────────────────────────────┘
                  │
        ┌─────────▼─────────┐
        │   PERSISTENCE      │  SQLite: sessions, bookings,
        │   (state + audit)  │  attempts, audit log, idempotency keys
        └────────────────────┘
```

### 2.2 The Scheduler (the "drop" at T-7 days)

Courts release on a rolling window: the slot for *day D* becomes bookable at a
fixed wall-clock time on *day D-7* (e.g. 07:00 London). The scheduler's job is
to be **authenticated and primed milliseconds before that instant** and to fire
**the moment it opens — not a second early (rejected) nor a second late (gone)**.

**Two-tier scheduling:**

1. **Outer trigger (OS-level, survives reboots):** `launchd` on macOS (or `cron`
   on Linux) wakes the process well ahead of each drop. On a sleeping Mac we use
   `pmset schedule wake` / `caffeinate` so the machine is awake at T-minus a few
   minutes. This guarantees the bot is *running* even after a crash/reboot.
2. **Inner trigger (in-process, precise):** once running, **APScheduler** holds
   the job and we hand off to a **busy-wait/spin loop in the final ~2 seconds**
   for sub-second precision (OS schedulers and `sleep()` are not reliable at the
   millisecond level).

**Clock discipline (critical):**
- Sync against NTP on start; never trust the local clock blindly.
- Measure **server skew**: read the `Date` response header from the target
  during pre-warm and compute the offset between *our* clock and *theirs*. We
  fire against **the server's clock**, not ours — being right by your watch and
  wrong by theirs loses the court.
- Fire at `drop_instant + skew + safety_epsilon` (small positive epsilon, e.g.
  +50–150ms, tuned empirically — firing fractionally late but accepted beats
  firing early and getting a hard reject that costs a retry).

**Timeline of a single drop (e.g. Hyde Park, 07:00):**

```
T-15 min : launchd wakes machine; orchestrator starts
T-5  min : SESSION MANAGER bootstraps via Playwright
           (Cloudflare + login), harvests cookies/CSRF
T-90 sec : warm httpx HTTP/2 connection to host (TLS handshake done early);
           re-read Date header → recompute skew
T-30 sec : pre-fetch availability to confirm target slots & slot IDs
T-2  sec : enter spin-wait loop
T-0      : BOOKING ENGINE fires concurrent reserve requests
T+Xms    : first success wins → proceed to payment; cancel the rest
T+...    : NOTIFIER reports result (or escalates SCA to human)
```

### 2.3 Concurrency (winning within seconds)

- **asyncio + httpx** with a shared HTTP/2 client (multiplexed, connection
  pre-warmed). No browser in the hot path.
- **Strategy fan-out:** the user specifies *ranked* preferences (e.g. "Court 2
  at 18:00, else any court 18:00–20:00"). At T-0 we fire reserve attempts for
  the **top-N candidate slots concurrently**; **first confirmed hold wins**, all
  other in-flight attempts are cancelled immediately to avoid accidental
  double-booking.
- **Idempotency:** every attempt carries an idempotency key; the persistence
  layer records "we are holding/booked slot X" before we ever fire a second
  candidate, so a race can never leave us paying for two courts.
- **Bounded retry with jitter:** on transient failures (429/5xx/network), retry
  a small number of times with exponential backoff + jitter, *within a hard
  deadline* (a slot held by someone else won't free up — don't hammer).
- **Per-host politeness cap:** a max concurrent-requests ceiling per platform so
  we look like an eager human, not an attack.

### 2.4 Notifications & Human-in-the-Loop (Telegram)

`python-telegram-bot` gives us both push *and* interactive callbacks:

- **Success:** "✅ Booked Hyde Park, Court 2, Sat 5 Jul 18:00–19:00. Ref #…".
- **Failure:** reason + what was tried (audit-grade, so we can tune).
- **HITL for SCA (see §4.3):** when a 3DS challenge appears, the bot pushes an
  alert with an inline **"Approve / I've completed it"** button and, if needed,
  a field to relay a one-time passcode — so you finish the bank step on your
  phone in seconds while the bot holds the slot.
- **Heartbeat/health:** a daily "armed and ready, next drop at …" message so you
  know it's alive *before* you're relying on it.

---

## 3. Tech Stack & Project Structure

### 3.1 Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | Best ecosystem for both Playwright and async HTTP. |
| Browser automation | **Playwright** (async) | More reliable than Selenium; great session/cookie control; `patchright`/stealth plugins for anti-bot. |
| Fast HTTP | **httpx** (HTTP/2, async) | Connection reuse, multiplexing, fine header control for the hot path. |
| Anti-detection | **patchright** / `playwright-stealth` | Patches the obvious automation fingerprints. |
| Scheduling | **APScheduler** (in-proc) + **launchd/cron** (OS) | Precise inner timing + robust outer trigger. |
| Clock | **ntplib** + server-`Date` skew correction | Fire against the server's clock. |
| Notifications / HITL | **python-telegram-bot** | Push + interactive buttons for SCA. |
| Config & validation | **Pydantic v2** + **YAML** | Typed config, clear target definitions. |
| State / audit | **SQLite** (via SQLModel/SQLAlchemy) | Zero-ops, perfect for single-node; stores sessions, bookings, attempts. |
| Secrets | **.env** (local) → keyring/age later | Card-on-file handle & tokens never in git. |
| Logging | **structlog** | Structured, queryable run logs — essential for debugging a once-a-day event. |
| Packaging / deploy | **Docker** + **uv** | Reproducible; lift-and-shift to a VPS when scaling. |
| Tests | **pytest** + **respx** (mock httpx) | Test provider contracts without hammering live sites. |

### 3.2 Folder structure

```
Tennis-Bot/
├── ARCHITECTURE.md            # this document
├── README.md
├── pyproject.toml             # uv / dependencies
├── .env.example               # secrets template (no real values)
├── config/
│   └── targets.yaml           # venues, courts, preferred times, drop rules
├── src/
│   └── tennisbot/
│       ├── __init__.py
│       ├── cli.py             # entrypoints: arm, run-now, test, status
│       ├── config.py          # Pydantic settings + targets loader
│       ├── models.py          # domain models: Target, Slot, Attempt, Booking
│       ├── clock.py           # NTP sync, server-skew, drop-instant math, spin-wait
│       ├── scheduler/
│       │   ├── orchestrator.py# computes drops, wires jobs, owns the lifecycle
│       │   └── triggers.py    # APScheduler + launchd/pmset helpers
│       ├── session/
│       │   ├── manager.py     # Playwright bootstrap: Cloudflare, login, harvest
│       │   └── store.py       # persist/restore cookies, tokens, TTLs
│       ├── providers/
│       │   ├── base.py        # Provider interface (the contract all venues meet)
│       │   ├── parksports.py  # Hyde Park & Regent's (API-first)
│       │   └── everyoneactive.py # Paddington (browser-first → harvest)
│       ├── booking/
│       │   ├── engine.py      # async fan-out, first-wins, retry, idempotency
│       │   └── payment.py     # payment init + 3DS/SCA HITL orchestration
│       ├── notify/
│       │   └── telegram.py    # push + inline-button HITL
│       └── persistence/
│           ├── db.py          # SQLite engine/migrations
│           └── repositories.py# sessions, bookings, attempts, audit
├── scripts/
│   └── capture_har.py         # turn a recorded HAR into a request catalogue
├── deploy/
│   ├── Dockerfile
│   └── launchd/com.tennisbot.plist  # macOS scheduled wake + run
└── tests/
    ├── test_clock.py
    ├── test_engine.py
    └── providers/
```

### 3.3 The Provider interface (key abstraction)

All venues hide behind one contract so the engine doesn't care *how* a booking
is made (raw API vs. browser):

```
class Provider(Protocol):
    async def bootstrap_session(self) -> Session            # Playwright path
    async def is_session_valid(self, s: Session) -> bool
    async def get_availability(self, s: Session, day) -> list[Slot]
    async def hold_slot(self, s: Session, slot) -> Hold      # the hot-path call
    async def confirm_and_pay(self, s, hold) -> Booking      # may raise ScaRequired
```

`ScaRequired` is a typed exception the booking engine catches to trigger the
Telegram HITL flow.

### 3.4 `config/targets.yaml` (illustrative shape)

```yaml
- name: "Hyde Park"
  provider: parksports
  drop:
    days_before: 7
    local_time: "07:00"
    timezone: "Europe/London"
  preferences:
    - { courts: [2, 3], time: "18:00", duration: 60 }
    - { courts: any,    time_range: ["18:00","20:00"], duration: 60 }
  days_of_week: [Sat, Sun]
```

---

## 4. Roadblocks & Mitigations

### 4.1 Session expiration

- **Bootstrap late, not early.** Harvest the session in the pre-warm window
  (T-5 min), not hours ahead, so cookies/tokens are freshest at fire time.
- **Validate then refresh.** `is_session_valid()` does a cheap authenticated
  ping at T-90s; if stale, re-bootstrap immediately (we have budget).
- **Persist + reuse** sessions in SQLite with TTLs to avoid re-login storms and
  to reduce anti-bot exposure from repeated fresh logins.
- **Token re-scrape.** CSRF/anti-forgery tokens are read fresh from the live
  page/availability response right before firing, never cached across drops.

### 4.2 Cloudflare / anti-bot

- **Let a real browser do the hard part.** Playwright (with stealth/patchright)
  clears JS challenges and managed-challenge cookies (`cf_clearance`) that raw
  HTTP can't. We then **harvest `cf_clearance` + the matching User-Agent** and
  reuse them on the `httpx` hot path — but **the TLS/JA3 fingerprint and the
  User-Agent must match the browser that earned the clearance**, or Cloudflare
  rejects it. (If JA3 matching proves necessary, swap `httpx` for a
  fingerprint-matching client such as `curl_cffi` on that platform.)
- **Residential IP** (your home line — the chosen free hosting) is the strongest
  lever; datacenter IPs get challenged far harder. This is *why* we run locally
  first.
- **Behave human:** realistic headers, sane request cadence, per-host
  concurrency caps, no early-firing. Don't generate traffic patterns that look
  automated.
- **Graceful escalation:** if a hard CAPTCHA appears that stealth can't clear,
  fall back to **Telegram HITL** — surface it to you to solve once, then
  continue. We do not build CAPTCHA-farm bypasses.

### 4.3 3D Secure / SCA — handled, **not bypassed**

> SCA is mandatory in the UK/EU and enforced cryptographically by your card
> issuer. There is no legitimate way to "skip" it. The architecture's goal is to
> make it **rare and fast**, never to defeat it.

> **MVP path — payment is fully out of scope (hold-and-notify).** On platforms
> that support an unpaid hold (confirmed for Paddington/Everyone Active), the bot
> stops at the hold and the user pays in the app. This removes 3DS/SCA, card
> storage, and PCI concerns from the prototype entirely. The strategy below
> applies only *if/when* we later automate payment, or for a platform that forces
> inline payment to confirm (TBD for Park Sports / Hyde & Regent's).

**Layered strategy, best case first:**

1. **Card-on-file / saved payment method.** Do the painful first booking
   manually so the card is tokenised and saved with the merchant. Many
   subsequent payments then qualify as **frictionless** (low-risk, low-value,
   recurring merchant) and complete with no challenge at all — this is the
   *normal*, intended path and our primary design target.
2. **Merchant-initiated / stored-credential transactions** where the platform
   supports them — these are exempt from step-up SCA by design.
3. **Frictionless flow exemptions.** Low-value (sub-£30-ish) and
   transaction-risk-analysis exemptions mean many tennis bookings won't
   challenge at all once a card is on file.
4. **Human-in-the-loop fallback (the safety net).** If the issuer *does* throw a
   challenge:
   - The booking engine raises `ScaRequired` while **holding the slot**.
   - The notifier fires an **instant Telegram alert** with an inline button and,
     if it's an OTP-type challenge, a way to **relay the code** you receive.
   - If it's an app-approval challenge, you tap "approve" in your banking app;
     the bot polls the payment status and **completes the booking** once cleared.
   - This keeps the hot path automated 95%+ of the time and needs you for only
     the few seconds a real bank challenge is live.

**Explicitly out of scope:** spoofing 3DS responses, OTP interception, or any
attempt to forge issuer authentication. Those are fraud and we won't build them.

---

## 5. Step-by-step logic flow (one drop, end to end)

```
1.  launchd wakes the machine and starts the orchestrator (T-15m).
2.  clock.sync_ntp(); compute each target's drop_instant in UTC.
3.  For the next due target:
4.    session = SessionManager.bootstrap()         # Playwright: CF + login
5.    persist session (cookies, tokens, UA, cf_clearance) to SQLite.
6.    warm httpx HTTP/2 connection; read Date header → server skew. (T-90s)
7.    if not provider.is_session_valid(): re-bootstrap.
8.    availability = provider.get_availability(target_day)  # confirm slot IDs (T-30s)
9.    rank candidate slots by user preferences.
10.   spin-wait until drop_instant + skew + epsilon.        # (T-2s → T-0)
11.   booking.engine fires hold_slot() for top-N candidates concurrently.
12.   first successful Hold wins; cancel the rest; write idempotency record.
13.   try provider.confirm_and_pay(hold):
14.       on success      → persist Booking, Telegram ✅ with reference.
15.       on ScaRequired  → Telegram HITL; on approval, resume & confirm.
16.       on failure      → bounded retry within deadline; else Telegram ❌.
17.  log everything (structlog) + write audit rows.
18.  orchestrator schedules the next drop and idles / lets the machine sleep.
```

---

## 6. Phased execution roadmap

> Each phase is independently demonstrable. We don't build the rocket before the
> go-kart works.

### Phase 0 — Recon & scaffolding (no booking yet)
- Manually book each venue once with DevTools open; export HARs.
- Run `capture_har.py`; document the request catalogue per platform.
- Decide, per platform, the *actual* split between httpx and Playwright.
- Stand up repo skeleton, config models, SQLite, structlog, Telegram hello-world.
- **Exit criterion:** we can authenticate and *read live availability* for all
  three venues from code (read-only, no booking).

### Phase 1 — Single-venue happy path — **"hold-and-notify"** (Paddington first)
> **Key MVP decision:** the bot's finish line is *securing an unpaid hold*, not
> paying. On Everyone Active, clicking an available slot creates an unpaid
> booking that appears in the user's app; the user completes payment there in a
> tap. This **decouples slot acquisition (time-critical, safe to automate) from
> payment (no time pressure, sidesteps 3DS/SCA, card data, and PCI entirely)**.
> The bot races for the court; the human pays. See §4.3.
- Implement `EveryoneActiveProvider` up to and including `hold_slot()` — stop at
  the unpaid hold (the `mrmProductStatus.aspx` basket). No payment automation.
- Implement the booking engine for a *single* candidate slot (no concurrency).
- Wire Telegram: on success, push slot details + a deep link to pay in the app.
- **Must confirm:** the hold TTL (how long before an unpaid hold auto-cancels)
  and that the hold is exclusive (locks the court from others pending payment).
- **Exit criterion:** bot secures a real unpaid hold on demand (`run-now`) and
  Telegram pings the user to complete payment in the app.

### Phase 2 — Timing & the drop
- Implement `clock.py`: NTP, server-skew, spin-wait.
- Implement scheduler (APScheduler) + launchd wake plist.
- Dry-run against a known drop time; measure our firing accuracy vs. server.
- **Exit criterion:** bot autonomously wins a court at the 7-day drop, unattended
  except for any SCA tap.

### Phase 3 — Concurrency & preferences
- Ranked preferences in `targets.yaml`; top-N concurrent fan-out, first-wins,
  idempotency, retry/jitter, per-host caps.
- **Exit criterion:** bot reliably secures the *best available* slot, not just
  *a* slot, under contention.

### Phase 4 — Everyone Active / Paddington
- Implement `EveryoneActiveProvider` (browser-first, harvest where stable).
- Handle Gladstone's multi-step/postback/token quirks.
- **Exit criterion:** all three venues bookable through the same engine.

### Phase 5 — Hardening & payment friction reduction
- Save card-on-file; verify frictionless/MIT path; refine SCA HITL.
- Cloudflare resilience (JA3 matching via `curl_cffi` if required).
- Health heartbeats, retries, alerting, structured audit dashboards.
- **Exit criterion:** weeks of unattended runs with bookings landing and clear
  reporting on every outcome.

### Phase 6 — Scale-out (optional, later)
- Dockerise (already structured for it); deploy to a cheap UK VPS or Oracle
  Cloud Always-Free if you want it off your home machine.
- Multi-user / multi-account support if ever needed.
- **Exit criterion:** runs 24/7 off-laptop with the same reliability.

---

## 7. Key risks & open questions (to revisit)

- **JA3/TLS fingerprinting** on the harvested-session hot path — may force
  `curl_cffi` on one or both platforms. Confirmed only after Phase 0 recon.
- **Gladstone MRM stability** — likely the most brittle integration; budget the
  most maintenance here.
- **Frictionless-payment eligibility** is issuer-dependent — we'll learn the
  real SCA frequency only with live runs (Phase 1/5).
- **Drop-time semantics** per venue (exact second, server timezone, whether
  availability is pre-staged) — to be pinned down empirically in Phase 0/2.
- **ToS** — periodically re-check each platform's terms; keep behaviour
  personal-scale and polite.
```
