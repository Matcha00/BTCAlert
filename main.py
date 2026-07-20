from __future__ import annotations

import argparse
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from utils.bitmex import (
    BitmexAPIError,
    extract_bucket_timestamp,
    extract_instrument_timestamp,
    get_btc_usd_price,
    get_bucketed_trades,
    get_current_bvol_value,
    get_historical_closes,
    get_instrument,
)
from utils.cftc import CFTCDataError, COTOpenInterestPoint, get_btc_cme_open_interest_history
from utils.freshness import FreshnessError, ensure_date_fresh, ensure_datetime_fresh, parse_iso_datetime
from utils.indicators import evaluate_alerts
from utils.logger import setup_logger
from utils.stablecoins import StablecoinsDataError, USDTSupplySnapshot, get_usdt_supply_snapshot
from utils.state import (
    get_last_alert_key,
    load_state,
    mark_failure_alert_sent,
    mark_recovery_alert_sent,
    record_alert_sent,
    record_monitor_error,
    record_monitor_run,
    record_monitor_success,
    write_state_atomic,
)
from utils.telegram import TelegramError, send_telegram_message


SYMBOL = ".BVOL7D"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
MONITOR_BVOL = "bitmex_bvol"
MONITOR_CFTC_BTC_OI = "cftc_btc_oi"
MONITOR_USDT_SUPPLY = "usdt_supply"
MONITOR_NAMES = {
    MONITOR_BVOL: "BitMEX .BVOL7D",
    MONITOR_CFTC_BTC_OI: "CFTC/CME BTC 持仓",
    MONITOR_USDT_SUPPLY: "USDT 发行总量",
}

DEFAULT_HIGH_VOL_WARNING_THRESHOLD = 13.0
DEFAULT_HIGH_VOL_ALERT_THRESHOLD = 15.0
DEFAULT_LOW_VOL_LOW_THRESHOLD = 4.0
DEFAULT_LOW_VOL_MEDIUM_THRESHOLD = 3.0
DEFAULT_LOW_VOL_HIGH_THRESHOLD = 2.0
DEFAULT_BVOL_MAX_DATA_AGE_HOURS = 72.0
DEFAULT_CFTC_BTC_OI_LOOKBACK_WEEKS = 52
DEFAULT_CFTC_BTC_OI_MIN_HISTORY_WEEKS = 8
DEFAULT_CFTC_BTC_OI_MEAN_MULTIPLIER = 1.0
DEFAULT_CFTC_MAX_REPORT_AGE_DAYS = 21
DEFAULT_USDT_SUPPLY_DROP_THRESHOLD_PERCENT = 0.5
DEFAULT_FAILURE_ALERT_THRESHOLD = 3


class ConfigError(RuntimeError):
    """Raised when environment configuration is invalid."""


@dataclass
class MonitorCheck:
    monitor: str
    current_value: float | int | None
    data_date: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    alert_key: str | None = None
    alert_message: str | None = None
    alert_metadata: dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC volatility and market risk alert bot")
    parser.add_argument(
        "--monitor",
        choices=("all", "bvol", "cftc-oi", "usdt-supply"),
        default="all",
        help="Select which monitor to run. Default: all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks without sending Telegram messages or updating state.",
    )
    parser.add_argument(
        "--ignore-state",
        action="store_true",
        help="Ignore duplicate alert suppression. Useful for manual testing.",
    )
    return parser.parse_args()


def load_environment() -> None:
    env_file = os.getenv("BTC_VOL_ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()


def get_dual_now() -> tuple[datetime, datetime]:
    utc_now = datetime.now(timezone.utc)
    beijing_now = utc_now.astimezone(BEIJING_TZ)
    return utc_now, beijing_now


def format_dt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid float env {name}") from exc


def parse_optional_float_env(name: str) -> float | None:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return None
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid float env {name}") from exc


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer env {name}") from exc


def parse_bool_env(name: str, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def validate_finite_number(
    name: str,
    value: float | int | None,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> None:
    if value is None:
        raise ValueError(f"{name} is missing.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} is not finite.")
    if min_value is not None and number < min_value:
        raise ValueError(f"{name} is below minimum {min_value}.")
    if max_value is not None and number > max_value:
        raise ValueError(f"{name} is above maximum {max_value}.")


def format_number(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{digits}f}"


def format_signed_number(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):+.{digits}f}"


def format_int(value: int | float | None) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.0f}"


def format_usd(value: int | float | None) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    sign = "-" if number < 0 else ""
    abs_value = abs(number)
    if abs_value >= 1_000_000_000:
        return f"{sign}${abs_value / 1_000_000_000:,.2f}B"
    if abs_value >= 1_000_000:
        return f"{sign}${abs_value / 1_000_000:,.2f}M"
    return f"{sign}${abs_value:,.0f}"


def format_supply(value: int | float | None, symbol: str = "") -> str:
    if value is None:
        return "N/A"
    number = float(value)
    suffix = f" {symbol}" if symbol else ""
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.3f}B{suffix}"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:,.3f}M{suffix}"
    return f"{number:,.0f}{suffix}"


