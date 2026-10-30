import pandas as pd
import pytest
import yaml

from config import get_alerts_config
from src.core.condition_checker import ConditionChecker
from src.core.data_fetcher import StockDataFetcher
from src.data.technical_indicators import TechnicalIndicators


def _write_rules(path, name, window):
    path.write_text(
        yaml.safe_dump(
            {"anchors": [{"name": name, "type": "daily_ma", "window": window}]}
        ),
        encoding="utf-8",
    )


def test_custom_alert_rules_drive_both_indicators_and_condition_checks(tmp_path):
    path = tmp_path / "custom_alerts.yaml"
    _write_rules(path, "custom_ma2", 2)
    config = {
        "stocks": [],
        "alerts": {"enabled": True, "config_path": str(path)},
        "storage": {"cache_dir": str(tmp_path / "cache")},
    }
    fetcher = StockDataFetcher(config)
    checker = ConditionChecker(config)
    prices = pd.DataFrame(
        {"stock_code": ["000001"] * 2, "close": [100.0, 110.0], "low": [90.0] * 2}
    )

    computed = fetcher.technical_indicators.calculate_indicators(prices)
    alerts = checker.alert_processor.alert_engine.evaluate_stock(computed.iloc[-1])

    assert computed["custom_ma2"].iloc[-1] == 105.0
    assert "ma60" not in computed
    assert len(alerts) == 1
    assert alerts[0]["anchor_name"] == "custom_ma2"
    assert alerts[0]["anchor_value"] == 105.0


def test_rules_are_isolated_by_path_and_reloaded_on_the_next_load(tmp_path):
    first_path, second_path = tmp_path / "first.yaml", tmp_path / "second.yaml"
    _write_rules(first_path, "first", 2)
    _write_rules(second_path, "second", 3)
    first = get_alerts_config(first_path)
    second = get_alerts_config(second_path)
    _write_rules(first_path, "updated", 4)
    refreshed = get_alerts_config(first_path)

    assert first.anchors[0]["name"] == "first"
    assert second.anchors[0]["name"] == "second"
    assert refreshed.anchors[0]["name"] == "updated"


def test_default_alert_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    expected = get_alerts_config().to_dict()
    monkeypatch.chdir(tmp_path)

    assert get_alerts_config().to_dict() == expected
    assert TechnicalIndicators().config == expected


@pytest.mark.parametrize("kind", ["missing", "invalid"])
def test_explicit_bad_alert_rules_do_not_silently_switch_to_ma60(tmp_path, kind):
    path = tmp_path / "bad.yaml"
    if kind == "invalid":
        path.write_text("- not-a-rules-mapping\n", encoding="utf-8")
    expected_error = FileNotFoundError if kind == "missing" else ValueError
    config = {"alerts": {"enabled": True, "config_path": str(path)}}

    with pytest.raises(expected_error):
        StockDataFetcher(config)
    with pytest.raises(expected_error):
        ConditionChecker(config)
