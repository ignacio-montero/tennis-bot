"""Playwright session persistence — reuse a logged-in Everyone Active session
across runs so we don't log in (and risk throttling) every time."""

from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSION_DIR = ROOT / ".session"
STATE_PATH = SESSION_DIR / "ea_state.json"

# Re-login if the saved session is older than this (Everyone Active sessions
# don't last forever; a day is a safe ceiling).
MAX_AGE_SECONDS = 12 * 3600


def state_path() -> Path:
    return STATE_PATH


def is_fresh(path: Path | None = None) -> bool:
    path = path or STATE_PATH
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < MAX_AGE_SECONDS


def save(ctx, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    ctx.storage_state(path=str(path))
