# Feature PRD — Cancellation Catcher + Telegram-configurable preferences

_A bounded feature PRD. Product context: [PRD.md](PRD.md). Design will land in
[ARCHITECTURE.md](ARCHITECTURE.md) / [API_SPEC.md](API_SPEC.md). Status:
[NEXT_STEPS.md](NEXT_STEPS.md)._

Owner: PM persona · Drafted 2026-07-23 · Status: **draft for review** (no build
until scope agreed).

---

## 1. Problem & goal

Today the bot only wins courts at **one instant**: the D−7 midnight drop
(`tennisbot-drop`). But courts also free up **throughout** the open 7-day window
when other members cancel — at unpredictable times, no schedule. Nothing
captures those: `watchd` *sees* them but only sends a Telegram, and by the time
a human reacts the slot is gone. So a whole class of winnable courts is missed.

Separately, **every preference is baked into `config/targets.yaml` in the
container image**. Changing "what court do I want this week" means editing git
and redeploying — too heavy for a preference that changes week to week.

**Goal.** Two linked capabilities:

1. **Cancellation Catcher** — a self-scheduling service that periodically scans
   the open window, and when a court matching my preferences has freed up,
   books it (hold-and-notify) — respecting a weekly cap so it can run unattended
   without over-holding.
2. **Telegram-configurable preferences** — I set what I want (days + hours, slot
   length, location) from my phone, and it governs **both** the catcher **and**
   the midnight drop booker, with no redeploy.

**Success looks like:** I tell the bot once "Tue & Thu after 18:00 at
Paddington, 1 hour," and over the following weeks it holds courts for me both at
the drop and from cancellations, never exceeds my weekly cap, and every booking
arrives as a Telegram to pay in the app — with zero silent failures and no more
site load than a considerate human.

## 2. Target user

Just me — same as the parent PRD. Personal, single-account, polite by design.
The Telegram bot is single-user; `TELEGRAM_CHAT_ID` is the sole authorised
sender (any other chat id is ignored).

## 3. Background the design must honour (validated 2026-07-23)

A live read-only probe of Everyone Active settled the key unknowns — the design
must build on these facts, not re-litigate them:

- **The EA search form filters by day and time natively** (`Include Days`
  Mon–Sun, `Preferred Times` Morning/Afternoon/Evening). My preference model
  maps onto the site's own controls — narrowing happens server-side.
- **One search + one grid-open returns a whole week** for a surface
  (`mrmResourceStatus.aspx`: rows = times, columns = the 7 dates,
  cells = Available / Not-Available). So a full-window scan of one centre costs
  ~1 search + ≤2 grid opens per cycle — *fewer page loads than watchd does
  today*, not the 24/cycle a naïve per-date/per-surface loop implied.
- **Cost of the win:** that week grid is a *different page* from the single-date
  courts×times grid the current parser handles, and it shows availability per
  (date, time) but **not per court**. So detection needs a **new week-grid
  parser**, and booking drills from a chosen (date, time) into the *existing*
  single-date flow to pick the court and hold. That seam is the main new build.

> **Product↔engineering seam (teach):** runtime-editable preferences mean config
> stops being "code in git" and becomes **mutable state on a volume** — we trade
> the git audit trail for phone control. And because that state governs *both*
> containers, it is the **first shared mutable state** in a system that has so
> far been deliberately share-nothing. One writer (Telegram handler) + two
> infrequent readers is the benign end of that problem, but it's a property we
> adopt on purpose, not by accident. The *how* (a state store, and a Telegram
> **long-poll** rather than a webhook so we keep "no open ports") is the
> Architect's call — flagged here so scope includes it.

## 4. MVP scope

### 4.1 Cancellation Catcher (new service)
- [ ] A self-scheduling **sidecar** (same pattern as `tennisbot-drop`: long-run
      container, `restart: unless-stopped`, no host cron, no Docker socket) that
      wakes every **30 min** (agreed — balances catching cancellations promptly
      against being polite to EA), scans, books if matched, sleeps.
- [ ] Scans the **whole open window (D0–D+7)** for the configured centre(s),
      applying the day/time preferences via the native search filters, and reads
      the week grid to find free (date, time) slots that match.
- [ ] When a matching slot is found, books it **hold-and-notify** — reusing the
      existing single-date booking path (court selection + `_commit_hold`).
- [ ] Honours **slot length**: 1 hour, or 2 consecutive hours same court
      (reuses existing `two_hours` logic).
- [ ] Respects the **weekly hold cap** (see 4.3) and existing **idempotency**
      (never re-book a date already held/paid — `has_booking`).
