"""Playwright session persistence — reuse a logged-in provider session across
runs so we don't log in (and risk throttling) every time."""

from __future__ import annotations

import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSION_DIR = ROOT / ".session"
STATE_PATH = SESSION_DIR / "ea_state.json"

# Re-login if the saved session is older than this (provider sessions don't
# last forever; a day is a safe ceiling).
MAX_AGE_SECONDS = 12 * 3600


def state_path() -> Path:
    return STATE_PATH


def is_fresh(path: Path | None = None) -> bool:
    path = path or STATE_PATH
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < MAX_AGE_SECONDS


def save(ctx, path: Path | None = None) -> None:
    """Persist the logged-in session.

    The written file holds live authentication cookies — a *bearer* credential:
    whoever has it is authenticated, and rotating the account password does NOT
    invalidate it (only the server expiring the session does). So it gets the
    same protection as a private key: owner-only directory, owner-only file.
    Playwright writes the file itself, so the chmod has to follow the write.
    """
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ctx.storage_state(path=str(path))
    os.chmod(path, 0o600)