def calculate_mean(values: list[int | float]) -> float:
    return sum(values) / len(values)


def get_telegram_config(logger: logging.Logger) -> tuple[str, str] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured.")
        return None
    return token, chat_id


def send_telegram_safely(message: str, args: argparse.Namespace, logger: logging.Logger) -> bool:
    if args.dry_run:
        logger.info("Dry-run mode. Telegram message not sent.")
        logger.info("Dry-run Telegram message:\n%s", message)
        return True

    telegram_config = get_telegram_config(logger)
    if telegram_config is None:
        return False
    token, chat_id = telegram_config

    try:
        send_telegram_message(token=token, chat_id=chat_id, text=message)
    except TelegramError:
        logger.exception("Telegram message failed.")
        return False
    return True


def build_telegram_message(
    current_value: float,
    previous_value: float | None,
    daily_change: float | None,
    percentile_rank: float | None,
    reasons: list[str],
    emoji: str | None,
    regime: str | None,
    level: str | None,
    headline: str | None,
    utc_now: datetime,
    beijing_now: datetime,
) -> str:
    percentile_text = "N/A" if percentile_rank is None else f"{percentile_rank:.0f}%"
    reason_text = "\n".join(f"- {reason}" for reason in reasons)
    title_prefix = emoji or "⚠️"
    regime_text = regime or "波动率"
    level_text = level or "预警"
    headline_text = headline or "BTC 波动率触发预警，请检查仓位与风险。"

    return (
        f"{title_prefix} *BTC {regime_text}{level_text}*\n\n"
        f"指标：BitMEX {SYMBOL}\n"
        f"当前值：{format_number(current_value)}\n"
        f"昨日值：{format_number(previous_value)}\n"
        f"日变化：{format_signed_number(daily_change)}\n"
        f"30日分位：{percentile_text}\n\n"
        "检查时间：\n"
        f"UTC：{format_dt(utc_now)}\n"
        f"北京时间：{format_dt(beijing_now)}\n\n"
        "触发原因：\n"
        f"{reason_text}\n\n"
        "状态判断：\n"
        f"{headline_text}\n\n"
        "风险提示：\n"
        "BTC 短期波动率进入异常区间，请检查现货、杠杆、止损与对冲风险。"
    )


