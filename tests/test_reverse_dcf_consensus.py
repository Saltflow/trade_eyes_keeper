from datetime import date

from src.fundamental_embedding.intrinsic_value import (
    ExpertValuation,
    IntrinsicValueConfig,
    IntrinsicValueEstimate,
    SubjectiveRiskAdjustment,
    ValuationSnapshot,
)
from src.fundamental_embedding.reverse_dcf_consensus import (
    REVERSE_DCF_CONSENSUS_CONTRACT,
    build_reverse_dcf_consensus,
)


def _snapshot() -> ValuationSnapshot:
    return ValuationSnapshot(
        symbol="000001",
        evaluation_date=date(2026, 8, 18),
        market_date=date(2026, 8, 18),
        current_price=10.0,
        shares=100.0,
        revenue_per_share=10.0,
        earnings_per_share=1.0,
        free_cash_flow_per_share=0.8,
        book_value_per_share=8.0,
        dividend_per_share=0.3,
        roe=0.12,
        growth=0.05,
        payout_ratio=0.3,
        cash_conversion=0.8,
        earnings_stability=0.8,
        dividend_stability=0.8,
        fcf_history_count=4,
        dividend_history_count=4,
        financial_age_days=30,
    )


def _estimate() -> IntrinsicValueEstimate:
    experts = (
        ExpertValuation(
            "cash_flow_dcf",
            True,
            0.7,
            8.0,
            10.0,
            12.0,
            assumptions={
                "cash_per_share": 0.8,
                "terminal_growth": 0.02,
                "cash_flow_kind": "fcfe",
            },
        ),
        ExpertValuation(
            "earnings_power_dcf",
            True,
            0.5,
            8.0,
            10.0,
            12.0,
            assumptions={
                "cash_per_share": 0.6,
                "terminal_growth": 0.02,
                "cash_flow_kind": "normalized_earnings",
            },
        ),
        ExpertValuation(
            "residual_income",
            True,
            0.9,
            8.0,
            10.0,
            12.0,
            assumptions={"cash_flow_kind": "residual_income"},
        ),
        ExpertValuation("dividend_discount", False, 0.0),
    )
    return IntrinsicValueEstimate(
        symbol="000001",
        evaluation_date=date(2026, 8, 18),
        market_date=date(2026, 8, 18),
        current_price=10.0,
        fair_value_low=8.0,
        fair_value=10.0,
        fair_value_high=12.0,
        buy_price=8.0,
        margin_of_safety=-0.2,
        fair_value_gap=0.0,
        confidence=0.8,
        gate={"cash_flow_dcf": 0.2, "earnings_power_dcf": 0.1},
        market_implied_growth={},
        experts=experts,
        risk=SubjectiveRiskAdjustment(),
        required_return_policy={
            "market_cost_of_equity": {"base": 0.08},
        },
        reverse_dcf={},
    )


def test_consensus_keeps_experts_and_excludes_residual_income():
    result = build_reverse_dcf_consensus(
        _estimate(), _snapshot(), IntrinsicValueConfig()
    )

    assert result.status == "solved_consensus"
    assert result.candidate_count == 2
    assert result.implied_growth is not None
    assert {item.expert_id for item in result.candidates} == {
        "cash_flow_dcf",
        "earnings_power_dcf",
        "residual_income",
        "dividend_discount",
    }
    residual = next(
        item for item in result.candidates if item.expert_id == "residual_income"
    )
    assert residual.status == "no_positive_equity_cash_flow"


def test_consensus_is_unresolved_without_positive_cash_flow():
    estimate = _estimate()
    estimate = IntrinsicValueEstimate(
        **{**estimate.__dict__, "experts": (ExpertValuation("residual_income", True, 0.9),)}
    )
    result = build_reverse_dcf_consensus(
        estimate, _snapshot(), IntrinsicValueConfig()
    )
    assert result.status == "unresolved"
    assert result.candidate_count == 0
    assert result.to_dict()["candidates"][0]["status"] == (
        "no_positive_equity_cash_flow"
    )
    assert REVERSE_DCF_CONSENSUS_CONTRACT == "market-cost-reverse-dcf-consensus-1"
