"""Explicit, auditable reference-position changes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.core.ref_portfolio import RefPortfolioManager
from src.interactive.command_parser import RefPositionCommand, parse_command


def _bound_portfolio(tmp_path):
    manager = RefPortfolioManager(tmp_path / "ref.yaml")
    portfolio = manager.reset(
        initial_capital=100000.0,
        inception_date="2026-09-01",
        market_group="a_share",
        strategy_run_id="run-1",
        strategy_id="technical_ensemble",
        strategy_timestamp="2026-09-01T00:00:00",
        params_hash="params",
        execution_hash="execution",
    )
    return manager, portfolio


def test_manual_position_change_updates_cash_holdings_and_trade_log(tmp_path):
    manager, portfolio = _bound_portfolio(tmp_path)

    portfolio, trades = manager.adjust_position(
        portfolio,
        "510300",
        "set",
        1000,
        3.85,
        "2026-09-04",
        commission_rate=0.005,
        lot_size=100,
    )
    assert len(trades) == 1
    assert portfolio.holdings["510300"].shares == 1000
    assert portfolio.cash == pytest.approx(96130.75)
    assert portfolio.trade_log[-1].reason == "manual_bot"
    assert portfolio.trade_log[-1].run_id == "run-1"
    assert portfolio.trade_log[-1].strategy_id == "technical_ensemble"

    # The same target command is idempotent after the first persisted event.
    repeated, repeated_trades = manager.adjust_position(
        portfolio,
        "510300",
        "set",
        1000,
        3.85,
        "2026-09-04",
        commission_rate=0.005,
        lot_size=100,
    )
    assert repeated_trades == []
    assert repeated.holdings["510300"].shares == 1000
    assert repeated.cash == pytest.approx(portfolio.cash)

    repeated, trades = manager.adjust_position(
        repeated,
        "510300",
        "sell",
        500,
        4.00,
        "2026-09-04",
        commission_rate=0.005,
        lot_size=100,
    )
    assert len(trades) == 1
    assert repeated.holdings["510300"].shares == 500
    assert repeated.cash == pytest.approx(98120.75)


def test_manual_position_change_rejects_invalid_operator_input(tmp_path):
    manager, portfolio = _bound_portfolio(tmp_path)

    with pytest.raises(ValueError, match="multiple of lot"):
        manager.adjust_position(
            portfolio, "510300", "buy", 101, 3.85, "2026-09-04", lot_size=100
        )
    with pytest.raises(ValueError, match="cash insufficient"):
        manager.adjust_position(
            portfolio,
            "510300",
            "buy",
            30000,
            3.85,
            "2026-09-04",
            lot_size=100,
        )
    with pytest.raises(ValueError, match="exceed"):
        manager.adjust_position(
            portfolio, "510300", "sell", 100, 3.85, "2026-09-04", lot_size=100
        )

    unbound = manager.load()
    unbound.strategy_run_id = ""
    with pytest.raises(ValueError, match="initialized and bound"):
        manager.adjust_position(
            unbound, "510300", "buy", 100, 3.85, "2026-09-04", lot_size=100
        )


def test_feishu_dispatch_routes_reference_position_command():
    from src.interactive.feishu_handler import _dispatch

    command = parse_command("/ref_position set a_share 510300 10000 3.85")
    assert isinstance(command, RefPositionCommand)
    with patch(
        "src.interactive.feishu_handler.handle_ref_position",
        return_value="routed",
    ) as handler:
        assert _dispatch(command) == "routed"
        handler.assert_called_once_with("set", "a_share", "510300", 10000, 3.85)
