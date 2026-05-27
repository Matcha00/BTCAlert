from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime

import requests


CFTC_HISTORY_URL_TEMPLATE = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"
TIMEOUT_SECONDS = 10
BTC_CME_MARKET_NAME = "BITCOIN - CHICAGO MERCANTILE EXCHANGE"
BTC_CME_CONTRACT_CODE = "133741"


class CFTCDataError(RuntimeError):
    """Raised when CFTC COT data cannot be fetched or parsed."""


@dataclass(frozen=True)
class COTOpenInterestPoint:
    report_date: date
    open_interest: int
    weekly_change: int | None
    contract_units: str
    contract_size_btc: float
    market_name: str
    contract_code: str
    source_url: str

    @property
    def notional_btc(self) -> float:
        return self.open_interest * self.contract_size_btc

    @property
    def weekly_change_btc(self) -> float | None:
        if self.weekly_change is None:
            return None
        return self.weekly_change * self.contract_size_btc


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.replace(",", "").strip()
    if stripped == "":
        return None
    return int(stripped)


def _parse_contract_size_btc(contract_units: str | None) -> float | None:
    if not contract_units:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*Bitcoins?", contract_units, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def _fetch_year_rows(year: int) -> list[dict[str, str]]:
    url = CFTC_HISTORY_URL_TEMPLATE.format(year=year)
    try:
        response = requests.get(url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        archive = zipfile.ZipFile(io.BytesIO(response.content))
    except (requests.RequestException, zipfile.BadZipFile) as exc:
        raise CFTCDataError(f"Failed to fetch or open CFTC history zip: {url}") from exc

    try:
        name = archive.namelist()[0]
        with archive.open(name) as file:
            text_file = io.TextIOWrapper(file, encoding="latin-1")
            return list(csv.DictReader(text_file))
    except (IndexError, KeyError, csv.Error, UnicodeDecodeError) as exc:
        raise CFTCDataError(f"Failed to parse CFTC history zip: {url}") from exc


def get_btc_cme_open_interest_history(years: list[int]) -> list[COTOpenInterestPoint]:
    points_by_date: dict[date, COTOpenInterestPoint] = {}

    for year in years:
        url = CFTC_HISTORY_URL_TEMPLATE.format(year=year)
        rows = _fetch_year_rows(year)
        for row in rows:
            market_name = row.get("Market and Exchange Names", "").strip()
            contract_code = row.get("CFTC Contract Market Code", "").strip()
            if market_name != BTC_CME_MARKET_NAME or contract_code != BTC_CME_CONTRACT_CODE:
                continue

            report_date_raw = row.get("As of Date in Form YYYY-MM-DD", "").strip()
            open_interest = _to_int(row.get("Open Interest (All)"))
            contract_units = row.get("Contract Units", "").strip()
            contract_size_btc = _parse_contract_size_btc(contract_units)
            if not report_date_raw or open_interest is None:
                continue
            if contract_size_btc is None:
                raise CFTCDataError(f"Unable to parse BTC contract units: {contract_units}")

            try:
                report_date = datetime.strptime(report_date_raw, "%Y-%m-%d").date()
            except ValueError as exc:
                raise CFTCDataError(f"Invalid CFTC report date: {report_date_raw}") from exc

            points_by_date[report_date] = COTOpenInterestPoint(
                report_date=report_date,
                open_interest=open_interest,
                weekly_change=_to_int(row.get("Change in Open Interest (All)")),
                contract_units=contract_units,
                contract_size_btc=contract_size_btc,
                market_name=market_name,
                contract_code=contract_code,
                source_url=url,
            )

    points = sorted(points_by_date.values(), key=lambda point: point.report_date)
    if not points:
        raise CFTCDataError("No CME Bitcoin open interest rows found in CFTC history data.")
    return points