def check_bvol(
    *,
    args: argparse.Namespace,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
) -> MonitorCheck:
    high_vol_warning_threshold = parse_float_env(
        "HIGH_VOL_WARNING_THRESHOLD", DEFAULT_HIGH_VOL_WARNING_THRESHOLD
    )
    high_vol_alert_threshold = parse_float_env(
        "HIGH_VOL_ALERT_THRESHOLD", DEFAULT_HIGH_VOL_ALERT_THRESHOLD
    )
    low_vol_low_threshold = parse_float_env("LOW_VOL_LOW_THRESHOLD", DEFAULT_LOW_VOL_LOW_THRESHOLD)
    low_vol_medium_threshold = parse_float_env(
        "LOW_VOL_MEDIUM_THRESHOLD", DEFAULT_LOW_VOL_MEDIUM_THRESHOLD
    )
    low_vol_high_threshold = parse_float_env(
        "LOW_VOL_HIGH_THRESHOLD", DEFAULT_LOW_VOL_HIGH_THRESHOLD
    )
    max_data_age_hours = parse_float_env("BVOL_MAX_DATA_AGE_HOURS", DEFAULT_BVOL_MAX_DATA_AGE_HOURS)

    if not (
        low_vol_high_threshold
        < low_vol_medium_threshold
        < low_vol_low_threshold
        < high_vol_warning_threshold
        <= high_vol_alert_threshold
    ):
        raise ConfigError(
            "Invalid threshold order. Expected LOW_VOL_HIGH_THRESHOLD < "
            "LOW_VOL_MEDIUM_THRESHOLD < LOW_VOL_LOW_THRESHOLD < "
            "HIGH_VOL_WARNING_THRESHOLD <= HIGH_VOL_ALERT_THRESHOLD."
        )

    try:
        bucketed_trades = get_bucketed_trades(symbol=SYMBOL)
    except BitmexAPIError:
        logger.exception("Failed to fetch BitMEX bucketed trade data.")
        bucketed_trades = []

    try:
        instrument = get_instrument(symbol=SYMBOL)
    except BitmexAPIError:
        logger.exception("Failed to fetch BitMEX instrument data.")
        instrument = {}

    current_value, source = get_current_bvol_value(bucketed_trades, instrument)
    validate_finite_number(".BVOL7D current value", current_value, min_value=0, max_value=500)

    bucket_time = (
        parse_iso_datetime(extract_bucket_timestamp(bucketed_trades[0]))
        if bucketed_trades
        else None
    )
    instrument_time = parse_iso_datetime(extract_instrument_timestamp(instrument))
    if source.startswith("trade/bucketed"):
        ensure_datetime_fresh(
            source="BitMEX trade/bucketed",
            data_time=bucket_time,
            now_utc=utc_now,
            max_age_hours=max_data_age_hours,
            required=True,
        )
        data_time = bucket_time
    else:
        ensure_datetime_fresh(
            source="BitMEX instrument",
            data_time=instrument_time,
            now_utc=utc_now,
            max_age_hours=max_data_age_hours,
            required=False,
        )
        data_time = instrument_time or bucket_time

    historical_values = get_historical_closes(bucketed_trades)
    if source.startswith("trade/bucketed"):
        previous_value = historical_values[1] if len(historical_values) > 1 else None
    else:
        previous_value = historical_values[0] if historical_values else None

    result = evaluate_alerts(
        current_value=current_value,
        previous_value=previous_value,
        historical_values=historical_values,
        high_vol_warning_threshold=high_vol_warning_threshold,
        high_vol_alert_threshold=high_vol_alert_threshold,
        low_vol_low_threshold=low_vol_low_threshold,
        low_vol_medium_threshold=low_vol_medium_threshold,
        low_vol_high_threshold=low_vol_high_threshold,
    )

    data_date = (
        data_time.astimezone(BEIJING_TZ).date().isoformat()
        if data_time is not None
        else beijing_now.date().isoformat()
    )
    metadata = {
        "source": source,
        "previous_value": previous_value,
        "daily_change": result.daily_change,
        "percentile_rank": result.percentile_rank,
        "history_count": len(historical_values),
        "bucket_timestamp_utc": format_dt(bucket_time) if bucket_time else None,
        "instrument_timestamp_utc": format_dt(instrument_time) if instrument_time else None,
    }

    logger.info(
        (
            "Check result | symbol=%s | current=%.4f | source=%s | previous=%s | "
            "daily_change=%s | percentile_rank=%s | data_date=%s | reasons=%s"
        ),
        SYMBOL,
        current_value,
        source,
        format_number(previous_value, 4),
        format_signed_number(result.daily_change, 4),
        "N/A" if result.percentile_rank is None else f"{result.percentile_rank:.2f}",
        data_date,
        result.reasons,
    )

    check = MonitorCheck(
        monitor=MONITOR_BVOL,
        current_value=current_value,
        data_date=data_date,
        metadata=metadata,
    )

    if not result.should_alert:
        logger.info("No BitMEX .BVOL7D alert conditions met.")
        return check

    today_beijing = beijing_now.date().isoformat()
    check.alert_key = today_beijing
    check.alert_message = build_telegram_message(
        current_value=current_value,
        previous_value=previous_value,
        daily_change=result.daily_change,
        percentile_rank=result.percentile_rank,
        reasons=result.reasons,
        emoji=result.emoji,
        regime=result.regime,
        level=result.level,
        headline=result.headline,
        utc_now=utc_now,
        beijing_now=beijing_now,
    )
    check.alert_metadata = {
        "value": current_value,
        "source": source,
        "regime": result.regime,
        "level": result.level,
        "reasons": result.reasons,
    }
    return check


