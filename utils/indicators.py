from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertEvaluation:
    should_alert: bool
    reasons: list[str]
    daily_change: float | None
    percentile_rank: float | None
    emoji: str | None = None
    level: str | None = None
    regime: str | None = None
    headline: str | None = None


def calculate_daily_change(
    current_value: float,
    previous_value: float | None,
) -> float | None:
    if previous_value is None:
        return None
    return current_value - previous_value


def calculate_percentile_rank(
    current_value: float,
    historical_values: list[float],
) -> float | None:
    if not historical_values:
        return None
    less_or_equal_count = sum(1 for value in historical_values if value <= current_value)
    return less_or_equal_count / len(historical_values) * 100


def evaluate_alerts(
    current_value: float,
    previous_value: float | None,
    historical_values: list[float],
    high_vol_warning_threshold: float,
    high_vol_alert_threshold: float,
    low_vol_low_threshold: float,
    low_vol_medium_threshold: float,
    low_vol_high_threshold: float,
) -> AlertEvaluation:
    reasons: list[str] = []
    emoji: str | None = None
    level: str | None = None
    regime: str | None = None
    headline: str | None = None

    if current_value >= high_vol_alert_threshold:
        emoji = "🔴"
        level = "高级警报"
        regime = "高波动"
        headline = "高波动已经确认，短期行情可能处于剧烈波动阶段。"
        reasons.append(f"{emoji} 高波动高级警报：.BVOL7D >= {high_vol_alert_threshold:g}")
    elif current_value >= high_vol_warning_threshold:
        emoji = "🟠"
        level = "预警"
        regime = "高波动"
        headline = "高波动预警，短期行情可能接近阶段性高点、低点或快速反转区间。"
        reasons.append(f"{emoji} 高波动预警：.BVOL7D >= {high_vol_warning_threshold:g}")
    elif current_value < low_vol_high_threshold:
        emoji = "🔴"
        level = "高级警报"
        regime = "低波动"
        headline = "低波动高级警报，波动率极低，后续出现方向性放大的概率上升。"
        reasons.append(f"{emoji} 低波动高级警报：.BVOL7D < {low_vol_high_threshold:g}")
    elif current_value < low_vol_medium_threshold:
        emoji = "🟡"
        level = "中级警报"
        regime = "低波动"
        headline = "低波动中级警报，行情可能进入压缩蓄势阶段。"
        reasons.append(f"{emoji} 低波动中级警报：.BVOL7D < {low_vol_medium_threshold:g}")
    elif current_value < low_vol_low_threshold:
        emoji = "🟢"
        level = "低级警报"
        regime = "低波动"
        headline = "低波动低级警报，波动率偏低，开始进入观察区。"
        reasons.append(f"{emoji} 低波动低级警报：.BVOL7D < {low_vol_low_threshold:g}")

    daily_change = calculate_daily_change(current_value, previous_value)
    percentile_rank = calculate_percentile_rank(current_value, historical_values)

    return AlertEvaluation(
        should_alert=bool(reasons),
        reasons=reasons,
        daily_change=daily_change,
        percentile_rank=percentile_rank,
        emoji=emoji,
        level=level,
        regime=regime,
        headline=headline,
    )
