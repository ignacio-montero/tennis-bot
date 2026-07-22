# Tennis-Bot — Status & next steps

_Last updated: 2026-07-16. For how to run it and the operational gotchas see
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

- **Court drop time — CONFIRMED: 00:00:00 London on D−7.** After the 0.1.3
  wraparound fix, two clean nights nailed it, both ~20 s brackets on the stroke
  of midnight exactly 7 days ahead:
  - **Mon 21 Jul**: 14 Jul 00:00:07 → 00:00:27 (63 slots)
  - **Thu 23 Jul**: 16 Jul 00:00:09 → 00:00:29 (62 slots)

  This clears the last blocker on enabling the `drop` job. **One outlier to
  keep watching:** **Tue 22 Jul** was seen opening ~04:16 on 14 Jul (20-min
  coarse-poll bracket) — off-clock and ~a day early vs strict D−7, most likely
  a manual re-release or cancellation batch, not the scheduled drop. Doesn't
  unseat the two clean confirmations but is why watchd stays running as a
  sentinel past the first live drop.
  - ⚠️ **The one caution ping you saw is benign:** on the 0.1.3 restart
    (13 Jul 22:36) watchd found Mon 20 Jul *already open* with no prior closed
    baseline, so it emitted `open_at_first_read` (⚠️) instead of a 🎯 DROP.
    Documented restart noise, not a failure. The 🎯 DROP pings are the real
    signal.
  - Old one-evening `watch` stays paused; the daemon has blackouts around the
    Mac activity jobs (Wed 19:00/20:30, Sun 13:00/14:30 ± margin).
- **Court-drop launchd job is DISABLED**
  (`deploy/launchd/com.tennisbot.court-drop.plist.DISABLED`). To enable after
  confirming the time: set `targets.yaml drop.local_time`, set the plist
  launch time ~4 min earlier, rename to drop `.DISABLED`, `launchctl load -w`.
- **One live confirmation needed for the 2-hour second-hour hold path**
  (coded, dry-run tested only).

## Recommended next steps

1. **Drop rehearsal DEPLOYED on the homelab (2026-07-16), dry-run.** Service
   `tennisbot-drop` (image `0.2.1`, `profiles:[drop]`) is cron-fired at **00:41
   London** (`41 23 * * *` UTC) by `run-drop.sh`, which stops watchd for the run
   and restarts it after (one-session rule), then books *any court ≥19:00 on
   D+7* — **dry-run, no hold**. Verified end-to-end on the box (login→skew→
   spin-wait fired at the instant→grid parse→outcome→watchd restarted).
   Runbook/compose live in the homelab repo (`services/tennisbot-drop/`).
   **✅ BLOCKER RESOLVED (2026-07-22 clean run).** The 00:41 dry-run for D+7 =
   29 Jul fired at 00:45:00 (lateness 0 ms) and the 0.2.1 full-grid logging
   settled the "no ≥19:00 slot" question: evening slots **do exist and are
   parsed correctly** — Synth showed 08:00–17:00 only, but **Tarmac had 18:00
   and 20:00 still available** at 00:45, and `--after 19:00` correctly picked
   `20:00 Tennis Court 5` (`drop.secured`, dry-run). So the earlier "no evening
   availability" was surface-specific + the pre-0.2.1 parser blind spot, not a
   real absence. The machinery is sound; graduation is now a go/no-go decision,
   not a blocker.
   **Graduation path (once unblocked):**
   a. Watch 1–2 nights of the 00:41 dry-run via Telegram `🎾 DRY-RUN` or
      `ssh homelab 'tail ~/homelab/services/tennisbot-drop/logs/cron.log'`.
      Expect a "would book 19:00+" (the 2026-07-22 run already produced one).
      ⚠️ **Known gap:** `drop-outcomes.jsonl` is NOT actually being written —
      the `tennisbot-drop-state` volume is empty despite `drop.result` logging.
      The cron.log is the source of truth for now; the jsonl persistence needs
      a fix (minor, non-blocking; likely `DROP_STATE_DIR` write path).
   b. Move to the **real 00:00** instant: swap the crontab line to the
      `DROP_TIME=00:00` / `56 22 * * *` UTC variant (in `crontab.example`),
      still dry-run, for a couple more nights.
   c. Go **live**: add `DROP_LIVE=1` to the crontab line — creates real holds.
   Keep watchd running as the sentinel throughout; **stop (don't delete)** it
   once the first live drop succeeds — re-arm with one `compose up -d` if `drop`
   later fails at 00:00 (policy drift, e.g. the 22 Jul-style off-clock release).
2. Verify a 2-hour live booking once.
3. **Production target = the homelab for EVERYTHING** (decided 2026-07-13,
   supersedes the old Windows-laptop idea in BACKLOG §7): court-drop job
   first, then the four activity jobs (removing the "Mac must be awake"
   caveat entirely). The trigger layer is portable by design — same image,
   new triggers (cron/compose on the box), booking code unchanged.

## Known risks / caveats

- At T-0 the browser flow takes ~10–15s (search→parse→click). Fine unless the
  drop is fiercely contested; fast-path optimisations are parked in BACKLOG §1.
- Gladstone/EA WebForms is the brittle end of the integration — expect
  selector/flow maintenance (see CLAUDE.md gotchas).
