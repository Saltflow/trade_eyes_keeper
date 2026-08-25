from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import INDICATOR_NAMES
from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.valuation_consensus_context import (
    VALUATION_CONSENSUS_CONTEXT_CONTRACT,
)
from src.search.config import StrategyConstraints
from src.search.workflow import _prepare_wf_evaluation_contexts
from src.strategy import StrategyMarketData, get_strategy
from src.strategy.context_enrichment import make_consensus_context_enricher
from src.strategy.fundamental_context import FundamentalStrategyMarketData
from src.strategy.valuation_context_kernel import (
    VALUATION_CONTEXT_QUALITY_NAME,
    VALUATION_CONTEXT_SIGNAL_NAMES,
)


def _dataset():
    names = (*VALUATION_CONTEXT_SIGNAL_NAMES, VALUATION_CONTEXT_QUALITY_NAME)
    feature_dates = (date(2025, 1, 1), date(2025, 7, 1))
    return FundamentalPricingDataset(
        feature_dates=feature_dates,
        label_end_dates=tuple(item + timedelta(days=30) for item in feature_dates),
        symbols=("000001", "000001"),
        feature_names=names,
        values=np.ones((2, len(names)), dtype=np.float64),
        availability_mask=np.ones((2, len(names)), dtype=bool),
        forward_returns=np.zeros(2),
        excess_returns=np.zeros(2),
        metadata={"contract": VALUATION_CONSENSUS_CONTEXT_CONTRACT},
    )


def _market(periods=320):
    dates = pd.bdate_range("2024-12-02", periods=periods)
    prices = np.full((periods, 1), 10.0, dtype=np.float32)
    return StrategyMarketData(
        indicator_matrix=np.ones((periods, 1, len(INDICATOR_NAMES)), dtype=np.float32),
        dates=dates.strftime("%Y-%m-%d").tolist(),
        symbols=["000001"],
        prices=prices,
        highs=prices,
        lows=prices,
        tradable=np.ones_like(prices, dtype=bool),
        date_ordinals=dates.values.astype("datetime64[D]").astype(np.int64),
        observation_counts=np.arange(1, periods + 1, dtype=np.int32).reshape(-1, 1),
    )


def test_enricher_is_causal_and_fingerprinted():
    enricher = make_consensus_context_enricher(_dataset())
    panel = enricher(_market())
    assert isinstance(panel, FundamentalStrategyMarketData)
    assert panel.fundamental_as_of_dates[0, 0] is None
    assert panel.fundamental_as_of_dates[-1, 0] == date(2025, 7, 1)
    assert enricher.contract == VALUATION_CONSENSUS_CONTEXT_CONTRACT
    assert len(enricher.contract_hash) == 64


def test_valuation_aware_strategy_requires_enriched_panel():
    strategy = get_strategy("valuation_aware_ensemble")
    params = strategy.sample_params()
    with pytest.raises(TypeError, match="historical fundamental panel"):
        strategy.make_signals(params, _market())
    panel = make_consensus_context_enricher(
        _dataset(), required_feature_names=strategy.fundamental_feature_dependencies
    )(_market())
    plan = strategy.make_signals(params, panel)
    assert plan.strategy_metadata["strategy_id"] == "valuation_aware_ensemble"
    assert plan.strategy_metadata["valuation_context_contract"] == (
        "historical-valuation-context-panel-1"
    )


def test_search_context_carries_enriched_panel_without_strategy_branch():
    strategy = get_strategy("valuation_aware_ensemble")
    enricher = make_consensus_context_enricher(
        _dataset(), required_feature_names=strategy.fundamental_feature_dependencies
    )
    market = _market(periods=12)
    manager = SimpleNamespace(
        dates=pd.to_datetime(market.dates),
        stock_codes=market.symbols,
        market_group="a_share",
        indicator_matrix=market.indicator_matrix,
        price_matrix=market.prices,
        price_high_matrix=market.highs,
        price_low_matrix=market.lows,
        benchmark_series={},
        benchmark_high_series={},
        market_data_enricher=enricher,
    )
    constraints = StrategyConstraints(
        {"benchmarks": {"a_share": ["risk_free"]}, "walk_forward": {}}
    )
    constraints.set_group("a_share")
    evaluator = SimpleNamespace(
        initial_cash=100_000.0,
        commission_rate=0.005,
        lot_size=100,
        fx_rate=1.0,
    )
    window = SimpleNamespace(train_start=0, train_end=8, test_start=8, test_end=12)
    prepared = _prepare_wf_evaluation_contexts(
        [window], "train", constraints, evaluator, manager, strategy=strategy
    )
    assert isinstance(prepared["full_market_data"], FundamentalStrategyMarketData)
    assert isinstance(
        prepared["windows"][0]["signal_market_data"], FundamentalStrategyMarketData
    )
