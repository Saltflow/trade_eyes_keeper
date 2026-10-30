from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import INDICATOR_NAMES, FastEvaluator
from src.backtest.execution import CorporateActionSlice
from src.fundamental_embedding.api import FundamentalPricingDataset
from src.search.config import StrategyConstraints
from src.search.contracts import GateDecision, ParameterSchema
from src.search.validation import ValidationController
from src.search.workflow import _evaluate_params_wf, _prepare_wf_evaluation_contexts
from src.strategy import Params, TradePlan
from src.strategy.capm_dcf_value_context import (
    CapmDcfValueContextConfig,
    CapmDcfValueContextEnricher,
    CapmDcfValuePolicy,
    CapmDcfValueSnapshot,
)
from src.strategy.context_enrichment import make_historical_dataset_enricher


class _RoundTripStrategy:
    name = "execution_contract_test"
    window_state_scope = "continuous"

    def make_signals(self, params, market):
        shape = market.prices.shape
        buys = np.zeros(shape, dtype=bool)
        sells = np.zeros(shape, dtype=bool)
        buys[0] = True
        sells[-1] = True
        priorities = np.tile(
            [float(int(code)) for code in market.symbols], (shape[0], 1)
        )
        return TradePlan(
            buy_signals=buys,
            sell_signals=sells,
            buy_priority=priorities,
            sell_priority=priorities,
            buy_cash_limit=1000,
            sell_cash_limit=10_000,
            warmup_rows=0,
            dates=list(market.dates),
            symbols=list(market.symbols),
            execution={"model": "cash_cap"},
        )


@pytest.fixture
def execution_case():
    raw = np.array(
        [[10, 20], [10, 20], [9, 19], [4.5, 19], [5, 20], [5.5, 21], [6, 22], [6, 22]],
        dtype=np.float32,
    )
    qfq = raw * np.array([0.5, 0.8], dtype=np.float32)
    gross = np.zeros_like(raw)
    gross[2] = [1.0, 0.5]
    multipliers = np.ones_like(raw)
    multipliers[3, 0] = 2.0
    actions = CorporateActionSlice(gross * 0.8, multipliers, gross, gross * 0.2)
    benchmark_gross = np.zeros((len(raw), 1))
    benchmark_gross[2] = 1.0
    benchmark_multipliers = np.ones_like(benchmark_gross)
    benchmark_multipliers[3] = 1.25
    benchmark_actions = CorporateActionSlice(
        benchmark_gross * 0.8,
        benchmark_multipliers,
        benchmark_gross,
        benchmark_gross * 0.2,
    )
    manager = SimpleNamespace(
        indicator_matrix=np.zeros((*raw.shape, len(INDICATOR_NAMES))),
        price_matrix=qfq,
        price_open_matrix=qfq.copy(),
        price_high_matrix=qfq + 0.2,
        price_low_matrix=qfq - 0.2,
        raw_price_matrix=raw,
        raw_price_high_matrix=raw + 0.5,
        raw_price_low_matrix=raw - 0.5,
        execution_tradable_matrix=np.ones_like(raw, dtype=bool),
        action_schedule=actions,
        stock_codes=["000001", "000002"],
        dates=pd.date_range("2026-01-05", periods=len(raw), freq="B"),
        benchmark_series={
            code: np.full(len(raw), 50.0) for code in ("510300", "510880")
        },
        benchmark_high_series={
            code: np.full(len(raw), 51.0) for code in ("510300", "510880")
        },
        benchmark_low_series={
            code: np.full(len(raw), 49.0) for code in ("510300", "510880")
        },
        benchmark_action_schedules={
            code: benchmark_actions for code in ("510300", "510880")
        },
        market_group="a_share",
        market_data_enricher=None,
    )
    constraints = StrategyConstraints(
        {
            "benchmarks": {
                "a_share": ["510300", "510880", "risk_free"],
                "risk_free_rates": {"a_share": 0.0},
            },
            "execution_params": {
                "initial_capital": 10_000,
                "commission_rate": 0,
                "min_holding_days": 0,
                "lot_sizes": {"a_share": 1},
                "fx_rates": {"a_share": 1.0},
                "withholding_rates": {"a_share": 0.2},
            },
            "validation": {
                "universe_robustness": {"minimum_mean_majority_excess": -1000}
            },
        }
    )
    constraints.set_group("a_share")
    evaluator = FastEvaluator(constraints.execution, "a_share")
    window = SimpleNamespace(train_start=0, test_start=0, test_end=len(raw))
    validator = ValidationController(
        _RoundTripStrategy(),
        constraints,
        manager,
        evaluator,
        ParameterSchema(()),
        SimpleNamespace(),
        SimpleNamespace(evaluate=lambda _: GateDecision(True)),
        [],
    )
    validator.ranking_windows = [window]
    return validator, window


