"""回测外层补数和窗口报告合同。"""

from datetime import date

import numpy as np
import pandas as pd

from src.data.backtest_data import prepare_backtest_data
from src.data.market_history import CorporateAction, PriceHistoryBundle
from src.search.reporting import render_optimizer_report


def _bundle(code="600000"):
    dates = pd.date_range("2020-01-01", periods=3, freq="D")
    raw = np.array([10.0, 8.0, 8.5])
    factor = np.array([1.0, 1.0, 1.0])
    return PriceHistoryBundle(
        code=code,
        source="fake-yahoo",
        currency="CNY",
        prices=pd.DataFrame(
            {
                "date": dates,
                "raw_open": raw,
                "raw_high": raw + 0.5,
                "raw_low": raw - 0.5,
                "raw_close": raw,
                "qfq_open": raw,
                "qfq_high": raw + 0.5,
                "qfq_low": raw - 0.5,
                "qfq_close": raw,
                "qfq_factor": factor,
                "volume": 1000,
                "tradable": True,
            }
        ),
        actions=[
            CorporateAction(
                code=code,
                action_type="cash_dividend",
                ex_date=date(2020, 1, 2),
                cash_per_share=0.2,
                source="fake-yahoo",
            )
        ],
    ).validate()


def test_prepare_backtest_data_fetches_missing_strict_bundle(tmp_path, monkeypatch):
    bundle = _bundle()

    class FakeProvider:
        def __init__(self, _config):
            pass

        def fetch(self, code, _start, _end):
            assert code == "600000"
            return bundle

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", FakeProvider)
    result = prepare_backtest_data(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        ["600000"],
        "2020-01-01",
        "2020-01-03",
        purpose="test",
    )

    assert result.ready
    assert result.fetched_codes == ["600000"]
    assert result.bundles["600000"].prices["qfq_close"].tolist() == [10.0, 8.0, 8.5]
    assert (tmp_path / "market" / "600000.csv").is_file()


def test_prepare_backtest_data_fails_closed_without_legacy_fallback(tmp_path, monkeypatch):
    class BrokenProvider:
        def __init__(self, _config):
            pass

        def fetch(self, _code, _start, _end):
            raise RuntimeError("source unavailable")

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", BrokenProvider)
    result = prepare_backtest_data(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        ["600000"],
        "2020-01-01",
        "2020-01-03",
        purpose="test",
    )

    assert not result.ready
    assert result.issues[0].code == "600000"
    assert "source unavailable" in result.issues[0].reason
    assert not list(tmp_path.rglob("*.csv"))


def test_fundamental_dependency_failure_is_not_silently_accepted(tmp_path, monkeypatch):
    bundle = _bundle()

    class FakeProvider:
        def __init__(self, _config):
            pass

        def fetch(self, _code, _start, _end):
            return bundle

    class FakeBackfill:
        def __init__(self, _config):
            pass

        def run(self, *, codes, evaluation_date):
            assert codes == ["600000"]
            assert evaluation_date == date(2020, 1, 3)
            return {
                "instruments": [
                    {
                        "code": "600000",
                        "statements": {
                            "status": "missing",
                            "reason": "no dated statement",
                        },
                    }
                ]
            }

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", FakeProvider)
    monkeypatch.setattr(
        "src.data.point_in_time_backfill.PointInTimeBackfillService",
        FakeBackfill,
    )
    strategy = type(
        "FundamentalStrategy",
        (),
        {"fundamental_feature_dependencies": ("valuation:quality",)},
    )()

    result = prepare_backtest_data(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        ["600000"],
        "2020-01-01",
        "2020-01-03",
        purpose="test",
        strategy=strategy,
    )

    assert not result.ready
    assert result.issues[-1].source == "fundamental"
    assert "no dated statement" in result.issues[-1].reason


def test_optimizer_report_exposes_all_roles_and_holdout_aggregate():
    report = render_optimizer_report(
        {
            "group": "a_share",
            "strategy_id": "percentile",
            "timestamp": "2026-09-03T19:00:00",
            "solver_id": "local_genetic",
            "parameter_schema": "parameter-space/1",
            "control_benchmarks": ["510880", "510300", "risk_free"],
            "params": {"buy_cash_tier": 2},
            "activation": {"eligible": False},
            "holdout_summary": {
                "return_pct": 4.0,
                "excess_return_pct": 1.5,
                "max_drawdown_pct": -8.0,
                "sharpe_ratio": 0.7,
            },
            "search": {
                "total_window_count": 22,
                "ranking_window_count": 16,
                "purged_overlap_window_count": 2,
                "validation_window_count": 4,
                "time_contract": {
                    "total_months": 84,
                    "state_lookback_months": 12,
                    "holdout_window_count": 4,
                    "holdout_window_months": 9,
                    "holdout_test_months": 9,
                },
            },
            "ranking_windows": [
                {
                    "role": "ranking",
                    "role_index": 1,
                    "global_index": 1,
                    "period": {
                        "train_start": "2019-01-01",
                        "train_end": "2020-01-01",
                        "test_start": "2020-01-02",
                        "test_end": "2020-10-01",
                    },
                    "return": 2.0,
                    "excess_return": 1.0,
                    "max_drawdown": -3.0,
                    "sharpe_ratio": 0.4,
                    "trade_count": 2,
                }
            ],
            "purged_windows": [
                {"role": "purged", "role_index": 1, "global_index": 17}
            ],
            "holdout_windows": [
                {"role": "holdout", "role_index": 1, "global_index": 19}
            ],
        }
    )

    assert "22" in report
    assert "16" in report
    assert "Purged" in report
    assert "Holdout" in report
    assert "Holdout 4 × 9 个月窗口" in report
    assert "2019-01-01" in report
    assert "4.00%" in report
    assert "1.50%" in report
    assert "12-24月" not in report
    assert "2 年历史" not in report
