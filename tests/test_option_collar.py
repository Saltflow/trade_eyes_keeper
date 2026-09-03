import pandas as pd

from scripts.backtest_510300_collar import (
    Contract,
    _fourth_wednesday,
    _metrics,
    _parse_grid,
    _select_contracts,
    _strike_from_contract_id,
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
