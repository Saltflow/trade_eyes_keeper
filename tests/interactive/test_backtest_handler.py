"""Offline handler tests preserve production date ranges without live providers."""

from types import SimpleNamespace

import pandas as pd

from src.interactive.commands import handlers


def test_backtest_returns_error_for_invalid_code(monkeypatch):
    monkeypatch.setattr(handlers, "_load_config", lambda: {})
    monkeypatch.setattr(handlers, "_get_backtest_strategy", lambda *args: None)
    result = handlers.handle_backtest("ZZZZZZ", "2024-01-01", "2024-09-30")
    assert "❌" in result
    assert "没有已激活的回测策略" in result


def test_backtest_with_controlled_nine_month_data(monkeypatch):
    import src.backtest.engine as engine
    import src.data.data_source as sources
    import src.search.config as search_config

    dates = pd.bdate_range("2024-01-02", "2024-12-31")
    history = pd.DataFrame({"date": dates, "close": 10.0})
    monkeypatch.setattr(handlers, "_load_config", lambda: {})
    monkeypatch.setattr(handlers, "_get_backtest_strategy", lambda *args: object())
    monkeypatch.setattr(handlers, "_get_backtest_params", lambda *args: (object(), False))
    execution = object()
    monkeypatch.setattr(search_config, "get_market_optimizer_config", lambda *args, **kwargs: SimpleNamespace(execution=execution, constraints=object()))
    monkeypatch.setattr(sources.DataSource, "__init__", lambda self, config: None)
    monkeypatch.setattr(sources.DataSource, "fetch_stock_data", lambda *args, **kwargs: history)
    seen = []

    def evaluate(*args, **kwargs):
        seen.append((args, kwargs))
        return {"a_share": SimpleNamespace(
            strategy_label="controlled", engine_name="unified", total_return=1.0,
            excess_return=0.5, max_drawdown=-1.0, sharpe_ratio=0.2,
            trade_count=1, composition=["601728"], timestamp="2024-09-30T16:00:00",
        )}

    monkeypatch.setattr(engine, "evaluate_all_groups", evaluate)
    result = handlers.handle_backtest("601728", "2024-01-01", "2024-09-30")
    assert "回测报告" in result
    assert "2024-01-01 ~ 2024-09-30" in result
    assert len(seen) == 1
    assert seen[0][0][4] is execution
    assert seen[0][1]["start_date"] == "2024-01-01"
    assert seen[0][1]["end_date"] == "2024-09-30"