def build_cftc_oi_message(
    *,
    latest: COTOpenInterestPoint,
    lookback_weeks: int,
    mean_open_interest: float,
    mean_notional_btc: float,
    multiplier: float,
    btc_price: float,
    btc_price_source: str,
    contract_threshold: float | None,
    btc_threshold: float | None,
    btc_low_threshold: float | None,
    usd_threshold: float | None,
    reasons: list[str],
    utc_now: datetime,
    beijing_now: datetime,
) -> str:
    diff = latest.open_interest - mean_open_interest
    diff_pct = diff / mean_open_interest * 100 if mean_open_interest else 0.0
    notional_btc = latest.notional_btc
    mean_notional_usd = mean_notional_btc * btc_price
    notional_usd = notional_btc * btc_price
    weekly_change_btc = latest.weekly_change_btc
    weekly_change_usd = weekly_change_btc * btc_price if weekly_change_btc is not None else None
    reason_text = "\n".join(f"- {reason}" for reason in reasons)
    threshold_text = format_int(mean_open_interest * multiplier)
    mean_btc_threshold_text = format_int(mean_notional_btc * multiplier)
    contract_threshold_text = (
        format_int(contract_threshold) if contract_threshold is not None else "未设置"
    )
    btc_threshold_text = format_int(btc_threshold) if btc_threshold is not None else "未设置"
    btc_low_threshold_text = (
        format_int(btc_low_threshold) if btc_low_threshold is not None else "未设置"
    )
    usd_threshold_text = format_usd(usd_threshold) if usd_threshold is not None else "未设置"
    is_low_alert = any("低位阈值" in reason for reason in reasons)
    status_text = (
        "CME BTC 期货未平仓合约跌入低位区间，说明机构级期货市场敞口收缩，后续需要警惕低流动性下的波动放大。"
        if is_low_alert
        else "CME BTC 期货未平仓合约高于设定阈值，说明机构级期货市场参与度和杠杆敞口偏热。"
    )
    risk_text = (
        "OI 低位本身不判断方向，但可能意味着市场参与度下降；若后续价格突破伴随 OI 回升，需要警惕趋势重新启动。"
        if is_low_alert
        else "OI 升高本身不判断方向，但常意味着后续波动、挤仓或趋势延续风险上升，请结合价格、资金费率和波动率一起看。"
    )

    return (
        "🔴 *CME BTC 持仓量预警*\n\n"
        "指标：CFTC COT / CME Bitcoin Futures Open Interest\n"
        f"报告日期：{latest.report_date.isoformat()}\n"
        f"当前 OI：{format_int(latest.open_interest)} 张\n"
        f"合约单位：{latest.contract_units}\n"
        f"折合 BTC：{format_int(notional_btc)} BTC\n"
        f"折合美元：{format_usd(notional_usd)}\n"
        f"BTCUSD：{format_usd(btc_price)}（{btc_price_source}）\n\n"
        f"{lookback_weeks}周均值：{format_int(mean_open_interest)} 张 / "
        f"{format_int(mean_notional_btc)} BTC / {format_usd(mean_notional_usd)}\n"
        f"均值触发线：{threshold_text} 张 / {mean_btc_threshold_text} BTC"
        f"（均值 x {multiplier:.2f}）\n"
        f"固定张数阈值：{contract_threshold_text}\n"
        f"固定 BTC 高位阈值：{btc_threshold_text}\n"
        f"固定 BTC 低位阈值：{btc_low_threshold_text}\n"
        f"固定 USD 阈值：{usd_threshold_text}\n"
        f"偏离均值：{format_signed_number(diff, 0)} 张（{diff_pct:+.1f}%）\n"
        f"周变化：{format_signed_number(latest.weekly_change, 0)} 张 / "
        f"{format_signed_number(weekly_change_btc, 0)} BTC / "
        f"{format_usd(weekly_change_usd)}\n\n"
        "检查时间：\n"
        f"UTC：{format_dt(utc_now)}\n"
        f"北京时间：{format_dt(beijing_now)}\n\n"
        "触发原因：\n"
        f"{reason_text}\n\n"
        "状态判断：\n"
        f"{status_text}\n\n"
        "风险提示：\n"
        f"{risk_text}"
    )


