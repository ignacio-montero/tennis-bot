# Tennis-Bot — API / Contract Spec

The **internal contracts** between components. Tennis-Bot has no public HTTP API;
the "seams that let parts be built independently" are (1) the **shared config
document** and (2) the **Telegram command surface**. Both are introduced by the
Cancellation-Catcher subsystem — see
[ARCHITECTURE.md §8](ARCHITECTURE.md#8-cancellation-catcher--telegram-configurable-prefs-subsystem)
and [PRD-cancellation-catcher.md](PRD-cancellation-catcher.md).

Drafted 2026-07-24 (Architect). **Schema v2 (2026-07-26):** per-day rule list,
two independent live switches, and a separate holds ceiling — see §1.2a, §1.6,
§2.3. Status: **built** — this reflects the shipped `prefs.py` /
`telegram_commands.py`, not just a plan.

---

## 1. Shared config document

The single source of truth for "what to book". Written by the Telegram handler
(sole writer, inside the catcher process); read by **both** the catcher and the
drop sprinter. One small JSON file on the shared `tennisbot-config` volume
(catcher rw, sprinter ro), read-modify-write via the `bracket.json` idiom, with
atomic temp-file-rename on write.

### 1.1 Location
- Path: `$TENNISBOT_CONFIG_DIR/prefs.json` (env-injected, like `DROP_STATE_DIR`).
- Absent file ⇒ defaults (§1.4). Never a hard error — a fresh box boots usable.

### 1.2 Schema

```jsonc
{
  "version": 2,                     // schema version (was 1); v1 docs MIGRATE on read (§1.6)
  "centres": ["paddington"],        // 1+ of the configured target keys
  // CANONICAL (v2): an ORDERED rule list — index 0 = highest priority. Empty []
  // = any day / any time (the permissive default). See §1.2a.
  "rules": [
    { "days": ["Tue", "Thu"], "earliest": "18:00", "latest": null }
  ],
  // DERIVED flat mirror of the SOLE rule (§1.2a). Kept in lock-step by the store
  // for legacy/tolerant readers + single-window display. With 0 or ≥2 rules
  // these go []/null. Writers should set `rules`; readers may read either.
  "days": ["Tue", "Thu"],           // = rules[0].days     iff exactly one rule
  "earliest": "18:00",              // = rules[0].earliest iff exactly one rule
  "latest": null,                   // = rules[0].latest   iff exactly one rule
  "slot_length_hours": 1,           // 1 | 2 (2 = two consecutive hours, same court)
  "weekly_cap": 3,                  // max PAID court bookings per Mon-reset week
  "max_holds": 5,                   // max concurrent UNPAID holds (0 = pause)
  "catcher_live": false,            // arm the cancellation catcher (real holds)
  "drop_live": false,               // arm the midnight drop (real holds)
  "updated_at": "2026-07-26T09:00:00+01:00",  // ISO; set by handler on every write
  "updated_by": "telegram"          // provenance: "telegram" | "default"
}
```

> The old single `live` boolean is **gone** from the document — it is replaced
> by the two switches `catcher_live`/`drop_live`. `Prefs.live` survives in code
> only as a read-only convenience property (= "is either booker armed"), used by
> `/status` and legacy readers; it is never serialised.

### 1.2a Rules & the flat mirror (schema v2)

**Why a rule list at all.** v1 had one global window (`days`+`earliest`+`latest`)
— you couldn't say "weeknights after 18:00 **but** Saturdays 10:00–15:00". v2
makes the unit of intent a **`Rule`** and lets you stack several:

```jsonc
{ "days": ["Tue","Thu"], "earliest": "18:00", "latest": null }   // one Rule
```

- `days`: subset of `Mon`…`Sun`; `[]` = **any day**.
- `earliest`: `HH:MM` **inclusive** floor, or `null` = no floor.
- `latest`: `HH:MM` **exclusive** ceiling, or `null` = no ceiling.

A candidate `(date, time)` is **bookable iff some rule admits it** — the rule's
`days` include that weekday **and** its window admits the time. **Order is
priority:** `rules[0]` is highest, and the catcher books the highest-priority
matching slot first. The owner ranks intent simply by the order they add rules.
An empty `rules` list is **fully permissive** (any day, any time) — identical to
a default v1 document.

**The flat mirror.** The top-level `days`/`earliest`/`latest` are **derived**,
not independent state. The store (`Prefs.__post_init__`) keeps them in lock-step
with `rules`, in both directions:

| `rules` | flat `days`/`earliest`/`latest` |
|---|---|
| exactly **one** rule | mirror that rule |
| **zero** or **two-plus** rules | `[]` / `null` / `null` |
| constructed from flat fields only (v1-style) | a single rule is **synthesised** from them |

This is a deliberately cheap migration: every v1-era reader and the single-window
`/status` display keep working unchanged for the common one-window config, and
`to_dict` still emits the flat fields for a tolerant reader — while new code
reads the canonical `rules`.

### 1.3 Field rules & validation (handler rejects on violation, config unchanged)

| Field | Rule |
|---|---|
| `centres` | non-empty; every entry a key in `config/targets.yaml`. |
| `rules` | each rule's `days` ⊆ `Mon…Sun`; each bound `HH:MM` 24h or `null`; if both set, `earliest < latest`. A bad rule inside an otherwise-valid list is **dropped and degrades the doc** (§1.4a) rather than discarding the good rules. |
| `days`/`earliest`/`latest` | validated as the sole-rule mirror (same window rule: if both set, `earliest < latest`). |
| `slot_length_hours` | exactly `1` or `2`. |
| `weekly_cap` | integer `≥ 0` (0 = pause; PAID court bookings per Mon-reset week). |
| `max_holds` | integer `≥ 0` (0 = pause; concurrent UNPAID holds ceiling). |
| `catcher_live` / `drop_live` | each a bool; a `false→true` transition requires the confirm handshake (§2.3). |

An invalid update is **rejected with a helpful Telegram reply** and leaves the
previous config intact (acceptance criterion in the PRD).

### 1.4 Defaults (when `prefs.json` is absent or a field is missing)
`centres: ["paddington"]`, `rules: []` (any day / any time, and hence flat
`days: []`, `earliest/latest: null`), `slot_length_hours: 1`, `weekly_cap: 3`,
`max_holds: 5`, `catcher_live: false`, `drop_live: false`. Safe-by-default:
both bookers dry-run, one hour, paid cap 3, holds ceiling 5.

### 1.4a Degraded reads — REFUSE TO BOOK (decided 2026-07-24)

**Absent file ⇒ clean defaults. Present-but-unreadable ⇒ defaults marked
`degraded`, and BOTH `catcher_live` and `drop_live` are FORCED to `false`.**

Why the asymmetry: every *constraint* field's default is the **permissive**
value — no `days` filter means any day, no `earliest` means any time, and
`weekly_cap` returns to 3. So falling back to defaults is right for a **fresh
box** (nothing to misread) and wrong for a **configured box** (it silently
*widens* what the bot may do). Concretely: the owner sets `/cap 0` to pause
before a holiday while `live` is on; one field corrupts; the cap springs back
to 3 and a live bot resumes holding courts on an account they deliberately
paused.

- `Prefs.degraded` is a tuple of the field names that failed to parse
  (`"<document>"` / `"<unreadable file>"` when nothing could be read; a bad rule
  appears as `rules[i]`).
- **Non-empty `degraded` ⇒ `catcher_live` AND `drop_live` are `false`,
  regardless of the file.**
- `summary()` appends `⚠️ UNREADABLE: … — booking paused`, so `/status` and the
  daily heartbeat tell the owner booking is paused and why.
- A `version` newer than `SCHEMA_VERSION` counts as degraded: fields may have
  changed meaning, so an older reader must not guess.

Readers need no extra logic — both live flags are already `false`. `degraded` is
for reporting.

### 1.5 How each reader consumes it

- **Catcher** (every cycle): read fresh (pick up phone changes next cycle);
  Stage-1 push `centres` into the EA search; Stage-2 filter the week grid with
  the per-day rules (`matching_rule`), ordering candidates by **rule priority**;
  enforce the two ceilings — `weekly_cap` (paid) and `max_holds` (unpaid holds);
  book per `catcher_live`.
- **Sprinter** (each night, pre-arm): read fresh; if D+7's weekday matches no
  rule (`allows_date` is false) → **skip the drop tonight**; else pursue the
  floor from the **highest-priority rule matching D+7's weekday**
  (`earliest_for_date`) plus `slot_length_hours`, and book per `drop_live`.
  ⚠️ The drop enforces a rule's `earliest` (floor) but **NOT** its `latest`
  (ceiling) — the single-date engine has no upper-time param (see §2.1 warning
  and BACKLOG). (Day-filtering the drop is the documented consequence of shared
  prefs — ARCHITECTURE §8.6.)

> **Contract note:** neither reader writes this file — only the Telegram handler
> does. Readers must tolerate a concurrent write (atomic rename means they see
> either the old or the new whole file, never a torn one). The two `*_live`
> flags are read **independently**: arming only the catcher never makes the drop
> book, and vice versa.

### 1.6 Migration — a v1 document is UPGRADED on read, never rejected

A v1 `prefs.json` (flat window, `version` 1 or absent) is read into the v2 shape
so an in-place upgrade needs no rewrite step:

| v1 input | v2 result |
|---|---|
| flat `days`/`earliest`/`latest` set, no `rules` key | one `Rule` synthesised from the flat window |
| all-default flat window, no `rules` key | `rules: []` (fully permissive) — identical behaviour |
| `live: true` (no `catcher_live`/`drop_live` keys) | `drop_live: true`, **`catcher_live` stays `false`** |
| `live: false` | both flags `false` |
| missing `version` | treated as current (absent fields default; doc stays usable) |
| `version` > `SCHEMA_VERSION` | **degraded** → both live flags forced `false` (§1.4a) |

**Why `live:true` → drop-only (conservative).** The drop was the *only* booker
under v1, so migrating its consent forward is faithful. The catcher is a **new**
live booker — silently arming it would start a second booker the owner never
consented to, so it stays off until an explicit `/catcher on`. The safe
migration direction is to under-arm, not over-arm.

---

## 2. Telegram command surface

Single-user. **Only `TELEGRAM_CHAT_ID` is honoured**; any other sender is
ignored silently (no reply — don't confirm the bot exists to strangers).
Transport: **long-poll `getUpdates`** (outbound only — preserves "no open
ports"), run inside the catcher process.

### 2.1 Read commands

| Command | Effect |
|---|---|
| `/status` | Reply with the active config (§1.2), **mode first** (LIVE/DRY-RUN), then a per-booker line (`catcher LIVE/DRY · drop LIVE/DRY`), the rules (a numbered priority list when ≥2, else the classic Days/Window lines), the paid cap (this week's count vs cap when known), the holds ceiling, and next scan time. **Warns** if `drop_live` is set while any rule carries a `latest` — the drop enforces the floor only, not the ceiling (§1.5, BACKLOG). |
| `/rules` | List the booking rules in priority order (read-only; `/rules clear` is a setter, see §2.2). |
| `/help` | List commands. |

> ⚠️ **`/status` needs two facts that are NOT in the config document**: this
> week's paid-booking count (derived from EA Manage Bookings — ARCHITECTURE
> §8.6) and the catcher's next scan time. The handler therefore takes them as
> **injected optionals** and renders `unknown` when absent. **Whoever builds the
> catcher loop must pass them in**, or `/status` will permanently under-report.
> (Gap found during implementation, 2026-07-24 — the handler was built against
> this contract before the catcher existed, which is exactly what the injection
> seam is for.)

### 2.2 Config commands (each validates per §1.3, then persists)

| Command | Example | Sets |
|---|---|---|
| `/centres` | `/centres paddington westway` | `centres` |
| `/rule` | `/rule Tue Thu 18:00-` · `/rule Sat 10:00-15:00` · `/rule Sun any` | **appends** a rule (lowest priority) to `rules` |
| `/rule del` | `/rule del 2` | removes rule #N (numbering per `/rules`) |
| `/rules clear` | `/rules clear` | empties `rules` → any day / any time |
| `/days` | `/days Tue Thu` · `/days any` | the **sole rule's** days (shorthand — see below) |
| `/window` | `/window 18:00-22:00` · `/window 18:00-` · `/window any` | the **sole rule's** window |
| `/length` | `/length 2` | `slot_length_hours` |
| `/cap` | `/cap 3` · `/cap 0` (pause booking) | `weekly_cap` (paid/week) |
| `/holds` | `/holds 5` · `/holds 0` (pause booking) | `max_holds` (concurrent unpaid holds) |

**Window grammar** (used by `/window` and the trailing token of `/rule`):
`18:00-22:00` (both bounds) · `18:00-` (floor only) · `-22:00` (ceiling only) ·
`any` (unbounded). `earliest` is inclusive, `latest` exclusive.

**`/days` and `/window` are a single-rule shorthand.** They create or edit the
**sole** rule and preserve v1 behaviour exactly when there are 0 or 1 rules. With
**≥2 rules they REJECT** (a single flat window is meaningless against a list) and
point the owner at `/rule` / `/rule del` / `/rules clear`. Setting the sole
rule's window and days back to permissive collapses `rules` to `[]`.

Each command replies with the new value **and** a one-line summary of the full
active config, so the phone always shows the whole picture after a change.
Rejected updates (§1.3) reply with the error and leave the config unchanged.

### 2.3 Live toggles (two independent switches, guarded)

v2 has **two** arm-able bookers, so there are two switches plus a `/live` panic
convenience that acts on both.

| Command | Flow |
|---|---|
| `/catcher off` | Immediate → `catcher_live=false`, reply "catcher DRY-RUN". |
| `/catcher on` | Two-step: bot replies *"⚠️ Enable REAL catcher bookings? Reply `CONFIRM` within 2 min."*; only a following `CONFIRM` sets `catcher_live=true` (already-live is a no-op reply). |
| `/drop off` | Immediate → `drop_live=false`, reply "drop DRY-RUN". |
| `/drop on` | Same handshake, targeting `drop_live`. |
| `/live off` | **Panic path** — immediate, no handshake: sets BOTH flags `false`. The safe direction never has a speed bump. |
| `/live on` | Arms BOTH via one handshake (targeted "both"); `CONFIRM` sets both flags `true`. |

**The handshake carries a target.** A pending confirmation records *which* switch
it will arm (`"catcher"` | `"drop"` | `"both"`). The confirm state is
`(pending_confirm_until, pending_confirm_target)`:

- `CONFIRM` **within 2 min** and with a non-null target → arms exactly that
  switch (or both).
- A `CONFIRM` with **no target** arms **nothing** (fail-closed) and re-prompts —
  it must never default to "both".
- `CONFIRM` **after** the deadline, or **any other message** while a confirm is
  pending → cancels the arming (still dry-run) and, if it was itself a command,
  processes it normally.
- The deadline is computed in **UTC** then rendered to London, so the autumn
  fall-back hour can't stretch the 2-minute window.

Rationale: the confirm speed-bump sits **only** on the one consequential
transition (dry-run→live), per switch; `off` (the safe direction) and every
config change stay instant. `/catcher`/`/drop` give per-booker control; `/live`
keeps the owner's muscle-memory one-shot arm/disarm. See ARCHITECTURE §8.8.

### 2.4 Outbound messages (bot → owner)

| Trigger | Content |
|---|---|
| Booking (catcher or sprinter) | slot(s), centre, date; **screenshot**; "pay in the app" (live) or "would book" (dry-run). |
| Error | which job, one-line reason (reuses the `never_opened`/`sold_out`/`prefs_too_narrow` diagnosis vocabulary). |
| Cap reached | once per week when the PAID weekly cap blocks a booking. |
| Hold ceiling reached | once per day when `max_holds` blocks a booking (unpaid holds ceiling hit). |
| Drop-time regression | the §8.3 verdict (moved / broken) with the D+7 evidence. |
| Daily heartbeat (~09:00) | "alive", **mode**, config summary, week's paid-count, open holds. |
| Empty poll cycle | *nothing* (silence by design). |

---

## 2.5 Obligations on the transport (long-poll) — READ BEFORE BUILDING IT

The handler is pure; these are the shell's responsibilities, and each one comes
from a real defect found in review (2026-07-24). They are **not** optional.

1. **Advance the `getUpdates` offset even when the handler raises.** Telegram
   redelivers un-acknowledged updates forever, so an exception that escapes the
   handler becomes an infinite redelivery loop — and with
   `restart: unless-stopped` that is a **poison-message crash loop** that takes
   the catcher's booking cycles down with it. Wrap `handle()` in a broad
   `except`, log, reply "something went wrong", and advance regardless. (PRD §7:
   "a crash in one cycle does not kill the loop".)
2. **Catch write failures.** `CommandSession.handle` calls `save_prefs`, which
   raises on a read-only or full volume — a plausible compose mis-mount, since
   the sprinter mounts this volume `ro` by design. Do not let it kill the loop.
3. **Pass zero-padded `HH:MM` to `allows_time()`.** It normalises `"9:00"` and
   fails closed on anything unparseable, but the *week-grid parser* (a new page,
   ARCHITECTURE §8.2) should emit padded times rather than rely on that.
4. **Never send a reply built by string-concatenating user input.** Replies go
   out with `parse_mode: HTML`; the handler already escapes at the render
   boundary, so pass its `reply` through unmodified.
5. **`TELEGRAM_CHAT_ID` must be set.** The handler fails closed when it is
   missing or blank (it authorises nobody), so a misconfigured deployment is
   inert rather than open — but that also means *no command will ever work*.
   Check it at startup and log loudly.

## 3. Not in this contract (deliberately)

- **Imperative commands** ("book court 3 now") — config-only surface for MVP
  (PRD §5). If added later, they need their own request/confirm spec here.
- **Per-court preferences** — the week grid is per (date, time), not per court;
  out of MVP.
- **Interval / drop-time / blackout tuning via Telegram** — fixed in code; the
  drop time is a discovered fact, not a preference.
