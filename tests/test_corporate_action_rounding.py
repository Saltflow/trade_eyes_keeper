from datetime import date

import numpy as np
import pytest

from src.backtest.engine import (
    FastEvaluator,
    _post_action_shares,
    buy_and_hold_nav,
)
from src.backtest.execution import (
    CorporateActionSlice,
    ExecutionPriceSlice,
    build_corporate_action_schedule,
)
from src.data.market_history import CorporateAction
from src.search.config import ExecutionConfig
from src.strategy import TradePlan


def test_fund_rounding_is_identical_in_cash_target_batch_and_benchmark():
    actions = CorporateActionSlice(
        cash_dividends=np.zeros((3, 1)),
        share_multipliers=np.array([[1.0], [1.14539], [1.0]]),
        share_rounding_up=np.array([[False], [True], [False]]),
    )
    prices = np.array([[10.0], [10.0 / 1.14539], [10.0 / 1.14539]])
    execution = ExecutionPriceSlice(
        valuation_prices=prices,
        buy_prices=prices,
        sell_prices=prices,
        tradable=np.ones((3, 1), dtype=bool),
        corporate_actions=actions,
    )
    entries = np.array([[True], [False], [False]])
    exits = np.zeros((3, 1), dtype=bool)
    plans = []
    for model in ("cash_cap", "target_weight"):
        plans.append(
            TradePlan(
                buy_signals=entries,
                sell_signals=exits,
                buy_priority=np.ones((3, 1)),
                sell_priority=np.ones((3, 1)),
                buy_cash_limit=1000.0,
                sell_cash_limit=1000.0,
                execution={
                    "model": model,
                    "per_symbol_cap": 1.0,
                    "total_exposure_cap": 1.0,
                },
                entry_events=entries,
                exit_events=exits,
                conviction=np.ones((3, 1)),
                target_weights=np.ones((3, 1)),
                date_ordinals=np.array([1, 2, 3]),
                warmup_rows=0,
            )
        )
    evaluator = FastEvaluator(
        ExecutionConfig(
            initial_capital=1000.0,
            commission_rate=0.0,
            min_holding_days=0,
            lot_sizes={"a_share": 1},
        ),
        "a_share",
    )
    inputs = dict(
        indicator_matrix=np.zeros((3, 1, 1), dtype=np.float32),
        price_matrix=prices,
        cash_baseline=np.full(3, 1000.0),
        execution_prices=execution,
    )
    results = [evaluator.evaluate(trade_plan=plan, **inputs) for plan in plans]
    results += evaluator.evaluate_batch([plans[0]], workers=1, **inputs)
    for result in results:
        assert result.final_shares[0] == 115.0
        assert result.cost_basis[0] * result.final_shares[0] == pytest.approx(1000)
        assert result.final_asset == pytest.approx(round(115 * prices[-1, 0], 2))
    benchmark = buy_and_hold_nav(prices, prices, 1000, 1, 0, corporate_actions=actions)
    assert benchmark[-1] == pytest.approx(115 * prices[-1, 0])


def test_decimal_ratio_does_not_round_an_exact_integer_up_twice():
    assert _post_action_shares(100000.0, 1.14539, True) == 114539.0
    assert _post_action_shares(100.0, 1.14539, True) == 115.0
    assert _post_action_shares(100.0, 1.14539, False) == pytest.approx(114.539)


def test_rounding_survives_schedule_slicing_currency_and_tax():
    action = CorporateAction(
        code="510500",
        action_type="split",
        ex_date=date(2022, 8, 29),
        published_at=date(2022, 8, 23),
        record_date=date(2022, 8, 26),
        share_multiplier=1.14539,
        share_rounding="ceil",
        source="sse_test",
    )
    restored = CorporateAction.parse_raw(action.json())
    schedule = (
        build_corporate_action_schedule(
            [restored], ["2022-08-26", "2022-08-29"], ["510500"]
        )
        .sliced(1, 2)
        .scaled(0.9)
        .with_withholding(0.2)
    )
    assert schedule.share_rounding_up.tolist() == [[True]]
    assert schedule.share_multipliers.tolist() == [[1.14539]]
    with pytest.raises(ValueError, match="multiple rounded"):
        build_corporate_action_schedule([action, action], ["2022-08-29"], ["510500"])
