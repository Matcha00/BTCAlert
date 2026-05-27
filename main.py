import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from utils.bitmex import (
    BitmexAPIError,
    get_bucketed_trades,
    get_btc_usd_price,
    get_current_bvol_value,
    get_instrument,
    get_historical_closes,
)
from utils.cftc import CFTCDataError, COTOpenInterestPoint, get_btc_cme_open_interest_history
from utils.indicators import evaluate_alerts
from utils.logger import setup_logger
from utils.telegram import TelegramError, send_telegram_message


SYMBOL = ".BVOL7D"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
MONITOR_BVOL = "bitmex_bvol"
MONITOR_CFTC_BTC_OI = "cftc_btc_oi"
DEFAULT_HIGH_VOL_WARNING_THRESHOLD = 13.0
DEFAULT_HIGH_VOL_ALERT_THRESHOLD = 15.0
DEFAULT_LOW_VOL_LOW_THRESHOLD = 4.0
DEFAULT_LOW_VOL_MEDIUM_THRESHOLD = 3.0
DEFAULT_LOW_VOL_HIGH_THRESHOLD = 2.0
DEFAULT_CFTC_BTC_OI_LOOKBACK_WEEKS = 52
DEFAULT_CFTC_BTC_OI_MIN_HISTORY_WEEKS = 8
DEFAULT_CFTC_BTC_OI_MEAN_MULTIPLIER = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC volatility and CME open interest alert bot")
    parser.add_argument(
        "--monitor",
        choices=("all", "bvol", "cftc-oi"),
        default="all",
        help="Select which monitor to run. Default: all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all checks without sending Telegram messages or updating state.json.",
    )
    parser.add_argument(
        "--ignore-state",
        action="store_true",
        help="Ignore same-day alert suppression. Useful for manual testing.",
    )
    return parser.parse_args()


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
    return float(raw_value)


def parse_optional_float_env(name: str) -> float | None:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return None
    return float(raw_value)


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return int(raw_value)


def parse_bool_env(name: str, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def read_state(path: Path, logger: logging.Logger) -> dict:
    if not path.exists():
        return {"last_alert_date": None}

    try:
        with path.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read state file: %s", path)
        return {"last_alert_date": None}

    if not isinstance(state, dict):
        logger.error("Invalid state file format: %s", path)
        return {"last_alert_date": None}

    return state


def write_state(path: Path, state: dict, logger: logging.Logger) -> None:
    try:
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        logger.exception("Failed to write state file: %s", path)
        raise


def format_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def format_signed_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.{digits}f}"


def format_int(value: int | float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.0f}"


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


def get_last_alert_key(state: dict, monitor: str) -> str | None:
    alert_keys = state.get("last_alert_keys", {})
    if isinstance(alert_keys, dict) and monitor in alert_keys:
        return alert_keys.get(monitor)
    if monitor == MONITOR_BVOL:
        return state.get("last_alert_date")
    return None


def update_monitor_state(
    state: dict,
    monitor: str,
    alert_key: str,
    utc_now: datetime,
    beijing_now: datetime,
    metadata: dict,
) -> None:
    alert_keys = state.setdefault("last_alert_keys", {})
    if isinstance(alert_keys, dict):
        alert_keys[monitor] = alert_key

    monitors = state.setdefault("monitors", {})
    if isinstance(monitors, dict):
        monitors[monitor] = {
            "last_alert_key": alert_key,
            "last_alert_utc": format_dt(utc_now),
            "last_alert_beijing": format_dt(beijing_now),
            **metadata,
        }

    if monitor == MONITOR_BVOL:
        state["last_alert_date"] = alert_key
        state["last_alert_utc"] = format_dt(utc_now)
        state["last_alert_beijing"] = format_dt(beijing_now)
        state["last_alert_value"] = metadata.get("value")
        state["last_alert_regime"] = metadata.get("regime")
        state["last_alert_level"] = metadata.get("level")
        state["last_alert_reasons"] = metadata.get("reasons")


def get_telegram_config(logger: logging.Logger) -> tuple[str, str] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured.")
        return None
    return token, chat_id


def send_alert(
    *,
    message: str,
    args: argparse.Namespace,
    state: dict,
    state_file: Path,
    logger: logging.Logger,
    monitor: str,
    alert_key: str,
    utc_now: datetime,
    beijing_now: datetime,
    metadata: dict,
) -> int:
    if args.dry_run:
        logger.info("Dry-run mode. Telegram message not sent and state not updated.")
        logger.info("Dry-run Telegram message:\n%s", message)
        return 0

    telegram_config = get_telegram_config(logger)
    if telegram_config is None:
        return 3
    token, chat_id = telegram_config

    try:
        send_telegram_message(token=token, chat_id=chat_id, text=message)
    except TelegramError:
        logger.exception("Telegram message failed.")
        return 4

    update_monitor_state(
        state=state,
        monitor=monitor,
        alert_key=alert_key,
        utc_now=utc_now,
        beijing_now=beijing_now,
        metadata=metadata,
    )
    write_state(state_file, state, logger)
    logger.info("Alert sent successfully and state updated | monitor=%s", monitor)
    return 0


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


