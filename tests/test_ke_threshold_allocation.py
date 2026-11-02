"""Fixed Ke requests over synthetic, explicitly dated fundamental inputs."""

from copy import deepcopy
from unittest.mock import patch

import numpy as np
import pytest

from src.notification.report_policy import implied_ke
from src.strategy import Params, StrategyMarketData, get_strategy, list_strategy_ids
from src.strategy.fundamental_context import FundamentalStrategyMarketData
from src.strategy.plugins.ke_threshold_allocation import KE_PANEL_CONTRACT


def _panel(values=(0.10, 0.07, 0.05, 0.09), *, columns=2):
    rows = len(values)
    dates = (np.datetime64("2026-01-01") + np.arange(rows)).astype(str).tolist()
    ke = np.tile(np.asarray(values, dtype=np.float64)[:, None], (1, columns))
    return FundamentalStrategyMarketData(
        indicator_matrix=np.zeros((rows, columns, 0), dtype=np.float64),
        dates=dates,
        symbols=[f"S{column}" for column in range(columns)],
        prices=np.full((rows, columns), 10.0),
        tradable=np.ones((rows, columns), dtype=bool),
        observation_counts=np.tile(np.arange(1, rows + 1)[:, None], (1, columns)),
        fundamental_features=ke[:, :, None],
        fundamental_availability_mask=np.ones((rows, columns, 1), dtype=bool),
        fundamental_feature_names=("implied_ke",),
        fundamental_feature_contract=KE_PANEL_CONTRACT,
        fundamental_as_of_dates=np.full((rows, columns), "2025-12-31", dtype=object),
        fundamental_historical_walk_forward_eligible=True,
    )


def _strategy():
    return get_strategy("ke_threshold_allocation")


def test_fixed_strategy_is_registered_for_all_markets_without_search_dimensions():
    assert "ke_threshold_allocation" in list_strategy_ids()
    strategy = _strategy()
    assert strategy.param_space.dims == []
    assert strategy.parameter_schema.parameters == ()
    assert strategy.fundamental_feature_dependencies == ("implied_ke",)
    for market in ("a_share", "hk", "us"):
        panel = _panel()
        panel.market = market
        assert strategy.supports_market(market)
        assert strategy.make_signals(Params({}), panel).buy_signals[0].all()


def test_matches_existing_daily_ke_percentage_presentation_without_recomputing_history():
    # The shared display calculation opens no archive/SMTP and recomputes no history.
    pe = 1 / (0.09 - 0.02)
    assert implied_ke(pe) == pytest.approx(9.0)
    plan = _strategy().make_signals(
        Params({}), _panel((implied_ke(pe) / 100,))
    )
    assert plan.buy_signals.all()
    assert implied_ke(-10) is None


def test_thresholds_are_inclusive_with_only_small_equality_tolerance():
    values = (
        0.09,
        0.09 - 0.5e-12,
        0.09 - 2e-12,
        0.07,
        0.05 + 2e-12,
        0.05 + 0.5e-12,
        0.05,
    )
    plan = _strategy().make_signals(Params({}), _panel(values, columns=1))
    assert plan.buy_signals[:, 0].tolist() == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert plan.sell_signals[:, 0].tolist() == [
        False,
        False,
        False,
        False,
        False,
        True,
        True,
    ]
    assert not (plan.buy_signals & plan.sell_signals).any()


def test_requests_are_persistent_and_executor_owns_all_actual_allocations():
    values = (0.100000000001, 0.100000000002, 0.07, 0.04, 0.04)
    plan = _strategy().make_signals(Params({}), _panel(values))
    assert plan.entry_events[:, 0].tolist() == [True, True, False, False, False]
    assert plan.exit_events[:, 0].tolist() == [False, False, False, True, True]
    np.testing.assert_array_equal(plan.entry_events, plan.buy_signals)
    np.testing.assert_array_equal(plan.exit_events, plan.sell_signals)
    assert plan.conviction.dtype == np.float64
    np.testing.assert_array_equal(
        plan.conviction[:, 0], np.array(values, dtype=np.float64)
    )
    assert plan.buy_priority[1, 0] > plan.buy_priority[0, 0]
    assert plan.target_weights is None
    assert not plan.force_exit_signals.any()
    assert plan.execution == {
        "model": "target_weight",
        "allocation_mode": "ke_equal_batch",
        "per_symbol_cap": 0.20,
        "total_exposure_cap": 1.0,
        "max_new_entries": 5,
        "min_holding_calendar_days": 30,
        "buy_price_model": "max_high_t_minus_1_t_t_plus_1",
        "sell_price_model": "trigger_day_low",
    }


def test_named_feature_position_and_other_market_features_cannot_change_ke_logic():
    panel = _panel()
    values = np.concatenate(
        [np.full(panel.fundamental_features.shape, 999.0), panel.fundamental_features],
        axis=2,
    )
    panel.fundamental_features = values
    panel.fundamental_availability_mask = np.ones(values.shape, dtype=bool)
    panel.fundamental_feature_names = ("unrelated_feature", "implied_ke")
    plan = _strategy().make_signals(Params({}), panel)
    assert plan.buy_signals[:, 0].tolist() == [True, False, False, True]


