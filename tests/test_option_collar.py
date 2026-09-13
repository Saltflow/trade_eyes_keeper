import pandas as pd
import pytest

from scripts.backtest_510300_collar import (
    Contract,
    Roll,
    _fourth_wednesday,
    _metrics,
    _option_execution_price,
    _parse_grid,
    _select_contracts,
    _simulate,
    _spot_benchmark,
    _strike_from_contract_id,
)
from scripts.backtest_collar_nav_sizing import (
    _neutralize_relative_months,
    _simulate_nav_resized,
)


def _contract(contract_id, option_type, strike, month="2609"):
    return Contract(
        security_id=contract_id[-5:],
        contract_id=contract_id,
        option_type=option_type,
        strike=strike,
        contract_month=month,
    )


def test_strike_and_expiry_parsing():
    assert _strike_from_contract_id("510300P2609M04400") == 4.4
    assert _strike_from_contract_id("510500P2609M06000", "510500") == 6.0
    assert _fourth_wednesday(2026, 9) == pd.Timestamp("2026-09-23")


def test_parse_grid_deduplicates_and_sorts_targets():
    assert _parse_grid("1.10, 0.95, 1.10") == [0.95, 1.10]


def test_selects_earliest_expiry_and_nearest_strikes():
    contracts = [
        _contract("510300P2610M04400", "put", 4.4, "2610"),
        _contract("510300C2610M04900", "call", 4.9, "2610"),
        _contract("510300P2609M04400", "put", 4.4),
        _contract("510300P2609M04500", "put", 4.5),
        _contract("510300C2609M04900", "call", 4.9),
        _contract("510300C2609M05000", "call", 5.0),
    ]
    put, call = _select_contracts(
        contracts,
        pd.Timestamp("2026-09-01"),
        underlying_close=4.68,
        target_put=0.95,
        target_call=1.05,
    )
    assert put.contract_id == "510300P2609M04400"
    assert call.contract_id == "510300C2609M04900"


def test_metrics_uses_252_day_sharpe_and_drawdown():
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=7, freq="D"),
            "nav": [100.0, 101.0, 100.0, 102.0, 101.0, 103.0, 104.0],
            "roll_index": [0, 0, 0, 0, 0, 0, 0],
        }
    )
    frame["return"] = frame["nav"].pct_change()
    metrics = _metrics(frame)
    assert metrics["trading_days"] == 7
    assert metrics["rolls"] == 1
    assert metrics["max_drawdown_pct"] < 0
    assert metrics["sharpe"] > 0


def test_spot_benchmark_applies_entry_cost_once():
    underlying = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3, freq="D"),
            "close": [100.0, 110.0, 105.0],
            "high": [100.0, 110.0, 105.0],
            "low": [100.0, 110.0, 105.0],
        }
    )
    frame = _spot_benchmark(underlying, transaction_cost_rate=0.002)
    assert frame["nav"].round(6).tolist() == [0.998004, 1.097804, 1.047904]
    assert frame["roll_index"].nunique() == 1


def test_option_execution_prefers_quotes_then_uses_high_low():
    contract = _contract("510300P2609M04400", "put", 4.4)
    daily = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-09-01")],
            "high": [5.0],
            "low": [3.0],
            "close": [4.0],
            "ask": [4.5],
            "bid": [3.5],
        }
    )
    expiry = pd.Timestamp("2026-09-23")
    assert (
        _option_execution_price(
            daily, pd.Timestamp("2026-09-01"), expiry, 4.68, contract, "buy"
        )
        == 4.5
    )
    assert (
        _option_execution_price(
            daily, pd.Timestamp("2026-09-01"), expiry, 4.68, contract, "sell"
        )
        == 3.5
    )
    fallback = daily.drop(columns=["ask", "bid"])
    assert (
        _option_execution_price(
            fallback, pd.Timestamp("2026-09-01"), expiry, 4.68, contract, "buy"
        )
        == 5.0
    )
    assert (
        _option_execution_price(
            fallback, pd.Timestamp("2026-09-01"), expiry, 4.68, contract, "sell"
        )
        == 3.0
    )


