# Tennis-Bot — API / Contract Spec

The **internal contracts** between components. Tennis-Bot has no public HTTP API;
the "seams that let parts be built independently" are (1) the **shared config
document** and (2) the **Telegram command surface**. Both are introduced by the
Cancellation-Catcher subsystem — see
[ARCHITECTURE.md §8](ARCHITECTURE.md#8-cancellation-catcher--telegram-configurable-prefs-subsystem)
and [PRD-cancellation-catcher.md](PRD-cancellation-catcher.md).

Drafted 2026-07-24 (Architect). Status: **contract for build** — settle here
before the catcher and the Telegram handler are built against it.

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
  "version": 1,                     // schema version; bump on breaking change
  "centres": ["paddington"],        // 1+ of the configured target keys
  "days": ["Tue", "Thu"],           // weekdays to pursue; [] or all 7 = any day
  "earliest": "18:00",              // HH:MM inclusive time floor (fine match); null = no floor
  "latest": null,                   // HH:MM exclusive ceiling (fine match); null = no ceiling
  "slot_length_hours": 1,           // 1 | 2 (2 = two consecutive hours, same court)
  "weekly_cap": 3,                  // max PAID bookings per Mon-reset week
  "live": false,                    // false = dry-run (default); true = create real holds
  "updated_at": "2026-07-24T09:00:00+01:00",  // ISO; set by handler on every write
  "updated_by": "telegram"          // provenance: "telegram" | "default" | "migration"
}
```

### 1.3 Field rules & validation (handler rejects on violation, config unchanged)

| Field | Rule |
|---|---|
| `centres` | non-empty; every entry a key in `config/targets.yaml`. |
| `days` | subset of `Mon…Sun`; empty ⇒ treated as all 7. |
| `earliest`/`latest` | `HH:MM` 24h or `null`; if both set, `earliest < latest`. |
| `slot_length_hours` | exactly `1` or `2`. |
| `weekly_cap` | integer `≥ 0` (0 = pause all booking without going non-live). |
| `live` | bool; transitions `false→true` require the confirm handshake (§2.3). |

An invalid update is **rejected with a helpful Telegram reply** and leaves the
previous config intact (acceptance criterion in the PRD).

### 1.4 Defaults (when `prefs.json` is absent or a field is missing)
`centres: ["paddington"]`, `days: []` (any), `earliest: null`, `latest: null`,
`slot_length_hours: 1`, `weekly_cap: 3`, `live: false`. Safe-by-default:
dry-run, one hour, cap 3.

### 1.4a Degraded reads — REFUSE TO BOOK (decided 2026-07-24)

**Absent file ⇒ clean defaults. Present-but-unreadable ⇒ defaults marked
`degraded`, and `live` is FORCED to `false`.**

Why the asymmetry: every *constraint* field's default is the **permissive**
value — no `days` filter means any day, no `earliest` means any time, and
`weekly_cap` returns to 3. So falling back to defaults is right for a **fresh
box** (nothing to misread) and wrong for a **configured box** (it silently
*widens* what the bot may do). Concretely: the owner sets `/cap 0` to pause
before a holiday while `live` is on; one field corrupts; the cap springs back
to 3 and a live bot resumes holding courts on an account they deliberately
paused.

- `Prefs.degraded` is a tuple of the field names that failed to parse
  (`"<document>"` / `"<unreadable file>"` when nothing could be read).
- **Non-empty `degraded` ⇒ `live` is `false`, regardless of the file.**
- `summary()` appends `⚠️ UNREADABLE: … — booking paused`, so `/status` and the
  daily heartbeat tell the owner booking is paused and why.
- A `version` newer than `SCHEMA_VERSION` counts as degraded: fields may have
  changed meaning, so a v1 reader must not guess.

Readers need no extra logic — `live` is already `false`. `degraded` is for
reporting.

### 1.5 How each reader consumes it

- **Catcher** (every cycle): read fresh (pick up phone changes next cycle);
  Stage-1 push `centres`+`days`+time-bucket(`earliest`/`latest`) into the EA
  search; Stage-2 filter the week grid by exact `earliest`/`latest` +
  `slot_length_hours`; enforce `weekly_cap`; book per `live`.
- **Sprinter** (each night, pre-arm): read fresh; if D+7's weekday ∉ `days`
  (and `days` non-empty) → **skip the drop tonight**; else pursue `earliest`/
  `latest`/`slot_length_hours` on D+7 per `live`. (Day-filtering the drop is the
  documented consequence of shared prefs — ARCHITECTURE §8.6.)

> **Contract note:** neither reader writes this file — only the Telegram handler
> does. Readers must tolerate a concurrent write (atomic rename means they see
> either the old or the new whole file, never a torn one).

---

## 2. Telegram command surface

Single-user. **Only `TELEGRAM_CHAT_ID` is honoured**; any other sender is
ignored silently (no reply — don't confirm the bot exists to strangers).
Transport: **long-poll `getUpdates`** (outbound only — preserves "no open
ports"), run inside the catcher process.

### 2.1 Read commands

| Command | Effect |
|---|---|
| `/status` | Reply with the active config (§1.2), **mode first** (LIVE/DRY-RUN), this week's paid-count vs cap, and next scan time. |
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
| `/days` | `/days Tue Thu` · `/days any` | `days` |
| `/window` | `/window 18:00-22:00` · `/window 18:00-` · `/window any` | `earliest`,`latest` |
| `/length` | `/length 2` | `slot_length_hours` |
| `/cap` | `/cap 3` · `/cap 0` (pause booking) | `weekly_cap` |

Each replies with the new value **and** a one-line summary of the full active
config, so the phone always shows the whole picture after a change.

### 2.3 Live toggle (guarded)

| Command | Flow |
|---|---|
| `/live off` | Immediate → `live=false`, reply "DRY-RUN". |
| `/live on` | Two-step: bot replies *"⚠️ Enable REAL bookings? Reply `CONFIRM` within 2 min."*; only a following `CONFIRM` sets `live=true`. Any other message cancels. |

Rationale: the confirm speed-bump sits **only** on the one consequential
transition (dry-run→live); everything else is instant. See ARCHITECTURE §8.8.

### 2.4 Outbound messages (bot → owner)

| Trigger | Content |
|---|---|
| Booking (catcher or sprinter) | slot(s), centre, date; **screenshot**; "pay in the app" (live) or "would book" (dry-run). |
| Error | which job, one-line reason (reuses the `never_opened`/`sold_out`/`prefs_too_narrow` diagnosis vocabulary). |
| Cap reached | once per week when the cap blocks a booking. |
| Drop-time regression | the §8.3 verdict (moved / broken) with the D+7 evidence. |
| Daily heartbeat (~09:00) | "alive", **mode**, config summary, week's paid-count. |
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
