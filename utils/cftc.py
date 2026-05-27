from __future__ import annotations

import csv
import io
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
    market_name: str
    contract_code: str
    source_url: str


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.replace(",", "").strip()
    if stripped == "":
        return None
    return int(stripped)


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
            if not report_date_raw or open_interest is None:
                continue

            try:
                report_date = datetime.strptime(report_date_raw, "%Y-%m-%d").date()
            except ValueError as exc:
                raise CFTCDataError(f"Invalid CFTC report date: {report_date_raw}") from exc

            points_by_date[report_date] = COTOpenInterestPoint(
                report_date=report_date,
                open_interest=open_interest,
                weekly_change=_to_int(row.get("Change in Open Interest (All)")),
                market_name=market_name,
                contract_code=contract_code,
                source_url=url,
            )

    points = sorted(points_by_date.values(), key=lambda point: point.report_date)
    if not points:
        raise CFTCDataError("No CME Bitcoin open interest rows found in CFTC history data.")
    return points
