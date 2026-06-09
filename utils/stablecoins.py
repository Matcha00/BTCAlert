from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


DEFILLAMA_STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins"
TIMEOUT_SECONDS = 10
DEFAULT_USDT_STABLECOIN_ID = "1"


class StablecoinsDataError(RuntimeError):
    """Raised when stablecoin supply data cannot be fetched or parsed."""


@dataclass(frozen=True)
class USDTSupplySnapshot:
    stablecoin_id: str
    name: str
    symbol: str
    current_supply: float
    previous_day_supply: float
    previous_week_supply: float | None
    previous_month_supply: float | None
    source_url: str

    @property
    def daily_change(self) -> float:
        return self.current_supply - self.previous_day_supply

    @property
    def daily_change_percent(self) -> float:
        return self.daily_change / self.previous_day_supply * 100


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pegged_usd(container: dict[str, Any] | None) -> float | None:
    if not isinstance(container, dict):
        return None
    return _as_float(container.get("peggedUSD"))


def get_usdt_supply_snapshot(stablecoin_id: str = DEFAULT_USDT_STABLECOIN_ID) -> USDTSupplySnapshot:
    params = {"includePrices": "true"}
    try:
        response = requests.get(DEFILLAMA_STABLECOINS_URL, params=params, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise StablecoinsDataError("Failed to fetch DefiLlama stablecoins data.") from exc
    except ValueError as exc:
        raise StablecoinsDataError("DefiLlama stablecoins response is not valid JSON.") from exc

    assets = data.get("peggedAssets") if isinstance(data, dict) else None
    if not isinstance(assets, list):
        raise StablecoinsDataError("DefiLlama stablecoins response missing peggedAssets list.")

    asset = None
    for item in assets:
        if not isinstance(item, dict):
            continue
        if str(item.get("id")) == stablecoin_id:
            asset = item
            break
    if asset is None:
        for item in assets:
            if isinstance(item, dict) and item.get("symbol") == "USDT":
                asset = item
                break

    if not isinstance(asset, dict):
        raise StablecoinsDataError(f"USDT stablecoin id={stablecoin_id} not found.")

    current_supply = _pegged_usd(asset.get("circulating"))
    previous_day_supply = _pegged_usd(asset.get("circulatingPrevDay"))
    if current_supply is None or previous_day_supply is None or previous_day_supply <= 0:
        raise StablecoinsDataError("USDT current or previous-day supply is missing.")

    return USDTSupplySnapshot(
        stablecoin_id=str(asset.get("id", stablecoin_id)),
        name=str(asset.get("name", "Tether")),
        symbol=str(asset.get("symbol", "USDT")),
        current_supply=current_supply,
        previous_day_supply=previous_day_supply,
        previous_week_supply=_pegged_usd(asset.get("circulatingPrevWeek")),
        previous_month_supply=_pegged_usd(asset.get("circulatingPrevMonth")),
        source_url=DEFILLAMA_STABLECOINS_URL,
    )
