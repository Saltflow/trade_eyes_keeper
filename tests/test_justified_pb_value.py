"""Contract tests for the fixed-rule justified-PB value strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy import Params, StrategyMarketData, get_strategy, list_strategy_ids
from src.strategy.fundamental_context import FundamentalStrategyMarketData

STRATEGY_ID = "justified_pb_value"


def _params() -> Params:
    return Params(values={})


def _market(
    periods: int = 6,
    symbols: int = 2,
    roe: float | None = 10.0,
    pb: float | None = 0.9,
    feature_mask: bool = True,
    source_dates: list[object] | None = None,
) -> FundamentalStrategyMarketData:
    dates = pd.bdate_range("2026-01-02", periods=periods).strftime("%Y-%m-%d").tolist()
    prices = np.full((periods, symbols), 10.0, dtype=np.float32)
    feats = np.full((periods, symbols, 2), np.nan, dtype=np.float64)
    mask = np.zeros((periods, symbols, 2), dtype=bool)
    as_of = np.full((periods, symbols), None, dtype=object)
    if feature_mask:
        for column in range(symbols):
            feats[:, column, 0] = roe
            feats[:, column, 1] = 1.0 / pb if pb else 1.0
            mask[:, column, :] = True
    default_source = "2026-01-02"
    for r in range(periods):
        for c in range(symbols):
            if source_dates is None:
                as_of[r, c] = default_source
            else:
                index = min(r, len(source_dates) - 1)
                as_of[r, c] = source_dates[index]
    return FundamentalStrategyMarketData(
        indicator_matrix=np.zeros((periods, symbols, 8), dtype=np.float32),
        dates=dates,
        symbols=[f"S{column}" for column in range(symbols)],
        prices=prices,
        tradable=np.ones((periods, symbols), dtype=bool),
        observation_counts=np.tile(
            np.arange(1, periods + 1, dtype=np.int32)[:, None], (1, symbols)
        ),
        fundamental_features=feats,
        fundamental_availability_mask=mask,
        fundamental_feature_names=("roe_ttm", "book_yield"),
        fundamental_feature_contract="historical-fundamental-panel-1",
        fundamental_as_of_dates=as_of,
        fundamental_historical_walk_forward_eligible=True,
    )


def test_registered_with_a_share_metadata_and_no_search_dims():
    assert STRATEGY_ID in list_strategy_ids()
    strategy = get_strategy(STRATEGY_ID)
    assert strategy is not None
    assert strategy.supported_markets == ("a_share",)
    assert strategy.param_space.dims == []
    assert strategy.warmup_rows == 1
    assert strategy.fundamental_feature_dependencies == ("roe_ttm", "book_yield")


def test_buys_below_justified_pb_ke_10():
    # ROE=10% -> j1=(10-2)/8=1.00; pb=0.9 < 1.0 -> one-shot entry on first row.
    strategy = get_strategy(STRATEGY_ID)
    plan = strategy.make_signals(_params(), _market())
    assert np.array_equal(plan.buy_signals[:, 0], [True] + [False] * 5)
    assert not plan.sell_signals.any()
    assert np.allclose(plan.target_weights, 0.20)
    assert plan.execution["model"] == "target_weight"
    assert plan.execution["per_symbol_cap"] == 0.20
    assert plan.execution["total_exposure_cap"] == 1.0
    assert plan.strategy_metadata["ke_buy"] == 0.10
    assert plan.strategy_metadata["ke_sell"] == 0.06
    assert plan.strategy_metadata["growth"] == 0.02


def test_sells_above_justified_pb_ke_6():
    # ROE=10% -> j2=(10-2)/4=2.00; pb=2.1 > 2.0 -> exits active on every row.
    strategy = get_strategy(STRATEGY_ID)
    plan = strategy.make_signals(_params(), _market(pb=2.1))
    assert plan.sell_signals[:, 0].all()
    assert not plan.buy_signals.any()


def test_holds_between_thresholds():
    # pb=1.5 -> between j1=1.0 and j2=2.0: no entry, no exit.
    strategy = get_strategy(STRATEGY_ID)
    plan = strategy.make_signals(_params(), _market(pb=1.5))
    assert not plan.buy_signals.any()
    assert not plan.sell_signals.any()


def test_roe_below_growth_never_buys():
    for roe in (1.0, 2.0):
        strategy = get_strategy(STRATEGY_ID)
        # pb tiny (0.4) would satisfy the band if ROE were meaningful.
        plan = strategy.make_signals(_params(), _market(roe=roe, pb=0.4))
        assert not plan.buy_signals.any()


def test_missing_roe_or_book_yield_excluded():
    # Unavailable fundamentals (or non-positive book equity) carry no signals.
    strategy = get_strategy(STRATEGY_ID)
    panel = _market(feature_mask=False)
    panel.fundamental_availability_mask[:, :, :] = False
    plan = strategy.make_signals(_params(), panel)
    assert not plan.buy_signals.any()
    assert not plan.sell_signals.any()


def test_one_shot_entry_per_quarterly_source():
    strategy = get_strategy(STRATEGY_ID)
    # Quarterly refresh at row 3 -> entries only at the first row and row 3.
    panel = _market(
        periods=6,
        symbols=1,
        source_dates=[
            "2026-01-02", "2026-01-02", "2026-01-02",
            "2026-04-01", "2026-04-01", "2026-04-01",
        ],
    )
    plan = strategy.make_signals(_params(), panel)
    assert np.array_equal(
        plan.buy_signals[:, 0], [True, False, False, True, False, False]
    )


def test_downward_cross_into_band_triggers_entry():
    strategy = get_strategy(STRATEGY_ID)
    panel = _market(periods=4, symbols=1, pb=1.5)
    # Day 2 price cross: pb from 1.5 (hold band) to 0.9 (buy band).
    panel.prices[2:, 0] = 10.0 * 0.9 / 1.5  # keep absolute PB path simple via features
    # Simulate the PB drop by changing book yield (same source, no as-of change).
    panel.fundamental_features[:2, 0, 1] = 1.0 / 1.5
    panel.fundamental_features[2:, 0, 1] = 1.0 / 0.9
    plan = strategy.make_signals(_params(), panel)
    assert np.array_equal(
        plan.buy_signals[:, 0], [False, False, True, False]
    )


def test_entry_and_exit_never_overlap():
    strategy = get_strategy(STRATEGY_ID)
    panel = _market(periods=6, symbols=1, roe=10.0, pb=1.5)
    panel.fundamental_features[:3, 0, 1] = 1.0 / 0.9
    panel.fundamental_features[3:, 0, 1] = 1.0 / 2.1
    panel.fundamental_as_of_dates[:] = "2026-01-02"
    plan = strategy.make_signals(_params(), panel)
    assert not np.any(plan.buy_signals & plan.sell_signals)
    # Exit band rows produce exits; entry only from the pre-band crossing.
    assert plan.sell_signals[3:, 0].all()


def test_requires_fundamental_panel():
    strategy = get_strategy(STRATEGY_ID)
    plain = StrategyMarketData(
        indicator_matrix=np.zeros((6, 2, 8), dtype=np.float32),
        dates=pd.bdate_range("2026-01-02", periods=6).strftime("%Y-%m-%d").tolist(),
        symbols=["S0", "S1"],
        prices=np.full((6, 2), 10.0, dtype=np.float32),
        tradable=np.ones((6, 2), dtype=bool),
    )
    with pytest.raises(TypeError, match="historical fundamental panel"):
        strategy.make_signals(_params(), plain)


def test_a_share_only_and_fail_closed_context():
    strategy = get_strategy(STRATEGY_ID)
    assert not strategy.supports_market("hk")
    with pytest.raises(ValueError, match="A-share-only"):
        strategy.make_context_enricher(
            {"point_in_time_data": {"output_dir": "data/point_in_time"}},
            market="hk",
            symbols=("000333",),
        )
    with pytest.raises(ValueError, match="market store is missing"):
        strategy.make_context_enricher(
            {"point_in_time_data": {"output_dir": "no/such/dir"}},
            market="a_share",
            symbols=("000333",),
        )