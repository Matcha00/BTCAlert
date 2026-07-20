from utils.indicators import evaluate_alerts


def test_bvol_high_and_low_thresholds() -> None:
    high = evaluate_alerts(
        current_value=15,
        previous_value=12,
        historical_values=[3, 5, 8, 12, 15],
        high_vol_warning_threshold=13,
        high_vol_alert_threshold=15,
        low_vol_low_threshold=4,
        low_vol_medium_threshold=3,
        low_vol_high_threshold=2,
    )
    assert high.should_alert
    assert high.level == "高级警报"
    assert high.regime == "高波动"
    assert high.daily_change == 3

    low = evaluate_alerts(
        current_value=2.5,
        previous_value=4,
        historical_values=[2, 2.5, 3, 4],
        high_vol_warning_threshold=13,
        high_vol_alert_threshold=15,
        low_vol_low_threshold=4,
        low_vol_medium_threshold=3,
        low_vol_high_threshold=2,
    )
    assert low.should_alert
    assert low.level == "中级警报"
    assert low.regime == "低波动"
