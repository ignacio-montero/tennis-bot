# Container deployment

`Dockerfile` builds the bot on the official Playwright Python base image (pinned to
match the `playwright` version in `requirements.txt`, so the image ships the exact
matching browser build).

The container is **outbound-only** — it publishes no ports and needs no inbound
firewall rule. Secrets come from an untracked `.env` in the service directory on
the host; `.env.example` here lists the variable names with placeholder values.

Images are built for `linux/amd64`, pushed to GHCR with a pinned version tag, and
pulled by the host — the host never builds.

> The operator runbook (host specifics, update loop, rollback procedure) lives in
> the private infrastructure repo alongside the service's compose file, and is
> deliberately not published here.