def check_cftc_btc_oi(
    *,
    args: argparse.Namespace,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
) -> MonitorCheck:
    if not parse_bool_env("ENABLE_CFTC_BTC_OI", True):
        logger.info("CFTC BTC OI monitor disabled by ENABLE_CFTC_BTC_OI.")
        return MonitorCheck(monitor=MONITOR_CFTC_BTC_OI, current_value=None, data_date=None)

    lookback_weeks = parse_int_env("CFTC_BTC_OI_LOOKBACK_WEEKS", DEFAULT_CFTC_BTC_OI_LOOKBACK_WEEKS)
    min_history_weeks = parse_int_env(
        "CFTC_BTC_OI_MIN_HISTORY_WEEKS", DEFAULT_CFTC_BTC_OI_MIN_HISTORY_WEEKS
    )
    enable_mean_alert = parse_bool_env("ENABLE_CFTC_BTC_OI_MEAN_ALERT", True)
    mean_multiplier = parse_float_env(
        "CFTC_BTC_OI_MEAN_MULTIPLIER", DEFAULT_CFTC_BTC_OI_MEAN_MULTIPLIER
    )
    contract_threshold = parse_optional_float_env("CFTC_BTC_OI_CONTRACT_THRESHOLD")
    legacy_contract_threshold = parse_optional_float_env("CFTC_BTC_OI_ABSOLUTE_THRESHOLD")
    btc_threshold = parse_optional_float_env("CFTC_BTC_OI_BTC_THRESHOLD")
    btc_low_threshold = parse_optional_float_env("CFTC_BTC_OI_BTC_LOW_THRESHOLD")
    usd_threshold = parse_optional_float_env("CFTC_BTC_OI_USD_THRESHOLD")
    max_report_age_days = parse_int_env("CFTC_MAX_REPORT_AGE_DAYS", DEFAULT_CFTC_MAX_REPORT_AGE_DAYS)

    if contract_threshold is None:
        contract_threshold = legacy_contract_threshold

    if lookback_weeks < 1 or min_history_weeks < 1 or mean_multiplier <= 0:
        raise ConfigError("Invalid CFTC BTC OI configuration values.")

    years = sorted({utc_now.year, utc_now.year - 1})
    history = get_btc_cme_open_interest_history(years=years)
    latest = history[-1]
    ensure_date_fresh(
        source="CFTC BTC COT",
        data_date=latest.report_date,
        now_utc=utc_now,
        max_age_days=max_report_age_days,
    )
    validate_finite_number("CFTC open interest", latest.open_interest, min_value=1, max_value=2_000_000)
    validate_finite_number("CFTC notional BTC", latest.notional_btc, min_value=1, max_value=10_000_000)

    previous_points = history[:-1][-lookback_weeks:]
    if len(previous_points) < min_history_weeks:
        logger.info(
            "Not enough CFTC BTC OI history | available=%s | required=%s",
            len(previous_points),
            min_history_weeks,
        )
        return MonitorCheck(
            monitor=MONITOR_CFTC_BTC_OI,
            current_value=latest.notional_btc,
            data_date=latest.report_date.isoformat(),
            metadata={"report_date": latest.report_date.isoformat(), "history_count": len(history)},
        )

    mean_open_interest = calculate_mean([point.open_interest for point in previous_points])
    mean_notional_btc = calculate_mean([point.notional_btc for point in previous_points])
    mean_threshold = mean_open_interest * mean_multiplier

    btc_price, btc_price_source = get_btc_usd_price()
    validate_finite_number("BTCUSD price", btc_price, min_value=100, max_value=5_000_000)

    current_notional_btc = latest.notional_btc
    current_notional_usd = current_notional_btc * btc_price
    reasons: list[str] = []
    if enable_mean_alert and latest.open_interest > mean_threshold:
        reasons.append(f"当前 BTC 名义持仓 > {lookback_weeks}周均值 x {mean_multiplier:.2f}")
    if contract_threshold is not None and latest.open_interest >= contract_threshold:
        reasons.append(f"当前 OI 张数 >= 固定阈值 {format_int(contract_threshold)} 张")
    if btc_threshold is not None and current_notional_btc >= btc_threshold:
        reasons.append(f"当前 BTC 名义持仓 >= 固定阈值 {format_int(btc_threshold)} BTC")
    if btc_low_threshold is not None and current_notional_btc <= btc_low_threshold:
        reasons.append(f"当前 BTC 名义持仓 <= 低位阈值 {format_int(btc_low_threshold)} BTC")
    if usd_threshold is not None and current_notional_usd >= usd_threshold:
        reasons.append(f"当前 USD 名义持仓 >= 固定阈值 {format_usd(usd_threshold)}")

    logger.info(
        (
            "CFTC BTC OI result | report_date=%s | current=%s | weekly_change=%s | "
            "lookback_weeks=%s | mean=%.2f | notional_btc=%.2f | "
            "notional_usd=%.2f | btc_price=%.2f | multiplier=%.2f | reasons=%s"
        ),
        latest.report_date.isoformat(),
        latest.open_interest,
        latest.weekly_change,
        lookback_weeks,
        mean_open_interest,
        current_notional_btc,
        current_notional_usd,
        btc_price,
        mean_multiplier,
        reasons,
    )

    metadata = {
        "report_date": latest.report_date.isoformat(),
        "open_interest": latest.open_interest,
        "weekly_change": latest.weekly_change,
        "contract_units": latest.contract_units,
        "contract_size_btc": latest.contract_size_btc,
        "notional_btc": current_notional_btc,
        "notional_usd": current_notional_usd,
        "btc_price": btc_price,
        "btc_price_source": btc_price_source,
        "lookback_weeks": lookback_weeks,
        "mean_open_interest": mean_open_interest,
        "mean_notional_btc": mean_notional_btc,
        "enable_mean_alert": enable_mean_alert,
        "mean_multiplier": mean_multiplier,
        "contract_threshold": contract_threshold,
        "btc_threshold": btc_threshold,
        "btc_low_threshold": btc_low_threshold,
        "usd_threshold": usd_threshold,
        "history_count": len(history),
    }
    check = MonitorCheck(
        monitor=MONITOR_CFTC_BTC_OI,
        current_value=current_notional_btc,
        data_date=latest.report_date.isoformat(),
        metadata=metadata,
    )

    if not reasons:
        logger.info("No CFTC BTC OI alert conditions met.")
        return check

    alert_key = latest.report_date.isoformat()
    check.alert_key = alert_key
    check.alert_message = build_cftc_oi_message(
        latest=latest,
        lookback_weeks=lookback_weeks,
        mean_open_interest=mean_open_interest,
        mean_notional_btc=mean_notional_btc,
        multiplier=mean_multiplier,
        btc_price=btc_price,
        btc_price_source=btc_price_source,
        contract_threshold=contract_threshold,
        btc_threshold=btc_threshold,
        btc_low_threshold=btc_low_threshold,
        usd_threshold=usd_threshold,
        reasons=reasons,
        utc_now=utc_now,
        beijing_now=beijing_now,
    )
    check.alert_metadata = {**metadata, "reasons": reasons}
    return check