- [ ] **Dry-run first** (default, no `--live`), exactly like the drop rollout.
- [ ] Obeys the **one-session-per-account** rule — inherits watchd's blackout
      approach; the Architect decides whether it shares the existing blackout
      windows or needs its own.
- [ ] Telegram on **book** (with screenshot + pay prompt) and on **error**.
      Poll cycles that find nothing are **silent** (no per-cycle noise). Plus
      **one daily heartbeat** (like watchd's 09:00 "alive"), so silence never
      means "dead".
- [ ] **Lapsed-hold re-booking** per the rule in 4.4 — a hold I never pay for
      is re-grabbed a bounded number of times, more persistently if it was held
      overnight while I was asleep.

### 4.2 Telegram-configurable preferences (governs BOTH jobs)
- [ ] Set from Telegram, persisted to a shared store, read by both the catcher
      and the drop booker — no redeploy:
  - **Days + hours** I want a court (e.g. "Tue, Thu, after 18:00").
  - **Slot length** — 1 or 2 hours.
  - **Location(s)** — Paddington and/or Westway (extensible to any EA centre).
- [ ] **Weekly cap** is also settable from Telegram (**default 3**).
- [ ] A **read-back** command so I can ask "what are my current settings?" and
      get the active config.
- [ ] Only `TELEGRAM_CHAT_ID` is honoured; messages from any other sender are
      ignored (single-user authorisation).

### 4.3 Weekly hold cap (agreed semantics)
- Cap counts **paid** bookings only — an unpaid hold that lapsed does **not**
  count (I didn't get the court).
- **Excludes** the scheduled Wed/Sun activity bookings — those are a separate
  commitment, not part of this budget.
- **Monday reset** (not a rolling 7 days).
- Default **3**; settable via Telegram.

> **Seam (teach):** the catcher creates *holds*, but the cap counts *paid*
> bookings — and I pay later, in the app, outside the bot's sight. So enforcing
> the cap means the catcher must **read Manage Bookings** to count this week's
> paid courts before booking. "Paid" is observable (the existing `has_booking`
> already distinguishes paid vs held); the mechanic is the Architect's to spec.

### 4.4 Lapsed-hold re-booking (agreed semantics)
An unpaid hold lapses after ~1 hour. If I haven't paid, the slot frees again —
and whether the catcher should re-grab it depends on **whether I was awake to
pay**:

- **Daytime holds (first held 09:00–23:00 London):** re-book the same slot **at
  most once**, then leave it for that day. If I ignored one re-hold while awake,
  I don't want it.
- **Overnight holds (first held after 23:00 London):** **keep re-booking every
  cycle until 09:00** the next morning. A hold made at 23:30 lapses at ~00:30
  while I'm asleep; persistent re-holding keeps the slot mine until I'm up to
  pay. After 09:00 the daytime rule takes over (one more re-hold, then release).

> **Seam (teach):** this needs **per-slot memory across cycles** — for each slot
> it holds, the catcher must remember *when it first held it* and *how many times
> it has re-booked*, surviving restarts. That's more state than "did I already
> book this date" (a yes/no); it's the second concrete thing (after shared
> config) pushing the design toward a real state store, not in-memory flags.

## 5. Explicitly out of scope (deliberately)

- **Telegram *commands* (imperative actions).** Telegram *configures* parameters;
  it does not take orders like "book court 3 tomorrow at 19:00". Command-style
  control is a possible later phase, not MVP — it carries a heavier
  authorisation burden.
- **Changing the drop *schedule* via Telegram.** The 00:00 D−7 drop time is a
  *discovered fact*, not a preference. It stays in code/config.
- **Midnight-drop booking at Westway.** Westway's `drop.local_time: 21:45` is
  unverified (only Paddington's 00:00 is confirmed). The **catcher** may target
  Westway (cancellations need no drop time), but pointing the **drop booker** at
  Westway stays out of scope until its drop time is confirmed empirically.
- **Court-level preference within a surface** (prefer/avoid specific courts) —
  nice-to-have, not MVP. The week grid is per (date, time), not per court.
- **Payment / 3DS / SCA** — always out of scope; a human pays in the app.
- **Multi-user / multi-account**, and **Park Sports venues** (Hyde/Regent's) —
  deferred, as in the parent PRD.

## 6. User stories

1. As the owner, I want a service that **watches the whole 7-day window for
   cancellations** and books a court matching my preferences, so I catch freed
   slots I'd otherwise miss between drops.
2. As the owner, I want to **set my desired days and hours from Telegram**, so I
   can change what I'm chasing week to week without touching code.
3. As the owner, I want to **set slot length (1 or 2 hours) from Telegram**, so
   the bot books the right duration for how I want to play.
4. As the owner, I want to **choose the centre(s) from Telegram** (Paddington
   and/or Westway), so I can shift where I'm looking without a redeploy.
5. As the owner, I want **one set of preferences to govern both the drop booker
   and the catcher**, so I configure my intent once and both act on it.
6. As the owner, I want the bot to **never exceed my weekly cap of paid
   bookings**, so unattended running can't over-hold my account.
7. As the owner, I want to **ask the bot what its current settings are** and get
   a clear read-back, so I can trust what it will do tonight.
8. As the owner, I want a **Telegram message on every booking and every error**
   (but silence on empty polls), so I'm informed without being spammed.

## 7. Acceptance criteria (become the Tester's checklist)

**Cancellation Catcher**
- [ ] Given a free slot matching my prefs anywhere in D0–D+7, a poll cycle books
      it (dry-run: "would book"; live: a real hold) and Telegrams the outcome.
- [ ] Given no matching free slot, the cycle books nothing and sends **no**
      Telegram, then sleeps to the next interval.
- [ ] Given a date already held/paid, the catcher does **not** re-book it.
- [ ] Given the weekly paid-booking count is already at the cap, the catcher
      books nothing and (once) notifies that the cap is reached.
- [ ] Activity bookings (Wed/Sun) do **not** count toward the cap.
- [ ] The cap counter **resets on Monday**.
- [ ] A crash in one cycle does not kill the loop (it logs, notifies, continues)
      — same resilience as `run_drop_loop`.
- [ ] A daytime lapsed hold (first held 09:00–23:00) is re-booked **at most
      once**, then not again that day.
- [ ] An overnight lapsed hold (first held after 23:00) is re-booked **every
      cycle until 09:00**, then reverts to the daytime rule.
- [ ] A **daily heartbeat** Telegram is sent once per day even when nothing is
      booked.
- [ ] Per-cycle EA load stays at ~1 search + ≤2 grid opens per centre (no
      per-date/per-surface fan-out).
- [ ] The service holds **no open ports** and never stacks a second EA session
      against the drop booker or activity jobs.

**Telegram configuration**
- [ ] Setting days/hours, slot length, or location from Telegram updates the
      shared store and is reflected in the **next** cycle of **both** jobs,
      without a redeploy.
- [ ] The read-back command returns the currently-active settings.
- [ ] A message from a chat id other than `TELEGRAM_CHAT_ID` changes nothing.
- [ ] Settings **survive a container restart** (persisted, not in-memory).
- [ ] An invalid setting (e.g. a malformed time) is rejected with a helpful
      Telegram reply and leaves the previous config intact.

**Safety / rollout**
- [ ] Ships **dry-run**; `--live` is a separate, deliberate flip after watching.
- [ ] No secret ever leaves the server; config store lives on a volume, not git.

## 8. Resolved decisions (2026-07-23)

- **Lapsed-hold re-booking** → §4.4: once/day in daytime; persistent until 09:00
  for overnight holds.
- **Daily heartbeat** → **yes**, one/day (folded into 4.1).
- **Interval** → **30 min**, fixed (not Telegram-settable in MVP — keeps scope
  tight; changeable in config if it ever needs tuning).

## 9. Open questions (for the Architect)

1. **Does the catcher absorb watchd, or coexist?** watchd's drop-time mission is
   done and the catcher overlaps its "watch availability" role. **Owner leans to
   a single service** (catcher absorbs watchd — keeping the regression-detector +
   heartbeat roles), but wants to settle it with the Architect. This is the
   first architectural decision to make; much else hangs off it.
2. **Drop ↔ catcher overlap.** If the catcher already held next Saturday from a
   cancellation, the midnight drop must skip Saturday — the shared cap + the
   `has_booking` idempotency check should cover this, but confirm no double-hold.
3. **Config schema & inbound Telegram transport** — long-poll vs webhook, and
   the store's shape/location. Long-poll preferred to keep "no open ports". The
   schema becomes the `API_SPEC.md` contract between the Telegram handler and
   both jobs.
4. **State store** — §4.4 (per-slot re-book memory) and §4.3 (weekly paid count)
   both need durable per-slot/per-week state surviving restarts. Decide its shape
   and whether it's the same store as the shared config.

## 10. Handoff

When this scope is agreed, the natural next step is the **Architect persona**:
design the shared config store + inbound-Telegram transport, the week-grid
parser and the detect→book seam, the cap-counting mechanic, and the catcher's
place in the one-session model — landing in `ARCHITECTURE.md` and `API_SPEC.md`
(the config schema is the contract between the Telegram handler and both jobs).
