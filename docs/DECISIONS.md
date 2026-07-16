# Tennis-Bot — Decisions log

Notable decisions and their rationale, roughly chronological. Fuller
discussion of the architectural ones lives in
[ARCHITECTURE.md](ARCHITECTURE.md).

- **Hold-and-notify MVP** — the bot stops at Everyone Active's unpaid 1-hour
  hold and the user pays in the app. Removes payment/3DS/SCA/PCI from scope
  entirely while keeping the time-critical part (winning the slot) automated.
- **Personal use only, polite client** — own bookings under own account, rate
  limits respected, per-host concurrency caps. No scalping, no DoS-like
  behaviour, no CAPTCHA or payment-security bypasses — hard challenges
  escalate to a human via Telegram.
- **Hybrid "session-harvest" connectivity model** — Playwright does what only
  a browser can (anti-bot, login), then the harvested session can drive fast
  raw-HTTP calls. In practice Everyone Active's WebForms stack proved too
  fragile for raw httpx replay, so the **hot path stays on Playwright** for
  now; a raw-postback fast path is a possible later optimisation (BACKLOG §1).
- **Fail safe, fail loud** — every run reports to Telegram; a silent failure
  is the worst failure for a once-a-day time-critical bot.
- **Run from home (residential IP), not the cloud** — datacenter IPs get
  challenged far harder by anti-bot systems; the home line is the strongest
  free anti-bot lever. Cloud/datacenter hosting rejected for this reason; the
  future always-on host is the old home laptop (BACKLOG §7).
- **Portable trigger layer** — booking logic is a self-contained CLI;
  scheduling is a thin trigger (`launchd` → `scripts/scheduled_run.sh` → CLI).
  Moving to another host/cron later swaps only the trigger, never the logic.
- **MRMLogin.aspx is the primary Connect entry** (3-tier: reuse saved session
  → MRMLogin direct → SSO token harvest) — the Next.js account SPA renders
  empty / throttles under repetition, so it's the last resort.
- **Session persistence via Playwright `storage_state`** (`.session/`,
  gitignored) — fewer fresh logins = less throttling and less anti-bot
  exposure.
- **Fire on the server's clock, not ours** — skew measured from the HTTP
  `Date` header + spin-wait (~0.1 ms accuracy verified). Being right by your
  own watch loses the court.
- **Match surfaces/activities by results-row NAME, not dropdown codes** — the
  search flow is site + group + "Any", then climb from each availability
  button to its row name. The QuickBook sidebar is ignored (always shows the
  member's home-centre shortcuts).
- **Idempotent re-hold backup jobs** — the backup launchd job only re-holds
  once the primary's 1-hour hold has lapsed, checked against Manage Bookings,
  so it can never double-book.
- **Watcher paused (2026-07-01)** — the 3-hour drop-time watcher collided with
  the Wed 19:00 activity job (two concurrent sessions on one account) and
  degraded the account's booking app. Rule since: never stack sessions; hunt
  the drop time gently (see NEXT_STEPS.md).
- **`drop` camps through overload** — the 21:49–22:02 thundering-herd
  error-block observed at the likely drop time means a single-shot fire would
  fail; after the spin-wait the bot retries through overload/"not dropped yet"
  within a bounded `--retry-window` (default 90s).
- **watchd on the homelab (2026-07-12)** — the drop hunt moved off the Mac to
  a 24/7 Docker daemon on the always-on homelab (residential IP, consistent
  with the run-from-home decision). All-day coarse polling of both boundary
  dates settles midnight-vs-evening in one night; a fine-cadence hot window
  auto-tightens around each detected bracket; built-in blackouts around the
  Mac activity jobs enforce the one-session rule. Read-only by design — it
  never books. Same-day morning evidence (today+7 open at 10:37) already
  falsified "~21:50 releasing D−7".
- **Hot windows must handle midnight wraparound (2026-07-13)** — the first
  real bracket (Mon 20 Jul: closed 23:59:40 → open 00:19:40) supports the
  **midnight-D7** drop theory, but the auto-tightened window it implied
  (23:54–00:24) crosses midnight and `in_hot_window`'s naive
  `start <= now < end` check could never match it — watchd would have coasted
  through the decisive night on the 20-min coarse cadence. Fixed (wraparound
  branch), added a static 23:40–00:30 midnight window as primary (evening
  21:35–22:15 kept as fallback), and stopped re-polling dates already seen
  open so the fine cycle is ~35 s not ~60 s. Shipped as image 0.1.3 the same
  evening, ~1.5 h before the expected drop.
- **Midnight-D7 confirmed (2026-07-16)** — after the wraparound fix, two clean
  nights settled the drop time: **Mon 21 Jul** flipped open 14 Jul 00:00:07 →
  00:00:27 and **Thu 23 Jul** flipped 16 Jul 00:00:09 → 00:00:29 — both ~20 s
  brackets on the stroke of midnight, exactly 7 days ahead. Conclusion: courts
  release at **00:00:00 London on D−7**. This clears the last blocker on
  enabling the `drop` job (fire just past 00:00 on the homelab). One outlier
  logged alongside: **Tue 22 Jul** was seen opening ~04:16 on 14 Jul (20-min
  coarse-poll bracket) — off-clock and ~a day early vs strict D−7, likely a
  manual re-release or cancellation batch rather than the scheduled drop. Not
  enough to unseat the two clean confirmations, but the reason watchd stays
  running as a sentinel past the first live drop rather than being retired
  immediately.
- **<redacted> <redacted> (2026-07-16, deliberate)** — decided not to
  <redacted> despite it being <redacted>
  setup; accepted as-is. (Telegram token was rotated ✅.) `.env` stays
  gitignored, launchd plists committed as `__PROJECT_DIR__` templates rendered
  at install, staged-secret scan before every push.