def build_usdt_supply_message(
    *,
    snapshot: USDTSupplySnapshot,
    drop_threshold_percent: float,
    utc_now: datetime,
    beijing_now: datetime,
) -> str:
    daily_change = snapshot.daily_change
    daily_change_percent = snapshot.daily_change_percent

    return (
        "🔴 *USDT 发行总量下跌预警*\n\n"
        "指标：USDT 总发行/流通量\n"
        "数据源：DefiLlama Stablecoins\n"
        f"当前总量：{format_supply(snapshot.current_supply, snapshot.symbol)}\n"
        f"昨日总量：{format_supply(snapshot.previous_day_supply, snapshot.symbol)}\n"
        f"24h变化：{format_supply(daily_change, snapshot.symbol)}（{daily_change_percent:+.3f}%）\n"
        f"跌幅阈值：-{drop_threshold_percent:.3f}%\n"
        f"7日前总量：{format_supply(snapshot.previous_week_supply, snapshot.symbol)}\n"
        f"30日前总量：{format_supply(snapshot.previous_month_supply, snapshot.symbol)}\n\n"
        "检查时间：\n"
        f"UTC：{format_dt(utc_now)}\n"
        f"北京时间：{format_dt(beijing_now)}\n\n"
        "触发原因：\n"
        f"- USDT 总量 24h 跌幅 >= {drop_threshold_percent:.3f}%\n\n"
        "状态判断：\n"
        "USDT 发行/流通总量出现明显收缩，可能代表稳定币流动性下降或链上资金撤出。\n\n"
        "风险提示：\n"
        "稳定币供应下滑本身不判断价格方向，但会影响市场可用美元流动性，请结合 BTC 波动率、CME OI、交易所余额和价格结构一起看。"
    )


def check_usdt_supply(
    *,
    args: argparse.Namespace,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
) -> MonitorCheck:
    if not parse_bool_env("ENABLE_USDT_SUPPLY", True):
        logger.info("USDT supply monitor disabled by ENABLE_USDT_SUPPLY.")
        return MonitorCheck(monitor=MONITOR_USDT_SUPPLY, current_value=None, data_date=None)

    drop_threshold_percent = parse_float_env(
        "USDT_SUPPLY_DROP_THRESHOLD_PERCENT",
        DEFAULT_USDT_SUPPLY_DROP_THRESHOLD_PERCENT,
    )
    if drop_threshold_percent <= 0:
        raise ConfigError("Invalid USDT_SUPPLY_DROP_THRESHOLD_PERCENT. Expected positive value.")

    stablecoin_id = os.getenv("USDT_SUPPLY_STABLECOIN_ID", "1").strip() or "1"
    snapshot = get_usdt_supply_snapshot(stablecoin_id=stablecoin_id)
    validate_finite_number("USDT current supply", snapshot.current_supply, min_value=1_000_000_000)
    validate_finite_number("USDT previous-day supply", snapshot.previous_day_supply, min_value=1_000_000_000)
    validate_finite_number(
        "USDT daily change percent",
        snapshot.daily_change_percent,
        min_value=-25,
        max_value=25,
    )

    logger.info(
        (
            "USDT supply result | current=%.2f | previous_day=%.2f | "
            "daily_change=%.2f | daily_change_percent=%.4f | threshold=%.4f"
        ),
        snapshot.current_supply,
        snapshot.previous_day_supply,
        snapshot.daily_change,
        snapshot.daily_change_percent,
        drop_threshold_percent,
    )

    data_date = beijing_now.date().isoformat()
    metadata = {
        "stablecoin_id": snapshot.stablecoin_id,
        "symbol": snapshot.symbol,
        "current_supply": snapshot.current_supply,
        "previous_day_supply": snapshot.previous_day_supply,
        "previous_week_supply": snapshot.previous_week_supply,
        "previous_month_supply": snapshot.previous_month_supply,
        "daily_change": snapshot.daily_change,
        "daily_change_percent": snapshot.daily_change_percent,
        "drop_threshold_percent": drop_threshold_percent,
        "source_url": snapshot.source_url,
        "fetched_utc": format_dt(utc_now),
    }
    check = MonitorCheck(
        monitor=MONITOR_USDT_SUPPLY,
        current_value=snapshot.current_supply,
        data_date=data_date,
        metadata=metadata,
    )

    if snapshot.daily_change_percent > -drop_threshold_percent:
        logger.info("No USDT supply alert conditions met.")
        return check

    check.alert_key = data_date
    check.alert_message = build_usdt_supply_message(
        snapshot=snapshot,
        drop_threshold_percent=drop_threshold_percent,
        utc_now=utc_now,
        beijing_now=beijing_now,
    )
    check.alert_metadata = metadata
    return check