def test_missing_untradable_and_insufficient_history_never_generate_requests():
    panel = _panel((0.11, 0.11, 0.04, 0.04))
    panel.fundamental_availability_mask[0, 0, 0] = False
    panel.fundamental_features[0, 0, 0] = np.nan
    panel.fundamental_as_of_dates[0, 0] = None
    panel.tradable[1, 0] = False
    panel.prices[2, 0] = np.nan
    panel.observation_counts[3, 0] = 0
    plan = _strategy().make_signals(Params({}), panel)
    assert not plan.buy_signals[:, 0].any()
    assert not plan.sell_signals[:, 0].any()
    assert np.count_nonzero(plan.conviction[:, 0]) == 0
    assert plan.buy_signals[:2, 1].all() and plan.sell_signals[2:, 1].all()


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_nonfinite_available_ke_is_rejected(value):
    panel = _panel((value,))
    with pytest.raises(ValueError, match="finite"):
        _strategy().make_signals(Params({}), panel)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("future", "future data"),
        ("missing_date", "missing source date"),
        ("current_only", "dated historical"),
        ("contract", "daily-implied-ke-history"),
        ("names", "unique named"),
        ("duplicates", "unique named"),
    ],
)
def test_historical_causal_provenance_is_mandatory(mutation, match):
    panel = _panel()
    if mutation == "future":
        panel.fundamental_as_of_dates[0, 0] = "2026-01-02"
    elif mutation == "missing_date":
        panel.fundamental_as_of_dates[0, 0] = None
    elif mutation == "current_only":
        panel.fundamental_historical_walk_forward_eligible = False
    elif mutation == "contract":
        panel.fundamental_feature_contract = "current-snapshot/1"
    elif mutation == "names":
        panel.fundamental_feature_names = ("capm_ke",)
    else:
        panel.fundamental_features = np.repeat(panel.fundamental_features, 2, axis=2)
        panel.fundamental_availability_mask = np.ones(
            panel.fundamental_features.shape, dtype=bool
        )
        panel.fundamental_feature_names = ("implied_ke", "implied_ke")
    with pytest.raises(ValueError, match=match):
        _strategy().make_signals(Params({}), panel)


@pytest.mark.parametrize(
    "mutation",
    [
        "values",
        "mask",
        "mask_dtype",
        "source_dates",
        "prices",
        "indicator",
        "tradable",
        "tradable_dtype",
        "ordinals",
    ],
)
def test_malformed_shapes_and_typed_masks_fail_closed(mutation):
    panel = _panel()
    if mutation == "values":
        panel.fundamental_features = panel.fundamental_features[:-1]
    elif mutation == "mask":
        panel.fundamental_availability_mask = panel.fundamental_availability_mask[:-1]
    elif mutation == "mask_dtype":
        panel.fundamental_availability_mask = (
            panel.fundamental_availability_mask.astype(int)
        )
    elif mutation == "source_dates":
        panel.fundamental_as_of_dates = panel.fundamental_as_of_dates[:-1]
    elif mutation == "prices":
        panel.prices = panel.prices[:-1]
    elif mutation == "indicator":
        panel.indicator_matrix = panel.indicator_matrix[:-1]
    elif mutation == "tradable":
        panel.tradable = panel.tradable[:-1]
    elif mutation == "tradable_dtype":
        panel.tradable = panel.tradable.astype(int)
    else:
        panel.date_ordinals = np.arange(4, dtype=float)
    with pytest.raises(ValueError):
        _strategy().make_signals(Params({}), panel)


def test_batch_revalidates_source_dates_after_preparation_and_preserves_scalar_precision():
    panel = _panel()
    strategy = _strategy()
    prepared = strategy.prepare(panel)
    scalar = strategy.make_signals(Params({}), panel)
    for batch in prepared.evaluate_batch([Params({}), Params({})]):
        np.testing.assert_array_equal(batch.buy_signals, scalar.buy_signals)
        np.testing.assert_array_equal(batch.conviction, scalar.conviction)
    panel.fundamental_as_of_dates[-1, 0] = "2026-01-05"
    with pytest.raises(ValueError, match="future data"):
        prepared.evaluate_batch([Params({})])


def test_no_parameters_or_execution_override_can_change_fixed_strategy():
    strategy = _strategy()
    with pytest.raises(ValueError, match="no search parameters"):
        strategy.execution_params(Params({"ke_buy": 0.08}))
    params = Params({})
    params.execution_snapshot = {
        **strategy.execution_params(params),
        "max_new_entries": 100,
    }
    with pytest.raises(ValueError, match="fixed contract"):
        strategy.make_signals(params, _panel())
    with pytest.raises(TypeError, match="historical Ke panel"):
        strategy.make_signals(Params({}), StrategyMarketData(np.zeros((1, 1, 1))))


def test_validated_asof_dates_are_checked_on_every_scalar_call():
    panel = _panel()
    with patch.object(
        panel, "validate_fundamental_panel", wraps=panel.validate_fundamental_panel
    ) as validate:
        _strategy().make_signals(Params({}), panel)
        _strategy().make_signals(Params({}), panel)
    assert validate.call_count == 2


def test_prefix_history_is_not_replaced_by_later_values_or_an_implicit_bot_price():
    panel = _panel()
    baseline = _strategy().make_signals(Params({}), panel)
    changed = deepcopy(panel)
    changed.fundamental_features[-1, :, 0] = 0.03
    changed.prices[-1] = 999
    result = _strategy().make_signals(Params({}), changed)
    np.testing.assert_array_equal(result.buy_signals[:-1], baseline.buy_signals[:-1])
    np.testing.assert_array_equal(result.conviction[:-1], baseline.conviction[:-1])
    assert (
        result.strategy_metadata["historical_bot_price_override"]
        == "unavailable_not_backfilled"
    )
