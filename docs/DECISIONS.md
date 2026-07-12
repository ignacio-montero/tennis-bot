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
- **Secrets hygiene** — `.env` gitignored; launchd plists committed as
  `__PROJECT_DIR__` templates rendered at install; staged-secret scan before
  every push. Telegram token rotated ✅; **EA password rotation still
  pending** (was <removed>).
