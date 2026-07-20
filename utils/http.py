from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT = (5.0, 10.0)
TOKEN_RE = re.compile(r"(/bot)[^/]+")


class HTTPRequestError(RuntimeError):
    """Raised when an HTTP request fails after bounded retries."""


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    backoff_base_seconds: float = 0.75
    backoff_cap_seconds: float = 6.0
    jitter_seconds: float = 0.25


def sanitize_url(url: str) -> str:
    return TOKEN_RE.sub(r"\1REDACTED", url)


def _should_retry(exc: requests.RequestException) -> bool:
    response = getattr(exc, "response", None)
    if response is None:
        return True
    status_code = getattr(response, "status_code", None)
    return status_code == 429 or (status_code is not None and status_code >= 500)


def _sleep_for_attempt(attempt: int, config: RetryConfig) -> None:
    delay = min(
        config.backoff_cap_seconds,
        config.backoff_base_seconds * (2 ** max(0, attempt - 1)),
    )
    if config.jitter_seconds > 0:
        delay += random.uniform(0, config.jitter_seconds)
    time.sleep(delay)


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry_config: RetryConfig = RetryConfig(),
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> requests.Response:
    log = logger or LOGGER
    http = session or requests.Session()
    safe_url = sanitize_url(url)
    last_exc: requests.RequestException | None = None

    for attempt in range(1, retry_config.max_attempts + 1):
        try:
            response = http.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            retryable = _should_retry(exc)
            status = getattr(getattr(exc, "response", None), "status_code", "N/A")
            if attempt >= retry_config.max_attempts or not retryable:
                log.error(
                    "HTTP request failed | method=%s | url=%s | status=%s | attempt=%s/%s",
                    method.upper(),
                    safe_url,
                    status,
                    attempt,
                    retry_config.max_attempts,
                )
                raise HTTPRequestError(f"HTTP request failed: {safe_url}") from exc

            log.warning(
                "HTTP request retrying | method=%s | url=%s | status=%s | attempt=%s/%s",
                method.upper(),
                safe_url,
                status,
                attempt,
                retry_config.max_attempts,
            )
            _sleep_for_attempt(attempt, retry_config)

    raise HTTPRequestError(f"HTTP request failed: {safe_url}") from last_exc


def request_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry_config: RetryConfig = RetryConfig(),
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> Any:
    response = request(
        method,
        url,
        params=params,
        data=data,
        json_body=json_body,
        timeout=timeout,
        retry_config=retry_config,
        session=session,
        logger=logger,
    )
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPRequestError(f"HTTP response is not valid JSON: {sanitize_url(url)}") from exc


def request_bytes(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry_config: RetryConfig = RetryConfig(),
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> bytes:
    response = request(
        method,
        url,
        params=params,
        timeout=timeout,
        retry_config=retry_config,
        session=session,
        logger=logger,
    )
    return response.content
