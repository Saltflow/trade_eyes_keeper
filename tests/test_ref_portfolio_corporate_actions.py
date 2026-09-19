from datetime import date

import pytest

from src.core.ref_portfolio import Holding, RefPortfolio, RefPortfolioManager
from src.data.market_history import CorporateAction


def test_reference_portfolio_nets_hk_dividend_and_adjusts_share_cost():
    portfolio = RefPortfolio(
        inception_date="2026-01-01",
        cash=1_000.0,
        market_group="hk",
        strategy_run_id="run-1",
        strategy_id="strategy-1",
    )
    portfolio.holdings["00883"] = Holding("00883", 100.0, 20.0)
    action = CorporateAction(
        code="00883",
        action_type="cash_and_bonus",
        ex_date=date(2026, 1, 5),
        published_at=date(2026, 1, 2),
        cash_per_share=1.0,
        share_multiplier=1.1,
        source="test",
    )

    updated, trades = RefPortfolioManager().apply_corporate_actions(
        portfolio,
        [action],
        "2026-01-05",
        withholding_rate=0.20,
        fx_rate=1.0,
    )

    assert updated.cash == pytest.approx(1_080.0)
    assert updated.gross_dividend_cash == pytest.approx(100.0)
    assert updated.dividend_tax_cost == pytest.approx(20.0)
    assert updated.net_dividend_cash == pytest.approx(80.0)
    assert updated.holdings["00883"].shares == pytest.approx(110.0)
    assert updated.holdings["00883"].avg_cost == pytest.approx(20.0 / 1.1)
    assert {trade.action for trade in trades} == {"dividend", "share_action"}

    replayed, repeated = RefPortfolioManager().apply_corporate_actions(
        updated,
        [action],
        "2026-01-05",
        withholding_rate=0.20,
    )
    assert repeated == []
    assert replayed.cash == pytest.approx(updated.cash)


def test_reference_portfolio_rejects_post_ex_dividend_publication():
    portfolio = RefPortfolio(inception_date="2026-01-01", cash=1_000.0)
    portfolio.holdings["00883"] = Holding("00883", 100.0, 20.0)
    action = CorporateAction(
        code="00883",
        action_type="cash_dividend",
        ex_date=date(2026, 1, 5),
        published_at=date(2026, 1, 6),
        cash_per_share=1.0,
        source="test",
    )

    with pytest.raises(ValueError, match="causal publication"):
        RefPortfolioManager().apply_corporate_actions(
            portfolio,
            [action],
            "2026-01-05",
            withholding_rate=0.20,
        )
