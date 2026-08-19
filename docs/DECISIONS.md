# Tennis-Bot — Decisions log

Notable decisions and their rationale, roughly chronological. Fuller
discussion of the architectural ones lives in
[ARCHITECTURE.md](ARCHITECTURE.md).

- **Hold-and-notify MVP** — the bot stops at the provider's unpaid 1-hour
  hold and the user pays in the app. Removes payment/3DS/SCA/PCI from scope
  entirely while keeping the time-critical part (winning the slot) automated.
- **Personal use only, polite client** — own bookings under own account, rate
  limits respected, per-host concurrency caps. No scalping, no DoS-like
  behaviour, no CAPTCHA or payment-security bypasses — hard challenges
  escalate to a human via Telegram.
- **Hybrid "session-harvest" connectivity model** — Playwright does what only
  a browser can (anti-bot, login), then the harvested session can drive fast
  raw-HTTP calls. In practice the provider's WebForms stack proved too
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
- **Secret-handling posture (2026-07-16)** — all credentials live in a
  gitignored `.env` and are never committed; `.env.example` carries names and
  placeholders only. launchd plists are committed as `__PROJECT_DIR__`
  templates rendered at install time, so no local paths leak. A staged-secret
  scan runs before every push. Rotation status for individual credentials is
  tracked privately rather than in this repo.