def run_bvol_monitor(
    *,
    args: argparse.Namespace,
    state: dict,
    state_file: Path,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
) -> int:
    try:
        high_vol_warning_threshold = parse_float_env(
            "HIGH_VOL_WARNING_THRESHOLD", DEFAULT_HIGH_VOL_WARNING_THRESHOLD
        )
        high_vol_alert_threshold = parse_float_env(
            "HIGH_VOL_ALERT_THRESHOLD", DEFAULT_HIGH_VOL_ALERT_THRESHOLD
        )
        low_vol_low_threshold = parse_float_env(
            "LOW_VOL_LOW_THRESHOLD", DEFAULT_LOW_VOL_LOW_THRESHOLD
        )
        low_vol_medium_threshold = parse_float_env(
            "LOW_VOL_MEDIUM_THRESHOLD", DEFAULT_LOW_VOL_MEDIUM_THRESHOLD
        )
        low_vol_high_threshold = parse_float_env(
            "LOW_VOL_HIGH_THRESHOLD", DEFAULT_LOW_VOL_HIGH_THRESHOLD
        )
    except ValueError:
        logger.exception("Invalid BitMEX .BVOL7D threshold configuration.")
        return 2

    if not (
        low_vol_high_threshold < low_vol_medium_threshold < low_vol_low_threshold
        < high_vol_warning_threshold <= high_vol_alert_threshold
    ):
        logger.error(
            (
                "Invalid threshold order. Expected LOW_VOL_HIGH_THRESHOLD < "
                "LOW_VOL_MEDIUM_THRESHOLD < LOW_VOL_LOW_THRESHOLD < "
                "HIGH_VOL_WARNING_THRESHOLD <= HIGH_VOL_ALERT_THRESHOLD."
            )
        )
        return 2

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

    try:
        current_value, source = get_current_bvol_value(bucketed_trades, instrument)
    except BitmexAPIError:
        logger.exception("Unable to determine current %s value.", SYMBOL)
        return 1

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

    logger.info(
        (
            "Check result | symbol=%s | current=%.4f | source=%s | previous=%s | "
            "daily_change=%s | percentile_rank=%s | reasons=%s"
        ),
        SYMBOL,
        current_value,
        source,
        format_number(previous_value, 4),
        format_signed_number(result.daily_change, 4),
        "N/A" if result.percentile_rank is None else f"{result.percentile_rank:.2f}",
        result.reasons,
    )

    if not result.should_alert:
        logger.info("No BitMEX .BVOL7D alert conditions met.")
        return 0

    today_beijing = beijing_now.date().isoformat()
    last_alert_date = get_last_alert_key(state, MONITOR_BVOL)

    if last_alert_date == today_beijing and not args.ignore_state:
        logger.info("Alert already sent today Beijing date=%s. Skipping.", today_beijing)
        return 0

    message = build_telegram_message(
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

    return send_alert(
        message=message,
        args=args,
        state=state,
        state_file=state_file,
        logger=logger,
        monitor=MONITOR_BVOL,
        alert_key=today_beijing,
        utc_now=utc_now,
        beijing_now=beijing_now,
        metadata={
            "value": current_value,
            "source": source,
            "regime": result.regime,
            "level": result.level,
            "reasons": result.reasons,
        },
    )


def calculate_mean(values: list[int]) -> float:
    return sum(values) / len(values)


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
    usd_threshold_text = format_usd(usd_threshold) if usd_threshold is not None else "未设置"

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
        f"固定 BTC 阈值：{btc_threshold_text}\n"
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
        "CME BTC 期货未平仓合约高于历史均值，说明机构级期货市场参与度和杠杆敞口偏热。\n\n"
        "风险提示：\n"
        "OI 升高本身不判断方向，但常意味着后续波动、挤仓或趋势延续风险上升，请结合价格、资金费率和波动率一起看。"
    )


