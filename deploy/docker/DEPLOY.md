# tennisbot-watchd — homelab deployment runbook

A 24/7 **read-only** Playwright daemon (`python -m tennisbot watchd`) that polls
Everyone Active to pin down the court drop time and notifies Telegram. It
creates **no holds and no bookings**. Outbound-only: **no ports published**.

- **Image:** `ghcr.io/ignacio-montero/tennisbot-watchd:<semver>` — **private**
  GHCR package (source repo is private). Always a pinned semver tag, never
  `:latest`. The homelab **pulls**; it never builds (building Chromium images on
  the N95 is slow and the box is RAM-constrained).
- **Base:** `mcr.microsoft.com/playwright/python:v1.60.0-noble`. The tag MUST
  match `playwright==1.60.0` in `requirements.txt` — bump them together.
- **Compose service:** `~/Development/homelab/services/tennisbot-watchd/docker-compose.yml`
  (registered in the homelab root `compose.yaml` `include:`).

## Resource & data profile

| Item | Value |
|---|---|
| RAM | `mem_limit: 1536m` (= `memswap_limit`). Headless Chromium page ~300–600 MB with spikes; Python + structlog is small. Total homelab limits become ~2.75 GB of the 8 GB ceiling. |
| CPU | Bursty during a poll (page load), idle between polls (coarse 20 min / fine 20 s cadence). Fine on the N95. |
| Ports | **None.** Do not publish anything. |
| Volumes | `tennisbot-watchd-state` → `/data/watchd` (observations.jsonl + bracket.json; a few MB/yr). `tennisbot-watchd-session` → `/app/.session` (EA login storage_state, ~KBs — keep it: losing it forces re-logins and risks account throttling). |
| Logs | Logs every poll → json-file rotation capped at 3×10 MB in the compose. |
| Secrets | `EA_EMAIL`, `EA_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` via untracked `.env` on the server (see `.env.example`). |
| Timezone | `TZ=Europe/London` (set in image and compose). |

⚠️ **One session at a time:** never run watchd on the homelab while the Mac is
also driving the same EA account (the launchd activity jobs Wed 19:00/20:30 and
Sun 13:00/14:30 UK time still run on the Mac). Concurrent sessions can trip
throttling — watchd tolerates this, but be aware when reading logs.

## Build & publish (on the Mac — repo root)

The Mac is arm64, the homelab is amd64 → always build `--platform linux/amd64`.

```bash
cd ~/Development/Tennis-Bot

# 1. Build (local verify)
docker buildx build --platform linux/amd64 \
  -f deploy/docker/Dockerfile \
  -t ghcr.io/ignacio-montero/tennisbot-watchd:0.1.0 --load .

# 2. Smoke test (arg parsing + imports; do NOT run the daemon for real locally)
docker run --rm ghcr.io/ignacio-montero/tennisbot-watchd:0.1.0 watchd --help

# 3. Push (needs a GHCR login with write:packages on this Mac)
docker push ghcr.io/ignacio-montero/tennisbot-watchd:0.1.0
```

The package will be **private** (inherits from the private repo). The homelab is
already `docker login`'d to ghcr.io with a read-scoped token (set up for
plaque-hunter), so no extra pull-auth step is needed on the server — just
confirm the token's scope covers this new package (fine if it's a classic
`read:packages` PAT; if it's fine-grained, add the package).

## First deploy (homelab repo loop)

1. Create the untracked secrets file **on the server**:
   ```bash
   ssh homelab 'mkdir -p ~/homelab/services/tennisbot-watchd'
   # then create ~/homelab/services/tennisbot-watchd/.env with the four vars
   # (see .env.example). Do NOT commit it.
   ```
2. Commit + push the homelab repo (new service dir + `compose.yaml` include line).
3. Deploy just this service:
   ```bash
   ssh homelab 'cd ~/homelab && git pull && docker compose pull tennisbot-watchd && docker compose up -d tennisbot-watchd'
   ```

No migrations, no seeding — first boot logs in to EA (creating
`/app/.session/ea_state.json` on the session volume) and starts polling.

### Verify it's up
```bash
ssh homelab 'docker ps --filter name=tennisbot-watchd'        # Up (healthy)
ssh homelab 'docker logs --tail 50 tennisbot-watchd'          # structlog poll lines, no tracebacks
ssh homelab 'docker port tennisbot-watchd'                    # must print NOTHING (no ports)
ssh homelab 'docker exec tennisbot-watchd ls /data/watchd'    # observations.jsonl appears after first poll
```
Also expect a Telegram start-up/first-observation message. Then re-run the
homelab `scripts/snapshot.sh` and log the change in its `docs/decisions.md`.

## Upgrade path

1. Change code in this repo → build + push a **new** tag (e.g. `0.2.0`). Never
   reuse a tag. If bumping Playwright, bump `requirements.txt` and the
   Dockerfile base tag **together**.
2. Bump the tag in `~/homelab/services/tennisbot-watchd/docker-compose.yml`,
   commit + push the homelab repo.
3. `ssh homelab 'cd ~/homelab && git pull && docker compose pull tennisbot-watchd && docker compose up -d tennisbot-watchd'`

There are **no schema migrations**: state is append-only JSONL + a small JSON
bracket file, and volumes survive recreation. Updates never wipe data (only
`docker volume rm` does).

## Rollback

Repoint the compose tag to the previous version and redeploy the service —
always safe here (no migrations, backward/forward-compatible state files):

```bash
# edit services/tennisbot-watchd/docker-compose.yml back to the prior tag, then
ssh homelab 'cd ~/homelab && git pull && docker compose up -d tennisbot-watchd'
```

Emergency stop (reversible): `ssh homelab 'docker compose stop tennisbot-watchd'`.