def build_failure_message(
    monitor: str,
    state: dict[str, Any],
    utc_now: datetime,
    beijing_now: datetime,
) -> str:
    entry = state.get("monitors", {}).get(monitor, {})
    error = entry.get("last_error") if isinstance(entry, dict) else None
    error_text = error.get("message") if isinstance(error, dict) else "未知错误"
    failures = entry.get("consecutive_failures", 0) if isinstance(entry, dict) else 0
    name = MONITOR_NAMES.get(monitor, monitor)
    return (
        "🔴 *BTCAlert 数据源故障*\n\n"
        f"监控项：{name}\n"
        f"连续失败：{failures} 次\n"
        f"错误：{error_text}\n\n"
        "检查时间：\n"
        f"UTC：{format_dt(utc_now)}\n"
        f"北京时间：{format_dt(beijing_now)}\n\n"
        "处理建议：\n"
        "- 检查服务器网络、数据源可用性和 systemd 日志。\n"
        "- Telegram token 与 chat_id 不会写入日志。"
    )


def build_recovery_message(monitor: str, utc_now: datetime, beijing_now: datetime) -> str:
    name = MONITOR_NAMES.get(monitor, monitor)
    return (
        "🟢 *BTCAlert 数据源恢复*\n\n"
        f"监控项：{name}\n"
        "状态：本次检查已经成功。\n\n"
        "恢复时间：\n"
        f"UTC：{format_dt(utc_now)}\n"
        f"北京时间：{format_dt(beijing_now)}"
    )


def execute_monitor(
    *,
    monitor: str,
    check_func: Callable[[], MonitorCheck],
    args: argparse.Namespace,
    state: dict[str, Any],
    state_file: Path,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
) -> int:
    try:
        failure_threshold = parse_int_env("FAILURE_ALERT_THRESHOLD", DEFAULT_FAILURE_ALERT_THRESHOLD)
        if failure_threshold < 1:
            raise ConfigError("FAILURE_ALERT_THRESHOLD must be >= 1.")
    except ConfigError as exc:
        logger.exception("Invalid failure alert configuration | monitor=%s", monitor)
        failure_threshold = DEFAULT_FAILURE_ALERT_THRESHOLD
        return _record_failed_monitor(
            monitor=monitor,
            error=exc,
            exit_code=2,
            args=args,
            state=state,
            state_file=state_file,
            logger=logger,
            utc_now=utc_now,
            beijing_now=beijing_now,
            failure_threshold=failure_threshold,
        )

    if not args.dry_run:
        record_monitor_run(state, monitor, utc_now, beijing_now)

    try:
        result = check_func()
    except (ConfigError, ValueError) as exc:
        logger.exception("Monitor configuration or validation failed | monitor=%s", monitor)
        return _record_failed_monitor(
            monitor=monitor,
            error=exc,
            exit_code=2,
            args=args,
            state=state,
            state_file=state_file,
            logger=logger,
            utc_now=utc_now,
            beijing_now=beijing_now,
            failure_threshold=failure_threshold,
        )
    except (BitmexAPIError, CFTCDataError, StablecoinsDataError, FreshnessError) as exc:
        logger.exception("Monitor data check failed | monitor=%s", monitor)
        return _record_failed_monitor(
            monitor=monitor,
            error=exc,
            exit_code=1,
            args=args,
            state=state,
            state_file=state_file,
            logger=logger,
            utc_now=utc_now,
            beijing_now=beijing_now,
            failure_threshold=failure_threshold,
        )
    except Exception as exc:
        logger.exception("Unhandled monitor failure | monitor=%s", monitor)
        return _record_failed_monitor(
            monitor=monitor,
            error=exc,
            exit_code=1,
            args=args,
            state=state,
            state_file=state_file,
            logger=logger,
            utc_now=utc_now,
            beijing_now=beijing_now,
            failure_threshold=failure_threshold,
        )

    if args.dry_run:
        if result.alert_message:
            duplicate = get_last_alert_key(state, monitor) == result.alert_key and not args.ignore_state
            logger.info(
                "Dry-run alert evaluation | monitor=%s | alert_key=%s | duplicate=%s",
                monitor,
                result.alert_key,
                duplicate,
            )
            logger.info("Dry-run Telegram message:\n%s", result.alert_message)
        return 0

    needs_recovery = record_monitor_success(
        state,
        monitor,
        utc_now,
        beijing_now,
        current_value=result.current_value,
        data_date=result.data_date,
        metadata=result.metadata,
    )

    exit_code = 0
    if needs_recovery:
        if send_telegram_safely(build_recovery_message(monitor, utc_now, beijing_now), args, logger):
            mark_recovery_alert_sent(state, monitor, utc_now, beijing_now)
            logger.info("Recovery notification sent | monitor=%s", monitor)
        else:
            logger.error("Recovery notification failed | monitor=%s", monitor)
            exit_code = 4

    if result.alert_message and result.alert_key:
        last_alert_key = get_last_alert_key(state, monitor)
        if last_alert_key == result.alert_key and not args.ignore_state:
            logger.info(
                "Alert already sent | monitor=%s | alert_key=%s. Skipping.",
                monitor,
                result.alert_key,
            )
        elif send_telegram_safely(result.alert_message, args, logger):
            record_alert_sent(
                state,
                monitor,
                result.alert_key,
                utc_now,
                beijing_now,
                metadata=result.alert_metadata,
            )
            logger.info("Alert sent successfully and state updated | monitor=%s", monitor)
        else:
            record_monitor_error(
                state,
                monitor,
                utc_now,
                beijing_now,
                error="Telegram alert delivery failed.",
                failure_threshold=failure_threshold,
            )
            exit_code = 4

    write_state_atomic(state_file, state, logger)
    return exit_code


