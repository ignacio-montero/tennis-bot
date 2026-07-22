# tennisbot-drop — homelab deployment runbook (self-scheduling booker)

The nightly court-drop **booker**, as a **self-scheduling sidecar**
(`python -m tennisbot drop-loop`): one long-running container that each night
sleeps until ~00:00 minus a pre-warm lead, runs one `drop` (pre-warm → spin-wait
to the instant → camp), and loops. **No host cron, no Docker socket** — it
deploys with the same `docker compose up -d` as watchd, needing no extra
privilege. Creates unpaid holds when `--live`; pay in the Everyone Active app.

- **Image:** `ghcr.io/ignacio-montero/tennisbot-watchd:<semver>` — the *same*
  image as watchd (entrypoint `python -m tennisbot`; this service overrides the
  command). Pinned semver, never `:latest`. Built by GitHub Actions on a `v*`
  tag (`.github/workflows/build-image.yml`) — the box and the Mac never build it.
- **Compose:** `~/homelab/services/tennisbot-drop/docker-compose.yml` (in the
  homelab root `compose.yaml` `include:`).
- **Drop time:** `config/targets.yaml` → `paddington.drop.local_time: "00:00"`
  (confirmed midnight-D7). TZ-correct via `Europe/London` in the image + compose,
  so **no DST maintenance** — unlike a UTC crontab, it never needs a clocks-change
  edit. The `_next_drop` helper anchors the booking date to the drop instant's
  civil date, so waking before midnight still books the right day.
- **One-session rule:** watchd (`0.3.0`+) has a nightly 23:53–00:07 blackout and
  yields the EA session to this booker itself — no external stop/start needed.

## Publish the image (from anywhere — triggers CI, no local Docker)

```bash
cd <tennis-bot checkout>
git tag v0.3.0 && git push origin v0.3.0
# GitHub Actions runs the tests, then builds linux/amd64 and pushes
#   ghcr.io/ignacio-montero/tennisbot-watchd:0.3.0
```

## Deploy (homelab repo loop — the one admin step)

1. Create the untracked secrets file on the server (same four vars as watchd):
   ```bash
   ssh homelab 'mkdir -p ~/homelab/services/tennisbot-drop'
   # create ~/homelab/services/tennisbot-drop/.env  (EA_* + TELEGRAM_*, see .env.example)
   ```
2. Pull + bring both up (rolling watchd to 0.3.0 gives it the blackout):
   ```bash
   ssh homelab 'cd ~/homelab && git pull \
     && docker compose pull tennisbot-drop tennisbot-watchd \
     && docker compose up -d tennisbot-drop tennisbot-watchd'
   ```

### Verify
```bash
ssh homelab 'docker ps --filter name=tennisbot-drop'         # Up (a healthy long-runner)
ssh homelab 'docker logs --tail 20 tennisbot-drop'           # a drop_loop.sleep line naming the next drop date
ssh homelab 'docker port tennisbot-drop'                     # must print NOTHING
```
At midnight expect `drop_loop.fire` → `drop.armed` → `wait.fired` → a Telegram
hold (or "no slot"), then it sleeps to the next night.

## Dry-run first (recommended)

The committed compose command is **dry-run** (no `--live`) and books "any court
≥19:00" (`--after 19:00`), matching the earlier rehearsal. Watch a night or two,
then add `--live` to the command and re-`up -d` to book for real. Switch
`--after 19:00` to your ranked `want` prefs (drop the flag) when you want the bot
to chase specific slots.

## Upgrade / rollback / stop

- **Upgrade:** push a new `v*` tag (CI builds it) → bump the tag in BOTH
  `tennisbot-drop` and `tennisbot-watchd` compose files → `git pull &&
  docker compose pull && docker compose up -d` those two.
- **Rollback:** repoint the tag(s) to the prior version, redeploy. Session
  volume survives; no migrations.
- **Emergency stop (reversible):** `ssh homelab 'docker compose stop tennisbot-drop'`.
