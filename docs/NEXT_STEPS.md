# Tennis-Bot — Status & next steps

_Last updated: 2026-07-13. For how to run it and the operational gotchas see
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

- **Pin down the court drop time — MIDNIGHT-D7 all but confirmed, one tight
  night to go.** watchd's bracket for **Mon 20 Jul**:
  closed at **Sun 12 Jul 23:59:40** → open at **Mon 13 Jul 00:19:40** (47
  slots). That kills the "evening releasing D−8" theory and is consistent with
  **D opens at 00:00 of D−7**. The bracket was 20 min wide because the
  auto-tightened window (23:54–00:24) crosses midnight and `in_hot_window`
  couldn't match wraparound windows — **fixed and redeployed 2026-07-13 ~22:15
  as image 0.1.3** (wraparound windows + static 23:40–00:30 hot window + skip
  dates already seen open). Tonight (13→14 Jul, Tue 21 Jul flips) the poll
  cycle is ~35 s inside the window → expect a ≤1-min bracket around 00:00 via
  Telegram. ⚠️ Evening reads of *open* dates flap `row_full` under load
  (~21:50–22:15) — a stray "already OPEN"/"DROP" ping about the 20th this
  evening is restart noise, not a drop.
  Old one-evening `watch` stays paused; the daemon has blackouts around the
  Mac activity jobs (Wed 19:00/20:30, Sun 13:00/14:30 ± margin).
- **Court-drop launchd job is DISABLED**
  (`deploy/launchd/com.tennisbot.court-drop.plist.DISABLED`). To enable after
  confirming the time: set `targets.yaml drop.local_time`, set the plist
  launch time ~4 min earlier, rename to drop `.DISABLED`, `launchctl load -w`.
- **One live confirmation needed for the 2-hour second-hour hold path**
  (coded, dry-run tested only).

## Recommended next steps

1. Check tonight's (13→14 Jul) Telegram bracket for Tue 21 Jul — expected
   ≤1 min around 00:00. Raw evidence:
   `ssh homelab 'docker exec tennisbot-watchd cat /data/watchd/observations.jsonl' | tail`.
   If confirmed: set `targets.yaml drop.local_time: "00:00"` (fire just past
   midnight; `days_before: 7` — at 00:00 of day X, X+7 opens, so to book Sat
   25 Jul the job fires in the night Fri 17→Sat 18). **Run `drop` on the
   homelab, not the Mac** (a sleeping Mac misses a midnight launchd job).
   Keep watchd running through the week to confirm the Fri→Sat drop matches,
   then **stop (don't delete) watchd** once the first live drop succeeds —
   it stays on the shelf as a sentinel, re-armed with one `compose up -d` if
   `drop` ever starts failing at 00:00 (policy drift). watchd needs a
   blackout/stop around the drop instant — one session per account.
2. Verify a 2-hour live booking once.
3. **<redacted>** (Telegram token already rotated) — it was shared
   <redacted>.
4. **Production target = the homelab for EVERYTHING** (decided 2026-07-13,
   supersedes the old Windows-laptop idea in BACKLOG §7): court-drop job
   first, then the four activity jobs (removing the "Mac must be awake"
   caveat entirely). The trigger layer is portable by design — same image,
   new triggers (cron/compose on the box), booking code unchanged.

## Known risks / caveats

- At T-0 the browser flow takes ~10–15s (search→parse→click). Fine unless the
  drop is fiercely contested; fast-path optimisations are parked in BACKLOG §1.
- Gladstone/EA WebForms is the brittle end of the integration — expect
  selector/flow maintenance (see CLAUDE.md gotchas).
