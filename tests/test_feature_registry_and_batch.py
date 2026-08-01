from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from src.analysis.backtester import FastEvaluator
from src.analysis.feature_registry import TECHNICAL_FEATURES
from src.analysis.search_interface import Params, StrategyMarketData, TradePlan
from src.analysis.strategies import get_strategy
from src.analysis.strategies.builder.engine import SELL_BUILDER_NAMES


def _indicators(rows=300, columns=2):
    time = np.arange(rows, dtype=np.float32)[:, None]
    close = 20.0 + time * 0.02 + np.arange(columns, dtype=np.float32)[None, :]
    values = np.zeros((rows, columns, 22), dtype=np.float32)
    values[:, :, 0] = close
    values[:, :, 1] = close * 0.98
    values[:, :, 2] = (close - values[:, :, 1]) / values[:, :, 1]
    values[:, :, 3] = 45 + 10 * np.sin(time / 10)
    values[:, :, 4] = close * 0.01
    values[:, :, 5] = close * 0.008
    values[:, :, 6] = close * 0.002
    values[:, :, 7] = 1.2
    values[:, :, 8] = 0.4
    values[:, :, 9] = 25
    values[:, :, 10] = close * 0.02
    values[:, :, 11:16] = 0.5
    values[:, :, 16] = close * 1.01
    values[:, :, 17] = close * 0.99
    values[:, :, 18] = close * 0.95
    values[:, :, 19] = 0.01
    values[:, :, 20] = 30
    values[:, :, 21] = 20
    return values


def test_all_22_features_are_causal_and_price_scale_invariant():
    original = _indicators()
    transformed, mask = TECHNICAL_FEATURES.transform(original)
    assert transformed.shape == original.shape
    assert transformed.dtype == np.float32
    assert len(TECHNICAL_FEATURES.features) == 22
    assert all(feature.lookback_rows >= 1 for feature in TECHNICAL_FEATURES.features)
    assert all(
        feature.contract()["future_rows"] == 0
        for feature in TECHNICAL_FEATURES.features
    )

    future_changed = original.copy()
    future_changed[200:] *= 3
    causal, _ = TECHNICAL_FEATURES.transform(future_changed)
    np.testing.assert_allclose(transformed[:200], causal[:200], equal_nan=True)

    scaled = original.copy()
    for index in (0, 1, 4, 5, 6, 10, 16, 17, 18):
        scaled[:, :, index] *= 10
    scale_free, scale_mask = TECHNICAL_FEATURES.transform(scaled)
    np.testing.assert_allclose(transformed, scale_free, atol=2e-6, equal_nan=True)
    np.testing.assert_array_equal(mask, scale_mask)


def test_pullback_factor_directions_cannot_be_reversed_by_solver():
    directions = {item.name: item.direction for item in TECHNICAL_FEATURES.features}
    assert directions["rsi"] == -1
    assert directions["rsi_pct"] == -1
    assert directions["boll_pct_b"] == -1
    assert directions["plus_di"] == 1
    assert directions["minus_di"] == -1


def test_technical_ensemble_batch_plan_equals_scalar_and_ignores_fundamentals():
    strategy = get_strategy("technical_ensemble")
    assert strategy.fundamental_feature_dependencies == ()
    indicators = _indicators()
    market = StrategyMarketData(
        indicator_matrix=indicators,
        prices=indicators[:, :, 0],
        highs=indicators[:, :, 16],
        lows=indicators[:, :, 17],
        dates=[f"2025-01-{(index % 28) + 1:02d}" for index in range(len(indicators))],
        symbols=["A", "B"],
    )
    params = Params(
        values={
            dim.name: (1 if dim.name.startswith("weight_") else 0)
            for dim in strategy.param_space.dims
        },
        _engine=strategy.name,
    )
    scalar = strategy.evaluate_one(params, market)
    batched = strategy.prepare(market).evaluate_batch([params])[0]
    np.testing.assert_array_equal(scalar.buy_signals, batched.buy_signals)
    np.testing.assert_array_equal(scalar.sell_signals, batched.sell_signals)
    np.testing.assert_allclose(scalar.buy_priority, batched.buy_priority)
    np.testing.assert_allclose(scalar.sell_priority, batched.sell_priority)


def test_builder_sell_pool_is_explicit_and_contains_profit_taking():
    assert "deep_value" not in SELL_BUILDER_NAMES
    assert "sell_profit_taking" in SELL_BUILDER_NAMES


def _plan(buy_day: int, sell_day: int, rows: int = 40) -> TradePlan:
    buy = np.zeros((rows, 1), dtype=bool)
    sell = np.zeros((rows, 1), dtype=bool)
    buy[buy_day, 0] = True
    sell[sell_day, 0] = True
    return TradePlan(
        buy_signals=buy,
        sell_signals=sell,
        buy_priority=np.where(buy, 1.0, -np.inf).astype(np.float32),
        sell_priority=np.where(sell, 1.0, -np.inf).astype(np.float32),
        buy_cash_limit=20_000,
        sell_cash_limit=20_000,
        warmup_rows=0,
        execution={"model": "cash_cap"},
    )


def test_fast_evaluator_batch_is_identical_to_scalar_path():
    rows = 40
    close = np.linspace(10, 14, rows, dtype=np.float32)[:, None]
    indicators = np.zeros((rows, 1, 22), dtype=np.float32)
    indicators[:, :, 0] = close
    evaluator = FastEvaluator(
        SimpleNamespace(
            initial_capital=100_000,
            lot_sizes={"a_share": 100},
            commission_rate=0.005,
            min_holding_days=0,
            fx_rates={"a_share": 1.0},
        ),
        "a_share",
    )
    plans = [_plan(1, 20), _plan(5, 30)]
    inputs = {
        "indicator_matrix": indicators,
        "price_matrix": close,
        "cash_baseline": np.full(rows, 100_000.0),
    }
    scalar = [evaluator.evaluate(trade_plan=plan, **inputs) for plan in plans]
    batched = evaluator.evaluate_batch(plans, workers=2, **inputs)
    for left, right in zip(scalar, batched):
        assert (
            left.strategy_return,
            left.max_drawdown_pct,
            left.sharpe_ratio,
            left.total_trades,
            left.final_asset,
            left.final_cash,
        ) == (
            right.strategy_return,
            right.max_drawdown_pct,
            right.sharpe_ratio,
            right.total_trades,
            right.final_asset,
            right.final_cash,
        )
        np.testing.assert_array_equal(left.final_shares, right.final_shares)
