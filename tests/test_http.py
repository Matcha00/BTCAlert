import requests

from utils.http import HTTPRequestError, request_json, sanitize_url


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FlakySession:
    def __init__(self):
        self.calls = 0
        self.timeouts = []

    def request(self, **kwargs):
        self.calls += 1
        self.timeouts.append(kwargs["timeout"])
        if self.calls < 3:
            raise requests.Timeout("timeout")
        return FakeResponse({"ok": True})


def test_request_json_retries_timeout(monkeypatch) -> None:
    session = FlakySession()
    monkeypatch.setattr("utils.http.time.sleep", lambda _: None)

    data = request_json("GET", "https://example.test/data", session=session)

    assert data == {"ok": True}
    assert session.calls == 3
    assert session.timeouts == [(5.0, 10.0), (5.0, 10.0), (5.0, 10.0)]


def test_request_json_raises_after_retries(monkeypatch) -> None:
    class DeadSession:
        def request(self, **kwargs):
            raise requests.Timeout("timeout")

    monkeypatch.setattr("utils.http.time.sleep", lambda _: None)

    try:
        request_json("GET", "https://example.test/data", session=DeadSession())
    except HTTPRequestError as exc:
        assert "https://example.test/data" in str(exc)
    else:
        raise AssertionError("HTTPRequestError was not raised")


def test_sanitize_telegram_url() -> None:
    url = "https://api.telegram.org/bot123456:secret/sendMessage"
    assert sanitize_url(url) == "https://api.telegram.org/botREDACTED/sendMessage"
