"""Contracts for the frozen-policy CAPM-DCF value strategy."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import build_trade_plan
from src.strategy import Params, StrategyMarketData, get_strategy
from src.strategy.capm_dcf_value_context import (
    CAPM_DCF_VALUE_FEATURE_NAMES,
    CapmDcfValueContextConfig,
    CapmDcfValueContextEnricher,
    CapmDcfValuePolicy,
    CapmDcfValueSnapshot,
    beta_adjusted_entry_fraction,
)


def _policy() -> CapmDcfValuePolicy:
    return CapmDcfValuePolicy(
        parameters={},
        available_from=date(2024, 1, 1),
        beta_reference=0.819,
        beta_reference_method="equal_episode_mean_point_in_time_beta",
        source_report="broad-policy.json",
        source_hash="a" * 64,
    )


def _market() -> StrategyMarketData:
    dates = pd.bdate_range("2025-01-02", periods=6)
    prices = np.asarray(
        [
            [10.0, 20.0],
            [11.0, 19.0],
            [12.0, 14.0],
            [13.0, 14.0],
            [21.0, 14.0],
            [20.0, 22.0],
        ],
        dtype=np.float32,
    )
    return StrategyMarketData(
        indicator_matrix=np.ones((6, 2, 1), dtype=np.float32),
        dates=dates.strftime("%Y-%m-%d").tolist(),
        symbols=["AAA", "BBB"],
        prices=prices,
        highs=prices,
        lows=prices,
        tradable=np.ones_like(prices, dtype=bool),
        observation_counts=np.tile(np.arange(1, 7, dtype=np.int32)[:, None], (1, 2)),
    )


def _enricher() -> CapmDcfValueContextEnricher:
    return CapmDcfValueContextEnricher(
        snapshots=(
            CapmDcfValueSnapshot(
                feature_date=date(2025, 1, 2),
                symbol="AAA",
                fair_price=20.0,
                entry_price=15.0,
                entry_fraction=0.75,
                company_beta=0.6,
                pool_beta=0.6,
                valuation_model="fcfe_dcf",
            ),
            CapmDcfValueSnapshot(
                feature_date=date(2025, 1, 2),
                symbol="BBB",
                fair_price=22.0,
                entry_price=15.0,
                entry_fraction=0.75,
                company_beta=0.6,
                pool_beta=0.6,
                valuation_model="fcfe_dcf",
            ),
            # This revision must be invisible throughout the short market test.
            CapmDcfValueSnapshot(
                feature_date=date(2025, 1, 15),
                symbol="AAA",
                fair_price=25.0,
                entry_price=20.0,
                entry_fraction=0.80,
                company_beta=0.6,
                pool_beta=0.6,
                valuation_model="fcfe_dcf",
            ),
        ),
        policy=_policy(),
        config=CapmDcfValueContextConfig(),
        skipped={},
        price_scales={
            "AAA": ((date(2025, 1, 2), 1.0),),
            "BBB": ((date(2025, 1, 2), 1.0),),
        },
    )


def _params() -> Params:
    return Params(values={"buy_cash_tier": 0, "sell_cash_tier": 0})


def test_low_beta_pool_can_relax_085_safety_margin_to_095():
    relaxed = beta_adjusted_entry_fraction(
        0.85,
        pool_beta=0.575,
        beta_reference=0.819,
        gamma=0.32,
        minimum=0.75,
        maximum=0.95,
    )
    assert relaxed == pytest.approx(0.95)
    tightened = beta_adjusted_entry_fraction(
        0.85,
        pool_beta=1.20,
        beta_reference=0.819,
        gamma=0.32,
        minimum=0.75,
        maximum=0.95,
    )
    assert 0.75 <= tightened < 0.85


def test_context_is_causal_and_has_stable_contract_hash():
    enricher = _enricher()
    panel = enricher(_market())
    assert panel.fundamental_feature_names == CAPM_DCF_VALUE_FEATURE_NAMES
    assert panel.fundamental_as_of_dates[3, 0] == date(2025, 1, 2)
    assert panel.fundamental_as_of_dates[4, 0] == date(2025, 1, 2)
    assert panel.fundamental_features[3, 0, 0] == 20.0
    assert panel.fundamental_features[4, 0, 0] == 20.0
    assert len(enricher.contract_hash) == 64


def test_context_scales_raw_dcf_threshold_into_adjusted_execution_prices():
    raw_policy = _policy()
    enricher = CapmDcfValueContextEnricher(
        snapshots=(
            CapmDcfValueSnapshot(
                feature_date=date(2025, 1, 2),
                symbol="AAA",
                fair_price=20.0,
                entry_price=15.0,
                entry_fraction=0.75,
                company_beta=0.6,
                pool_beta=0.6,
                valuation_model="fcfe_dcf",
            ),
        ),
        policy=raw_policy,
        config=CapmDcfValueContextConfig(),
        skipped={},
        price_scales={"AAA": ((date(2025, 1, 2), 0.5),)},
    )
    panel = enricher(_market())
    # raw 10 <= 15 and adjusted 5 <= 7.5 are the same investment decision.
    assert panel.fundamental_features[0, 0, 0] == pytest.approx(10.0)
    assert panel.fundamental_features[0, 0, 1] == pytest.approx(7.5)


def test_context_fails_closed_when_the_latest_filing_valuation_is_stale():
    enricher = CapmDcfValueContextEnricher(
        snapshots=(
            CapmDcfValueSnapshot(
                feature_date=date(2025, 1, 2),
                symbol="AAA",
                fair_price=20.0,
                entry_price=15.0,
                entry_fraction=0.75,
                company_beta=0.6,
                pool_beta=0.6,
                valuation_model="fcfe_dcf",
            ),
        ),
        policy=_policy(),
        config=CapmDcfValueContextConfig(maximum_snapshot_age_days=3),
        skipped={},
        price_scales={"AAA": ((date(2025, 1, 2), 1.0),)},
    )
    panel = enricher(_market())
    assert panel.fundamental_availability_mask[0, 0, 0]
    assert not panel.fundamental_availability_mask[4, 0, 0]


def test_value_strategy_buys_marketable_target_and_waits_for_pullback():
    strategy = get_strategy("capm_dcf_value")
    panel = _enricher()(_market())
    plan = strategy.make_signals(_params(), panel)

    # AAA is already below its 15 entry target at the first eligible snapshot.
    assert np.flatnonzero(plan.entry_events[:, 0]).tolist() == [0]
    # BBB first becomes affordable at 14 on the third session.
    assert np.flatnonzero(plan.entry_events[:, 1]).tolist() == [2]
    # Selling is a persistent executable decision while price is at/above fair.
    assert plan.exit_events[4, 0]
    assert plan.exit_events[5, 1]
    assert not np.any(plan.entry_events & plan.exit_events)
    assert plan.strategy_metadata["decision_contract"] == (
        "marketable_value_entry_or_downward_cross"
    )


def test_value_strategy_fails_closed_without_causal_value_context():
    strategy = get_strategy("capm_dcf_value")
    with pytest.raises(TypeError, match="historical value panel"):
        strategy.make_signals(_params(), _market())


def test_value_strategy_explicitly_limits_its_current_causal_market_coverage():
    strategy = get_strategy("capm_dcf_value")
    assert strategy.supports_market("a_share")
    assert not strategy.supports_market("hk")
    assert not strategy.supports_market("us")


def test_shared_plan_builder_applies_value_context_without_strategy_branch():
    dates = pd.bdate_range("2025-01-02", periods=6)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, 11.0, 12.0, 13.0, 21.0, 20.0],
            "high": [10.0, 11.0, 12.0, 13.0, 21.0, 20.0],
            "low": [10.0, 11.0, 12.0, 13.0, 21.0, 20.0],
            "close": [10.0, 11.0, 12.0, 13.0, 21.0, 20.0],
            "volume": [1_000] * 6,
        }
    )
    plan, _market_data, _prices, codes = build_trade_plan(
        {"AAA": frame},
        ["AAA"],
        get_strategy("capm_dcf_value"),
        _params(),
        context_enricher=_enricher(),
    )
    assert codes == ["AAA"]
    assert plan is not None
    assert plan.entry_events[0, 0]