- **Court-drop trigger = self-scheduling sidecar, not host cron
  (2026-07-22, supersedes the 0.2.x cron rehearsal)** — the nightly booker runs
  as a long-running `drop-loop` container (`restart: unless-stopped`) that
  sleeps to ~00:00−lead, books once, and loops. Replaces the earlier
  cron-fired one-shot (`run-drop.sh` + the host user's crontab + external watchd
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

- **Pre-warm lead + blackout raised together; retries added (2026-07-24,
  v0.3.2)** — the sidecar's first live firing died in a 45s cold-login timeout
  86s before the drop, while 53 slots released normally. Root cause is
  structural, not bad luck: the sprinter's session idles ~24h so it *always*
  cold-logs-in, and `lead_min=3` afforded ~one attempt with no retry. Changed as
  ONE coupled unit: `lead_min 3→10`, `DROP_BLACKOUT 23:53→23:45-00:07`, and
  `prewarm_attempts=3` bounded by `prewarm_floor_s=25` (never still logging in at
  the instant). The lead and the blackout are two halves of one handshake — the
  blackout must be open before the sprinter wakes, or raising the lead
  *manufactures* session contention. Both kept in **code** (not the compose
  command) so they can't drift, with a test that derives the wake time from
  `lead_min` and fails if the blackout no longer covers it. *Rejected for now:*
  sharing a session volume so the catcher keeps the sprinter's session warm —
  attacks the real root cause but crosses the deliberate share-nothing session
  boundary; deferred pending its own evidence.
- **`no_observation` added as a fourth failure code (2026-07-24)** — a crash
  before arming says *nothing* about the drop time, and conflating it with
  `never_opened` would have concluded "the drop moved" when the drop was fine
  (53 slots released). Emitted from the crash path, which previously wrote **no**
  outcome at all — so the most diagnostic failures were invisible in
  `drop-outcomes.jsonl`. Also elevates the independent observer from
  "regression detector" to **ground truth whenever the sprinter fails**: watchd's
  00:07 poll was the only evidence distinguishing "our bug" from "drop moved".

## Prefs schema v2 — per-day rules, two switches, split ceilings (2026-07-26)
Code is shipped (`prefs.py`, `telegram_commands.py`, `catcher.py`, `runner.py`);
contract in [API_SPEC.md §1.2a/§1.6/§2.3](API_SPEC.md), design in
[ARCHITECTURE.md §8.2/§8.6/§8.8](ARCHITECTURE.md).

- **Booking rules are an ORDERED priority list; add-order = priority.** v1's
  single global window couldn't express "weeknights after 18:00 **and** Saturday
  10:00–15:00". v2 makes `rules` an ordered tuple of `Rule(days, earliest,
  latest)`; a slot is bookable iff **some** rule admits it, and the catcher books
  the highest-priority match first (`rules[0]` wins). Ranking intent *by the
  order you add rules* needs no separate priority field or UI — the list order IS
  the ranking. The flat `days`/`earliest`/`latest` are **retained as a derived
  mirror** of a single rule (kept in lock-step by `Prefs.__post_init__`) so every
  v1-era reader and the single-window `/status` display keep working unchanged;
  a v1 doc **migrates** on read (flat window → one rule), it never degrades.
  *Rejected:* a separate integer `priority` per rule (redundant with list order,
  more to validate); ripping out the flat readers (a bigger, riskier change than
  mirroring). *Cost:* `/days`/`/window` are now a sole-rule shorthand that must
  **reject** when ≥2 rules exist (a single flat window is meaningless against a
  list), and reorder currently means clear + re-add (a REORDER command is on the
  BACKLOG).
- **Two independent live switches, with a conservative v1 migration.** The single
  `live` flag became `catcher_live` + `drop_live`: two live bookers are two
  separate consent decisions, and the owner may want one armed while the other
  stays dry. `/catcher on` / `/drop on` each run their own CONFIRM handshake
  (tagged with which switch it arms); `/live off` is a **panic path** that turns
  both off with no speed bump (the safe direction never has friction); `/live on`
  arms both. `Prefs.live` survives only as a read-only "is either armed"
  convenience for display, never serialised. A v1 `live:true` migrates to
  **`drop_live` only** — the drop was the sole v1 booker, so its consent carries
  forward faithfully, but the catcher is a NEW booker and silently arming it
  would run a second live booker the owner never consented to. Safe migration =
  under-arm, not over-arm. *Rejected:* one flag for both (can't dry-run one
  booker while the other is live); migrating `live:true` to both (arms an
  unconsented booker).
- **Positive court-ID confined to the PAID cap only (fail-open vs fail-closed).**
  Identifying "is this Manage-Bookings row a court" has two failure modes: the
  generous *negative rule* (any non-activity booking is a court) can OVER-count;
  *positive court-ID* (row text contains a configured surface token, e.g.
  `"Tennis - Synth"`) can UNDER-count on a token miss. These fail in opposite
  directions, so each consumer is routed to the identification whose failure is
  SAFE for it. **Only the paid weekly cap** uses positive-ID — a paid *swim* must
  not eat the court budget (money-safety wants precision) — with a fallback to
  the negative rule + a loud `court_token_divergence` log when a token is missing,
  so even its failure becomes an over-count (skip a winnable court), never an
  over-book. **Idempotency** (`held_court_date_keys`) and the **holds ceiling**
  (`count_unpaid_holds`) deliberately keep the negative rule: their unsafe
  direction is UNDER-counting (re-book a slot we already hold → a hold storm; or
  a ceiling that never trips), and over-counting merely makes them skip/hold-off.
  The general lesson: precision is not globally better — pick the identification
  whose failure mode is harmless for that specific guard. *Rejected:* one shared
  court-ID everywhere (whichever you pick is unsafe for half the callers).
- **`max_holds` is a SEPARATE ceiling from the weekly cap.** `weekly_cap` bounds
  money (PAID court bookings per Monday-reset week); `max_holds` bounds
  concurrent UNPAID holds parked right now (default 5, `/holds`). They cap
  different resources and can trip independently — you can be under your paid cap
  yet already sitting on too many unpaid holds. The holds ceiling is a **global
  stop** checked at the highest-priority bookable candidate (so that is the one
  held off), whereas a capped week does not stop the walk (a later, un-capped
  week must still be reachable — the D0–D+7 scan straddles Monday). Both count
  from the authoritative Manage-Bookings view, not a drifting local tally; either
  at 0 pauses booking. *Rejected:* folding holds into the weekly cap (conflates
  spend-this-week with parked-now; a single number can't pause one without the
  other).

## Drop enforces `latest`, rule reorder, rich `/help` (2026-07-27, v0.6.0)

- **Drop now enforces a rule's `latest` ceiling** — the catcher honoured both
  ends of a rule's window but the midnight drop honoured only the floor. Added a
  `before_time` (EXCLUSIVE upper bound, matching prefs `latest`) mirroring
  `after_time` through the single-date engine, filtered ONCE at the candidate
  pool so it composes with the ranked-prefs, `after_time`, and two-consecutive-
  hours paths without a per-branch filter. *Rejected:* filtering in each
  selection branch (three chances to forget the 2-hour second-hour case).
- **A configured rule is authoritative for the drop's window** — once any rule
  matches the drop's weekday, BOTH bounds come from that rule; the CLI `--after`
  is only a no-prefs fallback. A ceiling-only rule (`Sat -12:00`) uses a 00:00
  floor, not `--after 19:00` — otherwise the band inverts (`[19:00, 12:00)` is
  empty) and the drop silently books nothing (critic S1). Empty rules keep the
  exact CLI-driven behaviour. *Known scope:* the drop pursues only the highest-
  priority rule per weekday; the catcher considers all. Safe (drop ⊆ catcher);
  fuller union-of-same-day-rules fix is in BACKLOG §2.
- **`/rule move <from> <to>`** — rule priority is list order, so reordering
  needed a first-class command (was: `/rules clear` + retype). Pop-then-insert
  at the 1-based destination; `from==to` no-op; `<2 rules` rejected.
- **`/help` lists every command with a one-line definition** — the surface grew
  past the old terse subset; the help is now the discoverable source of truth,
  grouped See / Where &amp; when / Limits / Go live, static (no user text → no
  escaping risk), under Telegram's 4096-char limit.

## Calendar-driven booking — Architect decisions (2026-07-27)

Scoped in [PRD-calendar-integration.md](PRD-calendar-integration.md); design in
[ARCHITECTURE.md](ARCHITECTURE.md) §9. Read-only MVP (part 1); write-back (part 2)
deferred.

- **`.ics` subscription URL over CalDAV — least privilege wins.** The owner drives
  booking from a dedicated iCloud "Tennis" calendar. Read options were a public
  **`.ics` subscription link** (read-only, no login, exposes only *this one*
  calendar) vs **CalDAV** (live + read/write, but an app-specific password grants
  read/write to the owner's *entire* iCloud calendar set). Chose `.ics`. *Why:*
  the owner explicitly did not want the bot able to touch all their calendars, and
  the Tennis calendar is low-sensitivity — least privilege beats the "build once
  for write" convenience when write is "someday, no rush". *Rejected:* CalDAV-now
  (broadest secret on an always-on box, for a write feature we don't need yet).
  *Accepted costs:* read-only forever on this path (part 2 needs a separate
  authenticated door) and Apple's publish-cache lag (tolerable — blocks are placed
  well ahead of booking).
- **A `WindowSource` (Strategy pattern), not a second booking engine.** A calendar
  event == a date-scoped `Rule`, so `rules` and `calendar` are two producers of the
  same `(date, earliest, latest, priority)` windows behind one interface; the
  matcher/cap/ceiling/live-gates are untouched. `prefs.mode` (`"rules"` |
  `"calendar"`, default `"rules"`) selects the source. *Why:* keeps the feature a
  single seam instead of a parallel code path, and makes "calendar events are just
  dynamic rules" literally true. *Rejected:* a bespoke calendar booking path
  (duplicates the cap/hold/live logic — divergence risk).
- **Secret vs config split preserved.** Calendar URL → untracked `.env`
  (`TENNISBOT_CALENDAR_ICS_URL`); it's a "secret link" and prefs.json is
  echoed/logged. `mode` → prefs.json (Telegram-set). *Rejected:* URL in prefs
  (would leak via `/status`/logs).
- **All-day event = "any time that day"** (full-day window), matching a `<day>
  any` rule. *(owner decision)*
- **Fail-safe = book nothing, fail LOUD.** Read-OK-empty is silent; read-FAILED
  (network/parse/URL-unset) books nothing AND raises a rate-limited Telegram alert
  (≤ once/day), never falling back to stale rules or "book everything". *Why:* an
  unreadable intent must pause booking loudly, same posture as degraded-prefs →
  dry-run. *(owner decision: "loud fail")*
- **Weekend-first priority in calendar mode** — calendar events carry no
  owner-authored order (unlike rules' list index), so the ranking is `(weekend
  Fri/Sat/Sun first, then earliest date, then earliest time)`. Fixed default for
  MVP. *(owner decision)*
- **Untrusted-input hardening (built 2026-07-27, from the critic pass).** The
  `.ics` is external network data, and a `try/except` bounds *exceptions*, not
  *time or memory*. So we PREVENT the non-raising hazards rather than catch them:
  reject sub-daily `RRULE` before `dateutil.between()` expands it (else a
  `FREQ=SECONDLY` recurrence hangs the loop); stream the fetch with a byte cap
  (else a giant body OOM→SIGKILL→crash-loops the container) and a wall-clock
  deadline (else a slow-drip stalls a cycle); redact the secret URL in a
  catch-all. *Rejected:* relying on the existing per-cycle try/except (a hang/OOM
  never reaches it). A pathological event fails the read LOUD; a merely-malformed
  one is skipped — the `except CalendarReadError: raise` before the generic
  `except` keeps the loud signal from degrading into silent skip.
- **A long-running daemon must re-authenticate, not just navigate (2026-07-30,
  v0.7.1).** The catcher established its EA session once (`_session_ready`
  one-shot) then only `go_home()`d each cycle; when the Connect cookie expired
  (hours), every scan bounced to MRMLogin and it silently did nothing for 3 days.
  Fix: re-affirm the session each reused cycle (`_connect_live()` probe) and
  re-auth on a lapse via `enter_connect`'s MRMLogin path — the drop's robust
  login, which is exactly why the *drop* never had this bug (fresh login nightly).
  *Rejected:* a lazy heal only inside `scan()`'s surface loop — `get_bookings()`
  runs first and returns `[]` silently on a dead session (under-counts the cap),
  so the heal belongs at the shared `_ensure_session` chokepoint. This is the
  "fail loud" principle applied to a daemon: the heartbeat exists precisely
  because a stuck `no_bookable` looks identical to "nothing to book".
