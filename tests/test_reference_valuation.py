"""Missing holding quotes must never turn into a fabricated NAV loss."""

from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from src.core.ref_portfolio import Holding, RefPortfolio, RefPortfolioManager


def _portfolio() -> RefPortfolio:
    return RefPortfolio(
        inception_date="2026-09-02",
        cash=50000.0,
        initial_capital=100000.0,
        holdings={
            "QQQ": Holding("QQQ", 5, 4000.0, "2026-09-16"),
            "VOO": Holding("VOO", 5, 4500.0, "2026-09-16"),
        },
        market_group="us",
        strategy_run_id="pinned-run",
        strategy_id="technical_ensemble",
        params_hash="original-params",
        execution_hash="original-execution",
    )


@pytest.mark.parametrize(
    "quote", [None, 0, -1, float("nan"), float("inf"), -float("inf"), True, "bad"]
)
def test_invalid_holding_quote_blocks_aggregate_valuation(quote):
    portfolio = _portfolio()
    before = portfolio.to_dict()
    prices = {"QQQ": quote, "VOO": 4200.0}

    status = RefPortfolioManager.get_status(portfolio, prices)

    assert portfolio.total_market_value(prices) is None
    assert portfolio.nav(prices) is None
    assert portfolio.nav_return_pct(prices) is None
    assert RefPortfolioManager.calculate_nav(portfolio, prices) is None
    assert status["nav"] is None
    assert status["nav_return_pct"] is None
    assert status["total_market_value"] is None
    assert status["valuation_complete"] is False
    assert status["missing_price_codes"] == ["QQQ"]
    assert status["valuation_reason"] == "估值不完整：缺少 QQQ 行情"
    holdings = {holding["code"]: holding for holding in status["holdings"]}
    assert holdings["QQQ"]["price"] is None
    assert holdings["QQQ"]["market_value"] is None
    assert holdings["QQQ"]["price_available"] is False
    assert holdings["VOO"]["market_value"] == 21000.0
    assert status["cash"] == 50000.0
    assert status["requires_manual_reset"] is False
    assert portfolio.to_dict() == before


def test_no_quotes_retains_holdings_and_cash_without_claiming_a_loss():
    portfolio = _portfolio()

    status = RefPortfolioManager.get_status(portfolio)

    assert status["missing_price_codes"] == ["QQQ", "VOO"]
    assert len(status["holdings"]) == 2
    assert status["cash"] == portfolio.cash
    assert status["nav"] is None
    assert status["nav_return_pct"] is None


def test_empty_portfolio_has_complete_cash_valuation_without_quotes():
    portfolio = RefPortfolio(cash=90000.0)

    status = RefPortfolioManager.get_status(portfolio)

    assert status["nav"] == 90000.0
    assert status["nav_return_pct"] == -10.0
    assert status["total_market_value"] == 0.0
    assert status["valuation_complete"] is True
    assert status["missing_price_codes"] == []


def test_removed_symbol_is_quoted_without_changing_watchlist_or_account():
    from main import _reference_portfolio_prices

    portfolio = _portfolio()
    before = portfolio.to_dict()
    stock_data = pd.DataFrame(
        [{"stock_code": "VOO", "close": 600.0}, {"stock_code": "BRK.B", "close": 450.0}]
    )
    before_data = stock_data.copy(deep=True)
    fetch_quote = Mock(return_value={"close": 650.0})

    prices = _reference_portfolio_prices(portfolio, stock_data, 7.0, fetch_quote)
    status = RefPortfolioManager.get_status(portfolio, prices)

    fetch_quote.assert_called_once_with("QQQ")
    assert prices == {"VOO": 4200.0, "QQQ": 4550.0}
    assert status["nav"] == 93750.0
    assert status["nav_return_pct"] == -6.25
    assert status["valuation_complete"] is True
    assert status["valuation_reason"] == ""
    assert portfolio.to_dict() == before
    pd.testing.assert_frame_equal(stock_data, before_data)


@pytest.mark.parametrize("quote", [None, {"close": 0}, {"close": float("nan")}])
def test_failed_supplemental_quote_remains_missing(quote):
    from main import _reference_portfolio_prices

    portfolio = _portfolio()
    stock_data = pd.DataFrame([{"stock_code": "VOO", "close": 600.0}])
    fetch_quote = Mock(return_value=quote)

    prices = _reference_portfolio_prices(portfolio, stock_data, 7.0, fetch_quote)

    assert prices == {"VOO": 4200.0}
    assert RefPortfolioManager.get_status(portfolio, prices)["nav"] is None


def test_supplemental_quote_failure_does_not_abort_other_holdings():
    from main import _reference_portfolio_prices

    portfolio = _portfolio()
    fetch_quote = Mock(side_effect=[TimeoutError("quote timeout"), {"close": 600.0}])

    prices = _reference_portfolio_prices(portfolio, pd.DataFrame(), 7.0, fetch_quote)

    assert prices == {"VOO": 4200.0}
    assert [call.args[0] for call in fetch_quote.call_args_list] == ["QQQ", "VOO"]


def test_valid_session_prices_are_reused_and_invalid_prices_are_refetched():
    from main import _reference_portfolio_prices

    portfolio = _portfolio()
    stock_data = pd.DataFrame(
        [{"stock_code": "VOO", "close": 600.0}, {"stock_code": "QQQ", "close": -1.0}]
    )
    fetch_quote = Mock(return_value={"close": 650.0})

    prices = _reference_portfolio_prices(portfolio, stock_data, 7.0, fetch_quote)

    fetch_quote.assert_called_once_with("QQQ")
    assert prices["QQQ"] == 4550.0


@pytest.mark.parametrize("fx_rate", [0, -1, None, float("inf"), float("nan")])
def test_invalid_fx_rate_cannot_create_a_valuation(fx_rate):
    from main import _reference_portfolio_prices

    fetch_quote = Mock()
    with pytest.raises(ValueError, match="FX rate"):
        _reference_portfolio_prices(_portfolio(), pd.DataFrame(), fx_rate, fetch_quote)
    fetch_quote.assert_not_called()


def test_bots_show_missing_valuation_and_preserve_trading_block():
    from src.notification.feishu_notifier import _build_ref_portfolio_markdown
    from src.notification.telegram_notifier import TelegramNotifier

    status = RefPortfolioManager.get_status(_portfolio(), {"VOO": 4200.0})
    status["_label"] = "美股（停单）"
    status["_trading_blocked_reason"] = "固定运行或执行合同不可恢复"
    session = SimpleNamespace(ref_portfolio_status={"us": status})

    for render in (
        _build_ref_portfolio_markdown,
        TelegramNotifier._build_ref_portfolio_text,
    ):
        body = render(session)
        assert "净值 待补全" in body
        assert "回报 待补全" in body
        assert "缺少 QQQ 行情" in body
        assert "现金 50,000" in body
        assert "QQQ" in body
        assert "停单原因：固定运行或执行合同不可恢复" in body
        assert "-29.0%" not in body
