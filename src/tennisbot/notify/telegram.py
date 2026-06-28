"""Telegram notifications — plain HTTP via the Bot API (no heavy deps)."""

from __future__ import annotations

from pathlib import Path

import httpx

API = "https://api.telegram.org"


class Telegram:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0):
        self._base = f"{API}/bot{bot_token}"
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, text: str) -> None:
        with httpx.Client(timeout=self._timeout) as c:
            r = c.post(
                f"{self._base}/sendMessage",
                data={"chat_id": self._chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": "true"},
            )
            r.raise_for_status()

    def send_photo(self, image_path: str | Path, caption: str = "") -> None:
        image_path = Path(image_path)
        with httpx.Client(timeout=self._timeout) as c, image_path.open("rb") as f:
            r = c.post(
                f"{self._base}/sendPhoto",
                data={"chat_id": self._chat_id, "caption": caption,
                      "parse_mode": "HTML"},
                files={"photo": (image_path.name, f, "image/png")},
            )
            r.raise_for_status()
