import pytest

from utils.http import HTTPRequestError
from utils.telegram import TelegramError, send_telegram_message


def test_telegram_failure_raises(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise HTTPRequestError("boom")

    monkeypatch.setattr("utils.telegram.request_json", fail)

    with pytest.raises(TelegramError):
        send_telegram_message("123456:secret", "42", "hello")