def run_cftc_btc_oi_monitor(
    *,
    args: argparse.Namespace,
    state: dict,
    state_file: Path,
    logger: logging.Logger,
    utc_now: datetime,
    beijing_now: datetime,
) -> int:
    if not parse_bool_env("ENABLE_CFTC_BTC_OI", True):
        logger.info("CFTC BTC OI monitor disabled by ENABLE_CFTC_BTC_OI.")
        return 0

    try:
        lookback_weeks = parse_int_env(
            "CFTC_BTC_OI_LOOKBACK_WEEKS", DEFAULT_CFTC_BTC_OI_LOOKBACK_WEEKS
        )
        min_history_weeks = parse_int_env(
            "CFTC_BTC_OI_MIN_HISTORY_WEEKS", DEFAULT_CFTC_BTC_OI_MIN_HISTORY_WEEKS
        )
        mean_multiplier = parse_float_env(
            "CFTC_BTC_OI_MEAN_MULTIPLIER", DEFAULT_CFTC_BTC_OI_MEAN_MULTIPLIER
        )
        contract_threshold = parse_optional_float_env("CFTC_BTC_OI_CONTRACT_THRESHOLD")
        legacy_contract_threshold = parse_optional_float_env("CFTC_BTC_OI_ABSOLUTE_THRESHOLD")
        btc_threshold = parse_optional_float_env("CFTC_BTC_OI_BTC_THRESHOLD")
        usd_threshold = parse_optional_float_env("CFTC_BTC_OI_USD_THRESHOLD")
    except ValueError:
        logger.exception("Invalid CFTC BTC OI threshold configuration.")
        return 2

    if contract_threshold is None:
        contract_threshold = legacy_contract_threshold

    if lookback_weeks < 1 or min_history_weeks < 1 or mean_multiplier <= 0:
        logger.error("Invalid CFTC BTC OI configuration values.")
        return 2

    years = sorted({utc_now.year, utc_now.year - 1})
    try:
        history = get_btc_cme_open_interest_history(years=years)
    except CFTCDataError:
        logger.exception("Failed to fetch CFTC BTC open interest data.")
        return 1

    latest = history[-1]
    previous_points = history[:-1][-lookback_weeks:]
    if len(previous_points) < min_history_weeks:
        logger.info(
            "Not enough CFTC BTC OI history | available=%s | required=%s",
            len(previous_points),
            min_history_weeks,
        )
        return 0

    mean_open_interest = calculate_mean([point.open_interest for point in previous_points])
    mean_notional_btc = calculate_mean([point.notional_btc for point in previous_points])
    mean_threshold = mean_open_interest * mean_multiplier

    try:
        btc_price, btc_price_source = get_btc_usd_price()
    except BitmexAPIError:
        logger.exception("Failed to fetch BTCUSD price for CFTC BTC OI notional conversion.")
        return 1

    current_notional_btc = latest.notional_btc
    current_notional_usd = current_notional_btc * btc_price
    reasons: list[str] = []
    if latest.open_interest > mean_threshold:
        reasons.append(
            f"当前 BTC 名义持仓 > {lookback_weeks}周均值 x {mean_multiplier:.2f}"
        )
    if contract_threshold is not None and latest.open_interest >= contract_threshold:
        reasons.append(f"当前 OI 张数 >= 固定阈值 {format_int(contract_threshold)} 张")
    if btc_threshold is not None and current_notional_btc >= btc_threshold:
        reasons.append(f"当前 BTC 名义持仓 >= 固定阈值 {format_int(btc_threshold)} BTC")
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

    if not reasons:
        logger.info("No CFTC BTC OI alert conditions met.")
        return 0

    alert_key = latest.report_date.isoformat()
    last_alert_key = get_last_alert_key(state, MONITOR_CFTC_BTC_OI)
    if last_alert_key == alert_key and not args.ignore_state:
        logger.info("CFTC BTC OI alert already sent for report date=%s. Skipping.", alert_key)
        return 0

    message = build_cftc_oi_message(
        latest=latest,
        lookback_weeks=lookback_weeks,
        mean_open_interest=mean_open_interest,
        mean_notional_btc=mean_notional_btc,
        multiplier=mean_multiplier,
        btc_price=btc_price,
        btc_price_source=btc_price_source,
        contract_threshold=contract_threshold,
        btc_threshold=btc_threshold,
        usd_threshold=usd_threshold,
        reasons=reasons,
        utc_now=utc_now,
        beijing_now=beijing_now,
    )

    return send_alert(
        message=message,
        args=args,
        state=state,
        state_file=state_file,
        logger=logger,
        monitor=MONITOR_CFTC_BTC_OI,
        alert_key=alert_key,
        utc_now=utc_now,
        beijing_now=beijing_now,
        metadata={
            "report_date": alert_key,
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
            "mean_multiplier": mean_multiplier,
            "contract_threshold": contract_threshold,
            "btc_threshold": btc_threshold,
            "usd_threshold": usd_threshold,
            "reasons": reasons,
        },
    )


def main() -> int:
    args = parse_args()
    load_dotenv()

    log_file = Path(os.getenv("LOG_FILE", "logs/btc_vol_alert.log"))
    state_file = Path(os.getenv("STATE_FILE", "state.json"))
    logger = setup_logger(log_file)

    utc_now, beijing_now = get_dual_now()
    logger.info(
        "Starting BTC alert checks | monitor=%s | UTC=%s | Beijing=%s | dry_run=%s",
        args.monitor,
        format_dt(utc_now),
        format_dt(beijing_now),
        args.dry_run,
    )

    state = read_state(state_file, logger)
    exit_codes: list[int] = []

    if args.monitor in {"all", "bvol"}:
        exit_codes.append(
            run_bvol_monitor(
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
            run_cftc_btc_oi_monitor(
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
