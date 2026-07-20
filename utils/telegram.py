from __future__ import annotations

import logging

from utils.http import HTTPRequestError, request_json, sanitize_url


TELEGRAM_API_BASE = "https://api.telegram.org"
TIMEOUT = (5.0, 10.0)
LOGGER = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """Raised when Telegram delivery fails."""


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        data = request_json("POST", url, data=payload, timeout=TIMEOUT, logger=LOGGER)
    except HTTPRequestError as exc:
        LOGGER.error("Telegram request failed | url=%s", sanitize_url(url))
        raise TelegramError("Telegram request failed.") from exc

    if not data.get("ok"):
        LOGGER.error("Telegram API returned ok=false | description=%s", data.get("description"))
        raise TelegramError("Telegram API returned ok=false.")

    return data
