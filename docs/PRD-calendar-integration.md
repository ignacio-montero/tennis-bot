# PRD — Calendar-driven booking (iCloud "Tennis" calendar)

_Status: draft 2026-07-27 (PM). Next: Architect for the iCloud mechanics._
_Owner: single-user (Nacho). Builds on the existing prefs/rules engine
(ARCHITECTURE §8; `prefs.py` rules, the drop `run_drop_loop`, the catcher)._

## 1. Problem & goal

Today the owner tells the bot *when to play* by maintaining Telegram `/rule`s.
That's great for a stable weekly pattern, but real availability shifts week to
week, and editing rules by hand is friction. The owner already keeps their
schedule in a calendar — so let the bot read it and book around it, with **no
rule-editing required**, while keeping rules available as an alternative.

**Success:** the owner drops time blocks into a dedicated calendar and the bot
wins courts inside them, with the same safety rails (dry-run by default, weekly
cap, hold ceiling) as today — and without ever booking a slot the owner didn't
ask for.

## 2. Target user

The single owner, same account and Telegram control surface as the rest of the
bot. No multi-user or shared-calendar scope.

## 3. The model (agreed)

- A **dedicated "Tennis" calendar** the owner alone controls and views.
- **Every event in it is a booking request.** The event's **time range is the
  search window** for that date: "win me a court on this date, inside this
  window." No title/keyword parsing; no other calendar is consulted; no
  free/busy computation — the owner has already done that filtering by choosing
  to place the event.
- Conceptually, **each event is a dynamic, date-specific `/rule`**: an event
  "Sat 2 Aug 10:00–12:00" == a rule `Sat 10:00-12:00` scoped to that one date,
  authored in the calendar instead of Telegram, and gone once past. So this is a
  **new source of per-date windows feeding the existing matcher / drop /
  catcher**, not a new booking engine.

## 4. MVP scope (in)

