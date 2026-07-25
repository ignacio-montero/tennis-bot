"""Telegram notifications — plain HTTP via the Bot API (no heavy deps)."""

from __future__ import annotations

from pathlib import Path

import httpx

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """A Telegram API call failed. Its message is TOKEN-REDACTED so it is safe
    to log: the Bot API puts the token in the request URL (`…/bot<TOKEN>/…`),
    and a raw httpx error stringifies that URL — so `log(error=str(e))` would
    leak the token to `docker logs` (CWE-532; the token was rotated once before
    for exactly this). Raising this instead means no caller can leak it,
    whatever it logs."""


class Telegram:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0):
        self._token = bot_token
        self._base = f"{API}/bot{bot_token}"
        self._chat_id = chat_id
        self._timeout = timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def send(self, text: str) -> None:
        try:
            with httpx.Client(timeout=self._timeout) as c:
                r = c.post(
                    f"{self._base}/sendMessage",
                    data={"chat_id": self._chat_id, "text": text,
                          "parse_mode": "HTML",
                          "disable_web_page_preview": "true"},
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            # `from None` suppresses the token-bearing original in tracebacks too.
            raise TelegramError(self._redact(str(e))) from None

    def send_photo(self, image_path: str | Path, caption: str = "") -> None:
        image_path = Path(image_path)
        try:
            with httpx.Client(timeout=self._timeout) as c, \
                    image_path.open("rb") as f:
                r = c.post(
                    f"{self._base}/sendPhoto",
                    data={"chat_id": self._chat_id, "caption": caption,
                          "parse_mode": "HTML"},
                    files={"photo": (image_path.name, f, "image/png")},
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramError(self._redact(str(e))) from None
