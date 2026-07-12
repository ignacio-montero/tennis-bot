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
#  /app/.session    — Playwright storage_state (ea_state.json)        → volume
#  /app/screenshots — diagnostic screenshots (ephemeral is fine)
RUN mkdir -p /data/watchd /app/.session /app/screenshots \
    && chown -R pwuser:pwuser /data /app/.session /app/screenshots

USER pwuser

# No EXPOSE: outbound-only daemon, publishes no ports.
ENTRYPOINT ["python", "-m", "tennisbot"]
CMD ["watchd"]
