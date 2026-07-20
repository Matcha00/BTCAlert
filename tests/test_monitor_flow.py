import argparse
import logging
from datetime import datetime, timezone

import main


def _args(**overrides):
    defaults = {"dry_run": False, "ignore_state": False, "monitor": "all"}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_duplicate_alert_is_suppressed(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    state = {"last_alert_keys": {"bitmex_bvol": "2026-07-20"}, "monitors": {}}
    sent = []
    monkeypatch.setenv("FAILURE_ALERT_THRESHOLD", "1")
    monkeypatch.setattr(main, "send_telegram_safely", lambda message, args, logger: sent.append(message) or True)

    result = main.execute_monitor(
        monitor=main.MONITOR_BVOL,
        check_func=lambda: main.MonitorCheck(
            monitor=main.MONITOR_BVOL,
            current_value=15,
            data_date="2026-07-20",
            alert_key="2026-07-20",
            alert_message="alert",
        ),
        args=_args(),
        state=state,
        state_file=tmp_path / "state.json",
        logger=logging.getLogger("test_duplicate_alert_is_suppressed"),
        utc_now=now,
        beijing_now=now,
    )

    assert result == 0
    assert sent == []


def test_failure_and_recovery_notifications(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    state: dict = {}
    sent = []
    monkeypatch.setenv("FAILURE_ALERT_THRESHOLD", "1")
    monkeypatch.setattr(main, "send_telegram_safely", lambda message, args, logger: sent.append(message) or True)

    failed = main.execute_monitor(
        monitor=main.MONITOR_BVOL,
        check_func=lambda: (_ for _ in ()).throw(main.BitmexAPIError("source down")),
        args=_args(),
        state=state,
        state_file=tmp_path / "state.json",
        logger=logging.getLogger("test_failure_and_recovery_notifications"),
        utc_now=now,
        beijing_now=now,
    )

    assert failed == 1
    assert state["monitors"][main.MONITOR_BVOL]["failure_alert_active"] is True
    assert "数据源故障" in sent[-1]

    recovered = main.execute_monitor(
        monitor=main.MONITOR_BVOL,
        check_func=lambda: main.MonitorCheck(
            monitor=main.MONITOR_BVOL,
            current_value=5,
            data_date="2026-07-20",
        ),
        args=_args(),
        state=state,
        state_file=tmp_path / "state.json",
        logger=logging.getLogger("test_failure_and_recovery_notifications"),
        utc_now=now,
        beijing_now=now,
    )

    assert recovered == 0
    assert state["monitors"][main.MONITOR_BVOL]["failure_alert_active"] is False
    assert "数据源恢复" in sent[-1]
