from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_STATE_FILE = "/var/lib/btc-vol-alert/state.json"
MONITOR_NAMES = {
    "bitmex_bvol": "BitMEX .BVOL7D",
    "cftc_btc_oi": "CFTC/CME BTC 持仓",
    "usdt_supply": "USDT 发行总量",
}
STALE_HOURS = {
    "bitmex_bvol": 36,
    "cftc_btc_oi": 21 * 24,
    "usdt_supply": 36,
}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_dt(value: str | None) -> str:
    parsed = parse_dt(value)
    if parsed is None:
        return "N/A"
    beijing = parsed.astimezone(BEIJING_TZ)
    return f"UTC {parsed.isoformat(timespec='seconds')} / 北京 {beijing.isoformat(timespec='seconds')}"


def fmt_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def fmt_int(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


@st.cache_data(ttl=20)
def load_state(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.exists():
        return {"monitors": {}, "system": {"state_error": {"message": "state file missing"}}}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        return {"monitors": {}, "system": {"state_error": {"message": str(exc)}}}
    return data if isinstance(data, dict) else {"monitors": {}, "system": {}}


def monitor_status(monitor: str, entry: dict[str, Any], now_utc: datetime) -> tuple[str, str]:
    if entry.get("status") == "error":
        return "异常", "inverse"
    last_success = parse_dt(entry.get("last_success_utc"))
    if last_success is None:
        return "等待数据", "off"
    age_hours = (now_utc - last_success).total_seconds() / 3600
    if age_hours > STALE_HOURS.get(monitor, 48):
        return "数据过期", "inverse"
    return "正常", "normal"


def latest_run(monitors: dict[str, Any], key: str) -> str | None:
    values = [
        entry.get(key)
        for entry in monitors.values()
        if isinstance(entry, dict) and entry.get(key)
    ]
    return max(values) if values else None


def main() -> None:
    st.set_page_config(page_title="BTCAlert 状态", page_icon="BTC", layout="wide")
    state_file = os.getenv("STATUS_STATE_FILE") or os.getenv("STATE_FILE") or DEFAULT_STATE_FILE
    state = load_state(state_file)
    monitors = state.get("monitors", {})
    if not isinstance(monitors, dict):
        monitors = {}
    now_utc = datetime.now(timezone.utc)

    statuses = [
        monitor_status(monitor, entry, now_utc)[0]
        for monitor, entry in monitors.items()
        if isinstance(entry, dict)
    ]
    overall = "异常" if any(status in {"异常", "数据过期"} for status in statuses) else "正常"

    st.title("BTCAlert 状态页")
    top = st.columns(4)
    top[0].metric("总体状态", overall)
    top[1].metric("最后运行", fmt_dt(latest_run(monitors, "last_run_utc")))
    top[2].metric("最后成功", fmt_dt(latest_run(monitors, "last_success_utc")))
    top[3].metric("监控项", str(len(monitors)))

    state_error = state.get("system", {}).get("state_error") if isinstance(state.get("system"), dict) else None
    if state_error:
        st.error(f"状态文件异常：{state_error.get('message') or state_error}")

    for monitor in ("bitmex_bvol", "cftc_btc_oi", "usdt_supply"):
        entry = monitors.get(monitor, {})
        if not isinstance(entry, dict):
            entry = {}
        label, delta_color = monitor_status(monitor, entry, now_utc)
        st.subheader(MONITOR_NAMES.get(monitor, monitor))
        cols = st.columns(4)
        cols[0].metric("状态", label, delta_color=delta_color)
        cols[1].metric("当前值", fmt_number(entry.get("current_value")))
        cols[2].metric("数据日期", entry.get("data_date") or "N/A")
        cols[3].metric("连续失败", fmt_int(entry.get("consecutive_failures", 0)))

        detail_cols = st.columns(3)
        detail_cols[0].write("最后运行")
        detail_cols[0].caption(fmt_dt(entry.get("last_run_utc")))
        detail_cols[1].write("最后成功")
        detail_cols[1].caption(fmt_dt(entry.get("last_success_utc")))
        detail_cols[2].write("最近报警")
        detail_cols[2].caption(fmt_dt(entry.get("last_alert_utc")))

        if monitor == "bitmex_bvol":
            st.write(
                {
                    "昨日值": entry.get("previous_value"),
                    "日变化": entry.get("daily_change"),
                    "30日分位": entry.get("percentile_rank"),
                    "数据源": entry.get("source"),
                }
            )
        elif monitor == "cftc_btc_oi":
            st.write(
                {
                    "报告日期": entry.get("report_date"),
                    "OI 张数": entry.get("open_interest"),
                    "折合 BTC": entry.get("notional_btc"),
                    "折合美元": entry.get("notional_usd"),
                    "BTCUSD": entry.get("btc_price"),
                    "52周均值": entry.get("mean_open_interest"),
                }
            )
        elif monitor == "usdt_supply":
            st.write(
                {
                    "当前总量": entry.get("current_supply"),
                    "昨日总量": entry.get("previous_day_supply"),
                    "24h变化": entry.get("daily_change"),
                    "24h变化%": entry.get("daily_change_percent"),
                    "跌幅阈值%": entry.get("drop_threshold_percent"),
                }
            )

        last_error = entry.get("last_error")
        if isinstance(last_error, dict) and last_error.get("message"):
            st.error(f"最近错误：{last_error.get('message')}")

    st.subheader("最近报警")
    alert_rows = []
    for monitor, entry in monitors.items():
        if not isinstance(entry, dict) or not entry.get("last_alert_utc"):
            continue
        alert_rows.append(
            {
                "监控项": MONITOR_NAMES.get(monitor, monitor),
                "时间": fmt_dt(entry.get("last_alert_utc")),
                "Key": entry.get("last_alert_key"),
                "原因": entry.get("reasons"),
            }
        )
    if alert_rows:
        st.dataframe(alert_rows, use_container_width=True, hide_index=True)
    else:
        st.info("暂无报警记录。")


if __name__ == "__main__":
    main()
