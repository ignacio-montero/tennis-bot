# Tennis-Bot — Status & next steps

_Last updated: 2026-07-12. For how to run it and the operational gotchas see
[../CLAUDE.md](../CLAUDE.md); for future ideas see [BACKLOG.md](BACKLOG.md)._

## What works today ✅

- **`run-now` booking** for two centres (Paddington `0156`, Westway `0162`):
  courts (surface preference, optional two-consecutive-hours) and activities
  (Paddington "Tennis (adv)" Wed 1900 / Sun 1300). Dry-run by default.
- **Verified LIVE (real holds):** single Paddington court, and activity
  booking (Ref 1561842712, Sun 1300).
- **Automated activity booking:** 4 launchd jobs (Wed 19:00/20:30, Sun
  13:00/14:30) book the classes 7 days ahead, live, with idempotent re-hold
  backups. This is in production now — ⚠️ the Mac must be awake at those times.
- **Drop machinery built:** `tennisbot drop` (server-skew + spin-wait, sub-ms
  accuracy, overload-resilient camp/retry) and `tennisbot watch` (read-only
  drop-time finder). Dry-run verified.
- Session persistence, Telegram notifications, secret-free repo published
  (private GitHub `ignacio-montero/tennis-bot`).

## In progress / blocked ⏳

- **Pin down the court drop time — hunter is now LIVE on the homelab.**
  `tennisbot watchd` (deployed 2026-07-12, see CLAUDE.md) polls today+7/+8
  around the clock and will Telegram the exact closed→open bracket, then
  auto-tighten it night after night. **New evidence (2026-07-12 10:37):**
  today+7 (Sat 19 Jul) was already open with 17 slots in the *morning*, while
  today+8 (Sun 20 Jul) was `row_full`. That falsifies "~21:50 releasing D−7".
  Remaining candidates: **midnight** (D opens at 00:00 of D−7) or **evening
  ~21:50 releasing D−8** (which would reconcile the 2026-07-05 21:49–22:02
  error-block). Either way 20 Jul flips tonight — expect the answer within
  1–2 nights, sub-minute within ~3 (coarse 20 min → auto hot-window 20 s).
  Old one-evening `watch` stays paused; the daemon has blackouts around the
  Mac activity jobs (Wed 19:00/20:30, Sun 13:00/14:30 ± margin).
- **Court-drop launchd job is DISABLED**
  (`deploy/launchd/com.tennisbot.court-drop.plist.DISABLED`). To enable after
  confirming the time: set `targets.yaml drop.local_time`, set the plist
  launch time ~4 min earlier, rename to drop `.DISABLED`, `launchctl load -w`.
- **One live confirmation needed for the 2-hour second-hour hold path**
  (coded, dry-run tested only).

## Recommended next steps

1. Wait for watchd's Telegram bracket alerts (tonight/tomorrow), confirm the
   drop time, then enable the court-drop job. Read the raw evidence with:
   `ssh homelab 'docker exec tennisbot-watchd cat /data/watchd/observations.jsonl' | tail`.
   Once confirmed, consider moving `drop` itself to the homelab (image +
   trigger already portable) and retiring/tightening watchd's hot window.
2. Verify a 2-hour live booking once.
3. **<redacted>** (Telegram token already rotated) — it was shared
   <redacted>.
4. When ready, move hosting to the old Windows laptop (Docker + Tailscale —
   plan in [BACKLOG.md](BACKLOG.md) §7); trigger layer is already portable.

## Known risks / caveats

- At T-0 the browser flow takes ~10–15s (search→parse→click). Fine unless the
  drop is fiercely contested; fast-path optimisations are parked in BACKLOG §1.
- Gladstone/EA WebForms is the brittle end of the integration — expect
  selector/flow maintenance (see CLAUDE.md gotchas).
