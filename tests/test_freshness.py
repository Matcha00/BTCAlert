from datetime import date, datetime, timezone

import pytest

from utils.freshness import FreshnessError, ensure_date_fresh


def test_stale_data_raises() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with pytest.raises(FreshnessError):
        ensure_date_fresh(
            source="CFTC",
            data_date=date(2026, 6, 1),
            now_utc=now,
            max_age_days=21,
        )