def _contexts(validator, window, manager):
    return _prepare_wf_evaluation_contexts(
        [window], "continuous", validator.constraints, validator.evaluator, manager
    )


@pytest.mark.parametrize("indexes", [[1, 0], [0], [1]])
def test_subset_context_keeps_raw_ohlc_all_actions_and_benchmarks(
    execution_case, indexes
):
    validator, window = execution_case
    original = validator.wf_manager
    baseline = _contexts(validator, window, original)
    subset = validator._subset_manager(indexes)
    prepared = _contexts(validator, window, subset)
    execution = prepared["windows"][0]["execution_prices"]
    expected = baseline["windows"][0]["execution_prices"]

    assert prepared["full_market_data"].symbols == [
        original.stock_codes[index] for index in indexes
    ]
    assert prepared is not baseline
    for name in ("valuation_prices", "buy_prices", "sell_prices", "tradable"):
        np.testing.assert_allclose(
            getattr(execution, name), getattr(expected, name)[:, indexes]
        )
    for name in (
        "cash_dividends",
        "gross_cash_dividends",
        "dividend_tax_costs",
        "share_multipliers",
    ):
        np.testing.assert_array_equal(
            getattr(execution.corporate_actions, name),
            getattr(expected.corporate_actions, name)[:, indexes],
        )
    for code in ("510300", "510880"):
        np.testing.assert_array_equal(
            subset.benchmark_low_series[code], original.benchmark_low_series[code]
        )
        for name in (
            "cash_dividends",
            "gross_cash_dividends",
            "dividend_tax_costs",
            "share_multipliers",
        ):
            np.testing.assert_array_equal(
                getattr(subset.benchmark_action_schedules[code], name),
                getattr(original.benchmark_action_schedules[code], name),
            )
        np.testing.assert_array_equal(
            prepared["windows"][0]["benchmark_series"][code],
            baseline["windows"][0]["benchmark_series"][code],
        )
    assert subset._evaluation_context_cache is not original._evaluation_context_cache


def test_real_order_and_leave_one_evaluations_use_the_same_execution_contract(
    execution_case,
):
    validator, window = execution_case
    base = _evaluate_params_wf(
        Params(values={}),
        validator.strategy,
        [window],
        validator.constraints,
        validator.evaluator,
        validator.wf_manager,
        validation_window_count=0,
    )
    selected = validator._base_result(
        SimpleNamespace(
            candidate_id="base",
            parameters={},
            ranking_stats=base[1],
            objective_score=base[3],
            selection_score=base[3],
            ranking_metrics={},
            gate_results=(),
        )
    )

    result = validator._universe_robustness(selected)

    assert base[1][0].gross_dividend_cash > 0
    assert base[1][0].dividend_tax_cost > 0
    assert result["symbol_order_invariant"] is True
    assert result["variant_count"] == 2
    assert result["passed"] is True
    assert {item["removed"] for item in result["variants"]} == {"000001", "000002"}
    assert all(np.isfinite(item["mean_majority_excess"]) for item in result["variants"])


@pytest.mark.parametrize("field", ["raw_price_matrix", "action_schedule"])
def test_missing_execution_contract_is_rejected_before_subset_validation(
    execution_case, field
):
    validator, _ = execution_case
    delattr(validator.wf_manager, field)

    with pytest.raises(ValueError, match="validation requires"):
        validator._subset_manager([1])


