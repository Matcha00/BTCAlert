from __future__ import annotations

import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class DualTimezoneFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        utc_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        beijing_dt = utc_dt.astimezone(BEIJING_TZ)
        if datefmt:
            utc_text = utc_dt.strftime(datefmt)
            beijing_text = beijing_dt.strftime(datefmt)
        else:
            utc_text = utc_dt.isoformat(timespec="seconds")
            beijing_text = beijing_dt.isoformat(timespec="seconds")
        return f"UTC={utc_text} Beijing={beijing_text}"


def setup_logger(log_file: Path | None) -> logging.Logger:
    logger = logging.getLogger("btc_vol_alert")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = DualTimezoneFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    return logger
