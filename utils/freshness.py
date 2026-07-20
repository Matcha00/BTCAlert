from __future__ import annotations

from datetime import date, datetime, timezone


class FreshnessError(RuntimeError):
    """Raised when a data source returns stale or undated data."""


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_datetime_fresh(
    *,
    source: str,
    data_time: datetime | None,
    now_utc: datetime,
    max_age_hours: float,
    required: bool = True,
) -> None:
    if data_time is None:
        if required:
            raise FreshnessError(f"{source} data timestamp is missing or invalid.")
        return
    age_hours = (now_utc - data_time.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours < -1:
        raise FreshnessError(f"{source} data timestamp is unexpectedly in the future.")
    if age_hours > max_age_hours:
        raise FreshnessError(
            f"{source} data is stale: age={age_hours:.1f}h max={max_age_hours:.1f}h."
        )


def ensure_date_fresh(
    *,
    source: str,
    data_date: date | None,
    now_utc: datetime,
    max_age_days: int,
) -> None:
    if data_date is None:
        raise FreshnessError(f"{source} data date is missing.")
    age_days = (now_utc.date() - data_date).days
    if age_days < -1:
        raise FreshnessError(f"{source} data date is unexpectedly in the future.")
    if age_days > max_age_days:
        raise FreshnessError(
            f"{source} data is stale: age={age_days}d max={max_age_days}d."
        )