def _historical_enricher():
    values = np.array([[10, 100], [20, 200], [11, 110], [999, 999]])
    return make_historical_dataset_enricher(
        FundamentalPricingDataset(
            feature_dates=(
                date(2026, 1, 6),
                date(2026, 1, 7),
                date(2026, 1, 12),
                date(2026, 2, 5),
            ),
            label_end_dates=(date(2026, 4, 1),) * 4,
            symbols=("000001", "000002", "000001", "000001"),
            feature_names=("earnings_yield", "roe_ttm"),
            values=values,
            availability_mask=np.array(
                [[True, True], [True, False], [True, True], [True, True]]
            ),
            forward_returns=np.zeros(4),
            excess_returns=np.zeros(4),
            metadata={"contract": "quarterly-fundamental-pricing-data-1"},
        )
    )


def _capm_enricher():
    return CapmDcfValueContextEnricher(
        snapshots=tuple(
            CapmDcfValueSnapshot(
                feature_date=feature_date,
                symbol=symbol,
                fair_price=fair_price,
                entry_price=fair_price * 0.75,
                entry_fraction=0.75,
                company_beta=0.6,
                pool_beta=0.8,
                valuation_model="fcfe_dcf",
            )
            for feature_date, symbol, fair_price in (
                (date(2026, 1, 6), "000001", 30.0),
                (date(2026, 1, 7), "000002", 40.0),
                (date(2026, 1, 12), "000001", 35.0),
                (date(2026, 2, 5), "000001", 999.0),
            )
        ),
        policy=CapmDcfValuePolicy(
            parameters={},
            available_from=date(2026, 1, 1),
            beta_reference=0.8,
            beta_reference_method="equal_episode_mean_point_in_time_beta",
            source_report="test-frozen-policy.json",
            source_hash="a" * 64,
        ),
        config=CapmDcfValueContextConfig(),
        skipped={},
        price_scales={
            "000001": ((date(2026, 1, 5), 0.5), (date(2026, 1, 8), 0.25)),
            "000002": ((date(2026, 1, 5), 0.8),),
        },
    )


@pytest.mark.parametrize("indexes", [[1, 0], [0], [1]])
@pytest.mark.parametrize("state_scope", ["continuous", "train"])
@pytest.mark.parametrize("enricher_factory", [_historical_enricher, _capm_enricher])
def test_subset_rebuilds_causal_context_in_symbol_and_window_order(
    execution_case, indexes, state_scope, enricher_factory
):
    validator, _ = execution_case
    original = validator.wf_manager
    original.market_data_enricher = enricher_factory()
    window = SimpleNamespace(train_start=1, test_start=3, test_end=7)
    baseline = _prepare_wf_evaluation_contexts(
        [window], state_scope, validator.constraints, validator.evaluator, original
    )
    subset = validator._subset_manager(indexes)
    prepared = _prepare_wf_evaluation_contexts(
        [window], state_scope, validator.constraints, validator.evaluator, subset
    )

    assert subset.market_data_enricher is original.market_data_enricher
    assert prepared is not baseline
    full = baseline["full_market_data"]
    # The fixtures include unpublished rows, symbol-specific masks, revisions,
    # and CAPM raw-to-signal price factors. They must survive cache rebuilding.
    assert not full.fundamental_availability_mask[0].any()
    assert full.fundamental_as_of_dates[-1, 0] == date(2026, 1, 12)
    assert full.fundamental_as_of_dates[-1, 1] == date(2026, 1, 7)
    for actual, expected in (
        (prepared["full_market_data"], full),
        (
            prepared["windows"][0]["signal_market_data"],
            baseline["windows"][0]["signal_market_data"],
        ),
    ):
        assert actual.symbols == [original.stock_codes[index] for index in indexes]
        assert actual.dates == expected.dates
        assert actual.fundamental_feature_names == expected.fundamental_feature_names
        for name in (
            "prices",
            "observation_counts",
            "fundamental_features",
            "fundamental_availability_mask",
            "fundamental_as_of_dates",
        ):
            np.testing.assert_array_equal(
                getattr(actual, name), getattr(expected, name)[:, indexes]
            )
        actual.require_historical_walk_forward_eligibility()
    signal = prepared["windows"][0]["signal_market_data"]
    expected_dates = full.dates[1:7] if state_scope == "train" else full.dates
    assert signal.dates == expected_dates