def test_closed_roll_is_not_settled_again_at_expiry():
    put0 = Contract("put0", "put0", "put", 110.0, "2601", 1.0)
    call0 = Contract("call0", "call0", "call", 120.0, "2601", 1.0)
    put1 = Contract("put1", "put1", "put", 110.0, "2601", 1.0)
    call1 = Contract("call1", "call1", "call", 120.0, "2601", 1.0)
    rolls = [
        Roll("2026-01-02", "2026-01-28", "2601", put0, call0, 100.0, 1.0, 1.0),
        Roll("2026-01-05", "2026-01-28", "2601", put1, call1, 100.0, 1.0, 1.0),
    ]
    underlying = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-02", "2026-01-05", "2026-01-28", "2026-01-29"]
            ),
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [100.0, 100.0, 100.0, 100.0],
            "low": [100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
        }
    )
    daily = {
        security_id: pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
            }
        )
        for security_id in ("put0", "call0", "put1", "call1")
    }
    frame = _simulate(underlying, rolls, daily, transaction_cost_rate=0.0)
    assert frame["nav"].iloc[-1] == 110.0


def test_initial_option_cash_flow_is_not_counted_twice():
    put = Contract("put", "put", "put", 90.0, "2601", 1.0)
    call = Contract("call", "call", "call", 110.0, "2601", 1.0)
    rolls = [Roll("2026-01-02", "2026-01-28", "2601", put, call, 100.0, 5.0, 1.0)]
    underlying = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-28", "2026-01-29"]),
            "open": [100.0, 100.0, 100.0],
            "high": [100.0, 100.0, 100.0],
            "low": [100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0],
        }
    )
    daily = {
        security_id: pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "high": [price],
                "low": [price],
                "close": [price],
            }
        )
        for security_id, price in (("put", 5.0), ("call", 1.0))
    }
    frame = _simulate(underlying, rolls, daily, transaction_cost_rate=0.0)
    assert frame.attrs["initial_capital"] == 104.0
    assert frame["nav"].iloc[-1] == 100.0


def test_nav_resized_starts_with_strict_one_x_underlying_and_reconciles():
    put = Contract("put", "put", "put", 90.0, "2601", 1.0)
    call = Contract("call", "call", "call", 110.0, "2601", 1.0)
    rolls = [Roll("2026-01-02", "2026-01-28", "2601", put, call, 100.0, 5.0, 1.0)]
    underlying = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-28", "2026-01-29"]),
            "open": [101.0, 100.0, 100.0],
            "high": [101.0, 100.0, 100.0],
            "low": [101.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0],
        }
    )
    daily = {
        security_id: pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "high": [price],
                "low": [price],
                "close": [price],
            }
        )
        for security_id, price in (("put", 5.0), ("call", 1.0))
    }
    frame, ledger = _simulate_nav_resized(
        underlying, rolls, daily, transaction_cost_rate=0.0
    )
    assert frame["underlying_notional"].iloc[0] == 1.0
    assert frame.attrs["rebalance_checks"][0]["target_nav"] == 1.0
    assert (
        abs(float(ledger["collar_pnl"].sum()) - (frame["nav"].iloc[-1] - 1.0)) < 1e-12
    )


def test_relative_neutralization_keeps_all_months_and_compounds():
    monthly = pd.DataFrame(
        {
            "month": ["2026-01", "2026-02", "2026-03"],
            "collar_month_return": [0.10, -0.05, 0.02],
            "spot_month_return": [0.00, 0.00, 0.00],
            "trading_days": [21, 20, 22],
        }
    )
    monthly["relative_factor_return_pct"] = (
        (1.0 + monthly["collar_month_return"]) / (1.0 + monthly["spot_month_return"])
        - 1.0
    ) * 100.0
    result = _neutralize_relative_months(monthly)
    assert result["top_relative_months"][0]["month"] == "2026-01"
    neutralized = result["results"][1]
    assert neutralized["neutralized_months"] == ["2026-01"]
    assert neutralized["collar_total_return_pct"] == pytest.approx(
        (-0.05 + 0.02 - 0.05 * 0.02) * 100.0
    )
    paths = result["monthly_with_neutralized_paths"]
    assert len(paths) == 3