def _record_failed_monitor(
    *,
    monitor: str,
    error: Exception,
    exit_code: int,
    args: argparse.Namespace,
    state: dict[str, Any],
    state_file: Path,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
    failure_threshold: int,
) -> int:
    if args.dry_run:
        logger.info("Dry-run mode. Failure state not updated | monitor=%s", monitor)
        return exit_code

    should_alert = record_monitor_error(
        state,
        monitor,
        utc_now,
        beijing_now,
        error=error,
        failure_threshold=failure_threshold,
    )
    if should_alert:
        if send_telegram_safely(build_failure_message(monitor, state, utc_now, beijing_now), args, logger):
            mark_failure_alert_sent(state, monitor, utc_now, beijing_now)
            logger.info("Failure notification sent | monitor=%s", monitor)
        else:
            logger.error("Failure notification failed | monitor=%s", monitor)
            exit_code = 4

    write_state_atomic(state_file, state, logger)
    return exit_code


def resolve_log_file() -> Path | None:
    raw_value = os.getenv("LOG_FILE", "logs/btc_vol_alert.log")
    if raw_value is None or raw_value.strip() == "":
        return None
    return Path(raw_value)


def main() -> int:
    args = parse_args()
    load_environment()

    logger = setup_logger(resolve_log_file())
    state_file = Path(os.getenv("STATE_FILE", "state.json"))

    utc_now, beijing_now = get_dual_now()
    logger.info(
        "Starting BTC alert checks | monitor=%s | UTC=%s | Beijing=%s | dry_run=%s",
        args.monitor,
        format_dt(utc_now),
        format_dt(beijing_now),
        args.dry_run,
    )

    state = load_state(state_file, logger)
    exit_codes: list[int] = []

    if args.monitor in {"all", "bvol"}:
        exit_codes.append(
            execute_monitor(
                monitor=MONITOR_BVOL,
                check_func=lambda: check_bvol(
                    args=args, logger=logger, utc_now=utc_now, beijing_now=beijing_now
                ),
                args=args,
                state=state,
                state_file=state_file,
                logger=logger,
                utc_now=utc_now,
                beijing_now=beijing_now,
            )
        )

    if args.monitor in {"all", "cftc-oi"}:
        exit_codes.append(
            execute_monitor(
                monitor=MONITOR_CFTC_BTC_OI,
                check_func=lambda: check_cftc_btc_oi(
                    args=args, logger=logger, utc_now=utc_now, beijing_now=beijing_now
                ),
                args=args,
                state=state,
                state_file=state_file,
                logger=logger,
                utc_now=utc_now,
                beijing_now=beijing_now,
            )
        )

    if args.monitor in {"all", "usdt-supply"}:
        exit_codes.append(
            execute_monitor(
                monitor=MONITOR_USDT_SUPPLY,
                check_func=lambda: check_usdt_supply(
                    args=args, logger=logger, utc_now=utc_now, beijing_now=beijing_now
                ),
                args=args,
                state=state,
                state_file=state_file,
                logger=logger,
                utc_now=utc_now,
                beijing_now=beijing_now,
            )
        )

    return max(exit_codes) if exit_codes else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.getLogger("btc_vol_alert").exception("Unhandled exception.")
        raise
