from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE: dict[str, Any] = {
    "last_alert_date": None,
    "last_alert_keys": {},
    "monitors": {},
    "system": {},
}


def format_dt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_state() -> dict[str, Any]:
    return deepcopy(DEFAULT_STATE)


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _default_state()
    normalized.update(state)
    if not isinstance(normalized.get("last_alert_keys"), dict):
        normalized["last_alert_keys"] = {}
    if not isinstance(normalized.get("monitors"), dict):
        normalized["monitors"] = {}
    if not isinstance(normalized.get("system"), dict):
        normalized["system"] = {}
    return normalized


def load_state(path: Path, logger: logging.Logger) -> dict[str, Any]:
    if not path.exists():
        return _default_state()

    try:
        with path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        backup_path = path.with_name(f"{path.name}.corrupt.{_timestamp()}")
        try:
            shutil.copy2(path, backup_path)
            logger.error("State file is corrupt. Backed up to %s", backup_path)
        except OSError:
            logger.exception("Failed to back up corrupt state file: %s", path)
        logger.exception("Failed to read state file: %s", path)
        state = _default_state()
        state["system"]["state_error"] = {
            "path": str(path),
            "backup_path": str(backup_path),
            "error": f"{type(exc).__name__}: {exc}",
            "detected_utc": _timestamp(),
        }
        return state

    if not isinstance(loaded, dict):
        logger.error("Invalid state file format: %s", path)
        state = _default_state()
        state["system"]["state_error"] = {
            "path": str(path),
            "error": "state root is not an object",
            "detected_utc": _timestamp(),
        }
        return state

    return normalize_state(loaded)


def write_state_atomic(path: Path, state: dict[str, Any], logger: logging.Logger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalize_state(state), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd: int | None = None
    temp_name: str | None = None
    mode = 0o640
    if path.exists():
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o640

    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = None
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        temp_name = None
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        logger.exception("Failed to atomically write state file: %s", path)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def monitor_state(state: dict[str, Any], monitor: str) -> dict[str, Any]:
    monitors = state.setdefault("monitors", {})
    if not isinstance(monitors, dict):
        state["monitors"] = {}
        monitors = state["monitors"]
    entry = monitors.setdefault(monitor, {})
    if not isinstance(entry, dict):
        monitors[monitor] = {}
        entry = monitors[monitor]
    return entry


def get_last_alert_key(state: dict[str, Any], monitor: str) -> str | None:
    alert_keys = state.get("last_alert_keys", {})
    if isinstance(alert_keys, dict) and monitor in alert_keys:
        value = alert_keys.get(monitor)
        return str(value) if value is not None else None
    if monitor == "bitmex_bvol":
        value = state.get("last_alert_date")
        return str(value) if value is not None else None
    return None


def record_monitor_run(
    state: dict[str, Any],
    monitor: str,
    utc_now: datetime,
    beijing_now: datetime,
) -> None:
    entry = monitor_state(state, monitor)
    entry["last_run_utc"] = format_dt(utc_now)
    entry["last_run_beijing"] = format_dt(beijing_now)


def record_monitor_success(
    state: dict[str, Any],
    monitor: str,
    utc_now: datetime,
    beijing_now: datetime,
    *,
    current_value: float | int | None,
    data_date: str | None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    entry = monitor_state(state, monitor)
    had_failure_alert = bool(entry.get("failure_alert_active"))
    entry.update(
        {
            "status": "ok",
            "last_success_utc": format_dt(utc_now),
            "last_success_beijing": format_dt(beijing_now),
            "current_value": current_value,
            "data_date": data_date,
            "last_error": None,
            "consecutive_failures": 0,
        }
    )
    if metadata:
        entry.update(metadata)
    return had_failure_alert


def record_monitor_error(
    state: dict[str, Any],
    monitor: str,
    utc_now: datetime,
    beijing_now: datetime,
    *,
    error: Exception | str,
    failure_threshold: int,
) -> bool:
    entry = monitor_state(state, monitor)
    failures = int(entry.get("consecutive_failures") or 0) + 1
    error_text = str(error)
    entry.update(
        {
            "status": "error",
            "last_error": {
                "message": error_text,
                "type": type(error).__name__ if not isinstance(error, str) else "Error",
                "utc": format_dt(utc_now),
                "beijing": format_dt(beijing_now),
            },
            "consecutive_failures": failures,
        }
    )
    return failures >= failure_threshold and not bool(entry.get("failure_alert_active"))


def mark_failure_alert_sent(
    state: dict[str, Any],
    monitor: str,
    utc_now: datetime,
    beijing_now: datetime,
) -> None:
    entry = monitor_state(state, monitor)
    entry["failure_alert_active"] = True
    entry["last_failure_alert_utc"] = format_dt(utc_now)
    entry["last_failure_alert_beijing"] = format_dt(beijing_now)


def mark_recovery_alert_sent(
    state: dict[str, Any],
    monitor: str,
    utc_now: datetime,
    beijing_now: datetime,
) -> None:
    entry = monitor_state(state, monitor)
    entry["failure_alert_active"] = False
    entry["last_recovery_alert_utc"] = format_dt(utc_now)
    entry["last_recovery_alert_beijing"] = format_dt(beijing_now)


def record_alert_sent(
    state: dict[str, Any],
    monitor: str,
    alert_key: str,
    utc_now: datetime,
    beijing_now: datetime,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    alert_keys = state.setdefault("last_alert_keys", {})
    if isinstance(alert_keys, dict):
        alert_keys[monitor] = alert_key

    entry = monitor_state(state, monitor)
    entry.update(
        {
            "last_alert_key": alert_key,
            "last_alert_utc": format_dt(utc_now),
            "last_alert_beijing": format_dt(beijing_now),
        }
    )
    if metadata:
        entry.update(metadata)

    if monitor == "bitmex_bvol":
        state["last_alert_date"] = alert_key
        state["last_alert_utc"] = format_dt(utc_now)
        state["last_alert_beijing"] = format_dt(beijing_now)
        if metadata:
            state["last_alert_value"] = metadata.get("value")
            state["last_alert_regime"] = metadata.get("regime")
            state["last_alert_level"] = metadata.get("level")
            state["last_alert_reasons"] = metadata.get("reasons")
