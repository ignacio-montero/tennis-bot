# tennisbot-watchd — 24/7 read-only drop-time hunter daemon.
#
# Base: official Playwright Python image, tag pinned to MATCH the playwright
# version in requirements.txt (1.60.0). It ships Chromium + all OS deps and a
# non-root `pwuser` with the browsers world-readable under /ms-playwright, so
# no `playwright install` is needed here. Single stage on purpose: there is
# nothing to compile, and the browser payload IS the bulk of the image — a
# multi-stage build would save nothing.
#
# Build for the homelab (amd64 box; Mac is arm64):
#   docker buildx build --platform linux/amd64 \
#     -f deploy/docker/Dockerfile \
#     -t ghcr.io/ignacio-montero/tennisbot-watchd:0.1.0 --load .
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/London \
    PYTHONPATH=/app/src \
    WATCHD_STATE_DIR=/data/watchd

WORKDIR /app

# Deps first for layer caching. playwright==1.60.0 is already in the base
# image; pip just verifies it and adds the small pure-Python deps.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + baked-in config (no secrets in either).
COPY src/ /app/src/
COPY config/ /app/config/

# Writable dirs for the non-root user:
#  /data/watchd     — watchd state (observations.jsonl, bracket.json) → volume
#  /data/drop       — drop outcomes (drop-outcomes.jsonl)             → volume
#  /data/catch      — catcher state (catcher-state.json)              → volume
#  /config          — shared prefs.json (tennisbot-config volume; prefs writes it)
#  /app/.session    — Playwright storage_state (ea_state.json)        → volume
#  /app/screenshots — diagnostic screenshots (ephemeral is fine)
# Pre-creating the volume mountpoints as pwuser means a FRESH named volume
# initialises writable for the non-root user (Docker seeds a new volume with the
# mountpoint's ownership from the image). Without /data/catch here, a fresh
# tennisbot-catch-state volume mounted root-owned and the catcher's writes failed
# `Permission denied` until a one-time manual chown (homelab decisions 2026-07-26).
# ⚠️ /config only self-heals for a volume FIRST mounted by THIS image. If the
# tennisbot-config volume is HAND-SEEDED by another image (e.g.
# `alpine sh -c 'echo … > /config/prefs.json'`), it comes out root-owned and this
# mkdir can't override the mount — the seed step must then chown to pwuser (1001),
# else the prefs writer gets EACCES on its atomic temp file (decisions 2026-07-27).
RUN mkdir -p /data/watchd /data/drop /data/catch /config /app/.session /app/screenshots \
    && chown -R pwuser:pwuser /data /config /app/.session /app/screenshots

USER pwuser

# No EXPOSE: outbound-only daemon, publishes no ports.
ENTRYPOINT ["python", "-m", "tennisbot"]
CMD ["watchd"]
