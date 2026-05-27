from __future__ import annotations

import logging

import requests


TELEGRAM_API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 10
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
        response = requests.post(url, data=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        status = getattr(exc.response, "status_code", "N/A")
        body = getattr(exc.response, "text", "")
        LOGGER.error("Telegram request failed | status=%s | body=%s", status, body)
        raise TelegramError("Telegram request failed.") from exc
    except ValueError as exc:
        LOGGER.error("Telegram response is not valid JSON | body=%s", response.text)
        raise TelegramError("Telegram response is not valid JSON.") from exc

    if not data.get("ok"):
        LOGGER.error("Telegram API returned ok=false | response=%s", data)
        raise TelegramError("Telegram API returned ok=false.")

    return data
