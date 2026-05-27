from __future__ import annotations

from typing import Any

import requests


BASE_URL = "https://www.bitmex.com/api/v1"
TIMEOUT_SECONDS = 10
PRICE_FALLBACK_FIELDS = ("lastPrice", "markPrice", "indicativeSettlePrice")


class BitmexAPIError(RuntimeError):
    """Raised when BitMEX data cannot be fetched or parsed."""


def _get_json(path: str, params: dict[str, Any]) -> Any:
    url = f"{BASE_URL}{path}"
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise BitmexAPIError(f"BitMEX request failed: {url}") from exc
    except ValueError as exc:
        raise BitmexAPIError(f"BitMEX response is not valid JSON: {url}") from exc


def get_instrument(symbol: str = ".BVOL7D") -> dict[str, Any]:
    data = _get_json("/instrument", {"symbol": symbol})
    if not isinstance(data, list):
        raise BitmexAPIError("BitMEX instrument response is not a list.")
    if not data:
        raise BitmexAPIError(f"No BitMEX instrument data returned for {symbol}.")
    if not isinstance(data[0], dict):
        raise BitmexAPIError("BitMEX instrument item is not an object.")
    return data[0]


def get_bucketed_trades(symbol: str = ".BVOL7D", count: int = 30) -> list[dict[str, Any]]:
    params = {
        "binSize": "1d",
        "partial": "false",
        "symbol": symbol,
        "count": count,
        "reverse": "true",
    }
    data = _get_json("/trade/bucketed", params)
    if not isinstance(data, list):
        raise BitmexAPIError("BitMEX trade/bucketed response is not a list.")

    rows: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _bucket_close(row: dict[str, Any]) -> float | None:
    for field in ("close", "vwap", "lastPrice"):
        number = _as_float(row.get(field))
        if number is not None:
            return number
    return None


def get_historical_closes(bucketed_trades: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in bucketed_trades:
        value = _bucket_close(row)
        if value is not None:
            values.append(value)
    return values


def _instrument_fallback_price(instrument: dict[str, Any]) -> tuple[float, str] | None:
    for field in PRICE_FALLBACK_FIELDS:
        value = _as_float(instrument.get(field))
        if value is not None:
            return value, field
    return None


def get_current_bvol_value(
    bucketed_trades: list[dict[str, Any]],
    instrument: dict[str, Any],
) -> tuple[float, str]:
    if bucketed_trades:
        bucket_value = _bucket_close(bucketed_trades[0])
        if bucket_value is not None:
            return bucket_value, "trade/bucketed.close"

    fallback = _instrument_fallback_price(instrument)
    if fallback is not None:
        return fallback

    raise BitmexAPIError(
        "No current value available from trade/bucketed or instrument fallback fields."
    )