1. **Read-only calendar ingestion.** The bot reads events from the owner's
   Tennis calendar that fall in the **D0–D+7** window (the same horizon the
   catcher and drop already work). Read-only path (mechanism = Architect's call;
   chosen for lowest risk since write-back isn't urgent — see §6).
2. **Event → window mapping.** Each event yields `(date, earliest = event start,
   latest = event end)`, fed into the existing rule-matching logic. Recurring
   events count: expanded occurrences in the window are treated like any other
   event.
3. **A mode switch (calendar ⊕ rules), set from Telegram.** Exactly one source
   drives bookings at a time. In **calendar mode** the calendar provides the
   windows and `/rule`s lie dormant. In **rules mode** the calendar is ignored
   and behaviour is exactly as today (backward-compatible). Default: **rules
   mode** (no behaviour change on upgrade).
4. **Length from the existing `/length 1|2`.** The calendar says *when*; the
   global length setting says *how long*. A block must be ≥ the chosen length to
   fit it (a 2h length needs a ≥2h block); if two consecutive hours don't fit the
   window, fall back to a single hour, same as today's two-hours logic.
5. **Centre from the existing `/centres`.** A block says *when*, not *where*;
   the bot pursues the window at the configured centre(s).
6. **Priority under the cap.** When more blocks fall in a week than the weekly
   cap allows, **weekend blocks (Fri/Sat/Sun) are pursued before weekday
   blocks**; within a tier, **earliest date, then earliest time** wins. (Rules
   carry explicit list-order priority; calendar events have none, so this is the
   built-in ranking. Fixed default for MVP; could become configurable later.)
7. **Safety rails unchanged.** Weekly cap and the 5-hold ceiling still bound
   calendar-mode bookings. Dry-run vs live is still the existing
   `catcher_live` / `drop_live` switches — calendar mode does **not** imply live.
8. **Fail-safe on an unreadable calendar (non-negotiable).** If calendar mode is
   on and the calendar can't be reached or parsed, the bot books **nothing** —
   it never falls back to "book everything." Same spirit as the degraded-prefs →
   dry-run rule. The failure is surfaced (heartbeat / `/status`), because silence
   must never hide "I couldn't read your intent."
9. **`/status` reflects the active mode** and, in calendar mode, something useful
   about what it sees (e.g. count of upcoming blocks, last successful read).

## 5. Explicitly out of scope (for the MVP)

- **Part 2 — writing booked courts back to the calendar.** Deferred ("someday,
  no rush"). Needs an *authenticated, writable* iCloud connection (a different,
  higher trust boundary than read-only) — see §6. Revisit as a phase 2.
- **Per-event length override** (e.g. a "2h" tag on the event) — would
  reintroduce title parsing we deliberately avoided. Length stays global.
- **Configurable priority** — weekend-first is a fixed default for now.
- **Free/busy from other calendars** — explicitly not done; the owner curates
  the dedicated calendar.
- **Multi-user / group / shared-calendar** booking.
- **Running calendar and rules simultaneously** — it's a switch, not a blend.

## 6. The read/write access boundary (why part 2 is separate)

Reading and writing an iCloud calendar are different trust levels, which is why
part 1 ships first and part 2 waits:
- **Read (part 1):** can use a one-way, read-only source (e.g. a published/secret
  subscription link) — no account login, low blast radius. Mild privacy note: a
  public-subscription URL is a "secret link" — anyone with it can read the
  calendar, so it must be treated as a secret (untracked `.env` on the box).
- **Write (part 2):** requires an *authenticated* connection to the actual
  iCloud account (an app-specific password stored on the box) with write scope —
  strictly more powerful and more sensitive.
The Architect will choose the concrete read mechanism (subscription `.ics`/webcal
vs. authenticated CalDAV read). If part 2 were urgent we'd build the
authenticated path once (read+write together); since it isn't, the read-only
path is preferred for MVP.

## 7. User stories

- As the owner, I want to drop time blocks into a private Tennis calendar and
  have the bot win a court inside each, **so I don't have to edit rules every
  week**.
- As the owner, I want to **switch between calendar mode and rules mode** from
  Telegram, so I can use whichever fits a given week.
- As the owner, when I've marked more blocks than my cap allows, I want
  **weekend slots prioritised**, so my limited bookings land when I most want to
  play.
- As the owner, I want the bot to **book nothing if it can't read my calendar**,
  so a sync/auth glitch never turns into unwanted holds.
- As the owner, I want calendar mode to respect my existing **centre, length,
  cap, hold ceiling, and dry-run/live switches**, so nothing about safety
  changes.

## 8. Acceptance criteria (the Tester's checklist later)

1. Calendar mode ON + a Tennis event `Sat 2 Aug 10:00–12:00` in D0–D+7 ⇒ the bot
   pursues a court on 2 Aug within `[10:00, 12:00)` at the configured centre(s),
   at the configured length; dry-run logs "would book", live creates the hold.
2. Calendar mode ON + no events in the window ⇒ books nothing, stays silent.
3. Calendar mode ON + calendar unreachable/unparseable ⇒ books nothing **and**
   surfaces the failure; never books outside an event.
4. Calendar mode ON ⇒ a stale/leftover `/rule` never causes a booking (rules
   dormant).
5. Rules mode ON ⇒ the calendar is never read; behaviour is byte-for-byte
   today's (backward-compatible; default on upgrade).
6. More events than the weekly cap ⇒ weekend (Fri/Sat/Sun) events are pursued
   before weekday events; within a tier, earliest date then earliest time;
   weekly cap and 5-hold ceiling are respected.
7. `/length 2` + a block shorter than 2h ⇒ falls back to a single hour within the
   window (or nothing if even 1h can't be placed); never books outside the block.
8. Recurring event ⇒ each occurrence in D0–D+7 is honoured like a one-off.
9. `/status` shows the active mode and (in calendar mode) a read-health signal.
10. Dry-run/live is unchanged: calendar mode alone never books live; only
    `catcher_live` / `drop_live` do.

## 9. Open questions (for the Architect)

- **Read mechanism:** subscription `.ics`/webcal (no auth, simplest) vs.
  authenticated CalDAV read (app-specific password). Trade privacy/simplicity vs.
  a path that also unlocks part 2 later.
- **Mode + calendar config in prefs:** where the `mode` and the calendar
  URL/secret live (prefs.json `mode` field; secret in `.env`, not prefs).
- **Timezone & all-day events:** interpret event times in `Europe/London`; decide
  an all-day "Tennis" event = "any time that day" (widest window, like
  `<day> any`) vs. ignore — confirm the intended reading.
- **Refresh cadence:** read the calendar fresh at each drop pre-arm and each
  catcher cycle (so a phone edit lands next cycle), mirroring how prefs are read.
- **Switch command wording** (`/mode calendar|rules` vs. `/calendar on`) — a
  Designer/Architect surface detail; must show in `/help` and `/status`.
