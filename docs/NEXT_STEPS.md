# Tennis-Bot — Status & next steps

_Last updated: 2026-07-06. For how to run it and the operational gotchas see
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

- **Pin down the court drop time.** Best current theory: **evening ~21:50**,
  sells out in minutes — inferred from the 2026-07-05 thundering-herd
  error-block (21:49–22:02) and `row_full` readings. Not 100% confirmed;
  `row_full` is ambiguous ("not released yet" vs "sold out"). The nightly
  watcher is **paused** after it collided with the Wed activity job — when
  resuming, run gently, short windows, never overlapping the activity job
  times (Wed 19:00/20:30, Sun 13:00/14:30). Easiest confirmation: catch the
  error-block **onset** time, or a single morning read of today+7.
- **Court-drop launchd job is DISABLED**
  (`deploy/launchd/com.tennisbot.court-drop.plist.DISABLED`). To enable after
  confirming the time: set `targets.yaml drop.local_time`, set the plist
  launch time ~4 min earlier, rename to drop `.DISABLED`, `launchctl load -w`.
- **One live confirmation needed for the 2-hour second-hour hold path**
  (coded, dry-run tested only).

## Recommended next steps

1. Confirm the drop time (see above), then enable the court-drop job.
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
