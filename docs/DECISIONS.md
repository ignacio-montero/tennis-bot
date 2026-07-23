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
- **Court-drop trigger = self-scheduling sidecar, not host cron
  (2026-07-22, supersedes the 0.2.x cron rehearsal)** — the nightly booker runs
  as a long-running `drop-loop` container (`restart: unless-stopped`) that
  sleeps to ~00:00−lead, books once, and loops. Replaces the earlier
  cron-fired one-shot (`run-drop.sh` + nacho's crontab + external watchd
  stop/start). Chosen because it (a) needs **no Docker socket and no host
  crontab** — deploys with the same `docker compose up -d` as watchd, no extra
  privilege; and (b) is **TZ-aware (`Europe/London`) so it needs no DST
  maintenance**, unlike a UTC crontab that must be hand-edited at the Oct
  clocks change. The one-session rule is enforced by a nightly **23:53–00:07
  in-image blackout in watchd** (0.3.0), which tears down its browser and yields
  the EA session — no cross-container control needed. **All 0.2.x booking logic
  is kept** (grid logging, `--after` slot selection, the `_next_drop`
  midnight-rollover date fix); only the trigger layer changed — the "portable
  trigger" principle in action. `drop-loop` reuses `_next_drop` for scheduling.
  Image builds moved to GitHub Actions on a `v*` tag (no more hand-built images).
  **Deployed 2026-07-23** (dry-run), watchd rolled to 0.3.0 for the blackout.
- **A failed drop must say WHY, not just "no slot" (2026-07-23, v0.3.1)** —
  `run_drop` reported `no slot after retries` for every failure, so "the release
  moved" and "I lost the race" were indistinguishable. That made the planned
  fallback ("if the drop stops booking, wake the watcher to re-find the drop
  time") **untriggerable** — the signal it depends on didn't exist. Since
  `row_full` is ambiguous (see CLAUDE.md: it means BOTH "not released yet" and
  "sold out"), a single read can't diagnose it; the discriminator is
  **observational across the whole camp window** — did we ever get *into* a
  timetable? `RunResult` now carries `grid_seen`/`n_avail`, `run_drop`
  accumulates them across all attempts (not just the last, so one read landing
  mid-postback can't decide it), and `diagnose_drop_failure` classifies:
  `never_opened` (release didn't happen → wake the watcher), `sold_out`
  (released, gone before we parsed → lost the race), `prefs_too_narrow` (slots
  WERE free, the filter excluded them → config problem, not a race loss). The
  third case is the one that was previously invisible and most misleading: it
  looks exactly like losing, so it would send you tuning timing when the real
  fix is widening `--after`. Code + counts go to Telegram, a `drop.diagnosis`
  log line, and `drop-outcomes.jsonl`, so a run of bad nights is queryable
  rather than anecdotal. Rejected: inferring from the `notes` strings
  `_run_court` already builds — parsing prose is fragile and the counts were
  being thrown away anyway.

## Cancellation Catcher + Telegram config — architecture decisions (2026-07-24)
Feature scope in [PRD-cancellation-catcher.md](PRD-cancellation-catcher.md);
full design in [ARCHITECTURE.md §8](ARCHITECTURE.md); contract in
[API_SPEC.md](API_SPEC.md). The load-bearing calls, with rationale + what we
rejected:

- **Catcher absorbs watchd (one polling service, not two).** watchd's drop-time
  mission is complete; its residual roles (regression detect + heartbeat) fall
  out of the catcher's own D+7 scan for near-free. Buys one fewer service and
  one fewer EA-session consumer. *Rejected:* coexist — keeps watchd's read-only
  *invariant* (it structurally cannot book), but pays a redundant scan and a
  third session consumer for a service whose main job is done. At single-user
  scale a spurious hold lapses in ~1h and doesn't touch the paid cap, so the
  weakened invariant is acceptable.
- **Regression signal = D+7 closed→open *timing*, not court volume.** "Lots of
  courts in D+7" is the signature of a *normal* drop and would false-positive
  nightly. Classified by combining the catcher's daytime D+7 observation with
  the sprinter's 0.3.1 `never_opened` diagnosis (moved vs broken). Fine 20-s
  re-discovery demoted from 24/7 to a break-glass tool. *Rejected:* volume
  heuristic (noisy); always-on fine polling (unnecessary once the time is known).
- **Preferences are Telegram-set and govern BOTH jobs.** One config document,
  shared. *Consequence accepted:* config leaves git (lose the `git log` audit
  trail) and becomes the first shared mutable state; and the **sprinter becomes
  day-filtered** — it skips the drop on nights where D+7 isn't a preferred day.
- **Persistence reuses existing JSON-on-a-volume patterns; no new datastore.**
  Mutable-doc (`bracket.json` idiom) for config + per-slot state; append-JSONL
  for history. *Rejected:* SQLite/Redis — gold-plating for a single-user bot
  writing a few small JSON files (revisit only if state goes relational).
- **New shared `tennisbot-config` volume: catcher rw, sprinter ro.** The ro
  mount re-establishes a safety boundary (sprinter can read prefs, not corrupt
  them). Single-writer ⇒ no lock; atomic temp-rename on write. Telegram handler
  co-located in the catcher process (single-writer + outbound long-poll = "no
  open ports").
- **Weekly cap counted from EA Manage Bookings, not a local counter.** EA is
  authoritative (manual bookings count too); `has_booking` already gives
  paid-vs-held. Semantics: paid-only, Monday reset, activity jobs excluded,
  default 3. *Rejected:* local tally — drifts if a court is booked outside the bot.
- **Live flip is Telegram-settable, guarded.** Owner chose convenience over a
  deploy-level barrier; fat-finger risk judged low. Guards: a confirm handshake
  on the `false→true` transition *only*, and mode always shown in
  heartbeat/read-back (closes the "invisible persisted state" gap a Telegram
  flag opens vs a git-visible compose flag). Net unchanged: hold-and-notify
  bounds the blast radius. *Rejected:* deploy-level-only (safer but less
  convenient; the guards close most of the gap).
- **One-session model unchanged in shape.** Catcher inherits all of watchd's
  blackouts; sprinter stays privileged. Three consumers (sprinter/catcher/Mac
  jobs) is the comfortable ceiling for fixed time-windows; a fourth would
  justify a real inter-process lock.
- **EA access validated by live probe (2026-07-23):** native day/time search
  filters + a whole-week grid (`mrmResourceStatus.aspx`) ⇒ ~1 search + ≤2 grid
  opens per centre/cycle. Two-stage filter (coarse server-side, fine
  client-side). Known build cost: a **new week-grid parser** (different page,
  per-(date,time) not per-court) feeding the existing single-date booking flow.
