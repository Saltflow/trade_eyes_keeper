from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.market_history import CorporateAction, PriceHistoryBundle
from src.fundamental_embedding.dcf_entry_calibration import (
    CapmDcfEntryCalibrator,
    CapmDcfEntryConfig,
    CapmDcfEntryParameters,
    DcfEntryEpisode,
    GrowthInputs,
    GrowthWeights,
    ValuationModel,
    _normalized_future_path,
    _valuation_financial_age,
    equity_dcf_per_share,
    financial_growth,
    growth_inputs_from_statements,
    risk_adjusted_growth,
)
from src.instruments.models import FinancialStatementSnapshot


def _parameters(fraction: float = 0.95) -> CapmDcfEntryParameters:
    return CapmDcfEntryParameters(
        growth_weights=GrowthWeights("test", 0.5, 0.5, 0.0, 0.0, 0.0),
        growth_floor=0.0,
        growth_cap=0.10,
        entry_fair_value_fraction=fraction,
    )


def _episode(
    *,
    when: date = date(2021, 4, 30),
    lows: np.ndarray | None = None,
    closes: np.ndarray | None = None,
) -> DcfEntryEpisode:
    horizon = 504
    return DcfEntryEpisode(
        symbol="TEST",
        evaluation_date=when,
        market_date=when,
        current_price=4.0,
        cash_per_share=0.20,
        growth_inputs=GrowthInputs(
            revenue_cagr=0.05,
            earnings_cagr=0.05,
            fcf_cagr=None,
            roe_reinvestment=None,
            roe_trend=None,
            annual_observation_count=4,
            fcf_observation_count=3,
        ),
        risk_free_rate=0.02,
        beta=1.0,
        beta_observations=400,
        future_lows=(lows if lows is not None else np.full(horizon, 2.0, dtype=float)),
        future_closes=(
            closes if closes is not None else np.full(horizon, 5.0, dtype=float)
        ),
        financial_age_days=30,
        action_adjusted=False,
    )


def _calibrator(config: CapmDcfEntryConfig | None = None):
    benchmark = PriceHistoryBundle(
        code="510300",
        prices=pd.DataFrame(
            {
                "date": pd.date_range("2018-01-01", periods=800, freq="B"),
                "raw_open": 1.0,
                "raw_high": 1.0,
                "raw_low": 1.0,
                "raw_close": 1.0,
                "qfq_open": 1.0,
                "qfq_high": 1.0,
                "qfq_low": 1.0,
                "qfq_close": 1.0,
                "qfq_factor": 1.0,
                "volume": 1.0,
                "tradable": True,
            }
        ),
    ).validate()
    return CapmDcfEntryCalibrator(
        ".", benchmark, {date(2021, 4, 30): 0.02}, config=config
    )


def test_growth_combines_only_available_components_and_clips():
    growth, detail = financial_growth(
        GrowthInputs(
            revenue_cagr=0.30,
            earnings_cagr=-0.10,
            fcf_cagr=None,
            roe_reinvestment=None,
            roe_trend=None,
            annual_observation_count=4,
            fcf_observation_count=0,
        ),
        _parameters(),
    )

    assert growth == pytest.approx(0.10)
    assert detail["weighted_raw_growth"] == pytest.approx(0.10)


def test_erp_overlay_is_conservative_and_uses_the_requested_branches():
    config = CapmDcfEntryConfig()

    high_erp_growth, high_multiplier = risk_adjusted_growth(0.06, 0.06, config)
    low_erp_growth, low_multiplier = risk_adjusted_growth(0.06, 0.05, config)

    assert high_multiplier == pytest.approx(1.25)
    assert low_multiplier == pytest.approx(1.50)
    assert high_erp_growth == pytest.approx(0.048)
    assert low_erp_growth == pytest.approx(0.04)
    assert low_erp_growth < high_erp_growth < 0.06


def test_hkd_valuation_converts_cny_statement_at_point_in_time_fx():
    fx = PriceHistoryBundle(
        code="CNYHKD=X",
        prices=pd.DataFrame(
            {
                "date": [pd.Timestamp("2021-04-30")],
                "raw_open": [1.20],
                "raw_high": [1.20],
                "raw_low": [1.20],
                "raw_close": [1.20],
                "qfq_open": [1.20],
                "qfq_high": [1.20],
                "qfq_low": [1.20],
                "qfq_close": [1.20],
                "qfq_factor": [1.0],
                "volume": [1.0],
                "tradable": [True],
            }
        ),
        currency="HKD",
    ).validate()
    calibrator = CapmDcfEntryCalibrator(
        ".",
        _calibrator().benchmark_bundle,
        {date(2021, 4, 30): 0.02},
        market="hk",
        market_currency="HKD",
        currency_conversion_bundles={"CNY": fx},
    )
    statement = FinancialStatementSnapshot(
        period_end=date(2020, 12, 31),
        published_at=date(2021, 3, 30),
        source="test",
        currency="CNY",
        revenue=100.0,
        net_income_parent=10.0,
        diluted_eps=1.0,
        free_cash_flow=8.0,
    )

    converted, error = calibrator._convert_statements_to_market_currency(
        [statement], date(2021, 4, 30)
    )

    assert error is None
    assert converted is not None
    assert converted[0].currency == "HKD"
    assert converted[0].revenue == pytest.approx(120.0)
    assert converted[0].diluted_eps == pytest.approx(1.20)
    assert converted[0].free_cash_flow == pytest.approx(9.60)


def test_currency_mismatch_without_auditable_fx_curve_rejects_episode():
    calibrator = CapmDcfEntryCalibrator(
        ".",
        _calibrator().benchmark_bundle,
        {date(2021, 4, 30): 0.02},
        market="hk",
        market_currency="HKD",
    )
    statement = FinancialStatementSnapshot(
        period_end=date(2020, 12, 31),
        published_at=date(2021, 3, 30),
        source="test",
        currency="CNY",
    )

    converted, error = calibrator._convert_statements_to_market_currency(
        [statement], date(2021, 4, 30)
    )

    assert converted is None
    assert error == "financial_currency_conversion_curve_missing:CNY_to_HKD"


def test_currency_conversion_rejects_a_quote_in_the_wrong_target_currency():
    fx = PriceHistoryBundle(
        code="CNYUSD=X",
        prices=pd.DataFrame(
            {
                "date": [pd.Timestamp("2021-04-30")],
                "raw_open": [0.15],
                "raw_high": [0.15],
                "raw_low": [0.15],
                "raw_close": [0.15],
                "qfq_open": [0.15],
                "qfq_high": [0.15],
                "qfq_low": [0.15],
                "qfq_close": [0.15],
                "qfq_factor": [1.0],
                "volume": [1.0],
                "tradable": [True],
            }
        ),
        currency="USD",
    ).validate()
    calibrator = CapmDcfEntryCalibrator(
        ".",
        _calibrator().benchmark_bundle,
        {date(2021, 4, 30): 0.02},
        market="hk",
        market_currency="HKD",
        currency_conversion_bundles={"CNY": fx},
    )
    statement = FinancialStatementSnapshot(
        period_end=date(2020, 12, 31),
        published_at=date(2021, 3, 30),
        source="test",
        currency="CNY",
    )

    converted, error = calibrator._convert_statements_to_market_currency(
        [statement], date(2021, 4, 30)
    )

    assert converted is None
    assert error == (
        "financial_currency_conversion_quote_currency_mismatch:"
        "expected=HKD;actual=USD"
    )


def test_equity_dcf_rejects_capm_rate_at_or_below_terminal_spread():
    assert equity_dcf_per_share(1.0, 0.04, 0.021, 0.02, 5, 0.0025) is None
    assert equity_dcf_per_share(1.0, 0.04, 0.08, 0.02, 5, 0.0025) > 0


def test_limit_entry_uses_future_low_and_success_uses_later_closes_only():
    calibrator = _calibrator()
    lows = np.full(504, 5.0, dtype=float)
    lows[10] = 2.0
    closes = np.full(504, 5.0, dtype=float)
    result = calibrator.evaluate(
        [_episode(lows=lows, closes=closes)], _parameters(), 0.06, include_rows=True
    )

    row = result["rows"][0]
    assert row["hit_within_one_year"] is True
    assert row["entry_trading_day"] == 11
    assert row["post_entry_above_rate"] == pytest.approx(1.0)
    assert row["success"] is True
    assert result["post_entry_success_rate"] == pytest.approx(1.0)


def test_marketable_target_fills_at_current_price_without_waiting_for_a_low():
    calibrator = _calibrator()
    expensive = replace(_episode(), cash_per_share=0.50)

    result = calibrator.evaluate(
        [expensive], _parameters(), 0.06, include_rows=True
    )

    row = result["rows"][0]
    assert row["eligible"] is True
    assert row["buy_price"] > row["current_price"]
    assert row["execution_price"] == row["current_price"]
    assert row["entry_execution_mode"] == "marketable_at_valuation_close"
    assert row["hit_within_one_year"] is True
    assert row["entry_trading_day"] == 0
    assert result["by_valuation_model"]["fcfe_dcf"][
        "marketable_entry_count"
    ] == 1


def test_financial_episode_uses_residual_income_without_fcfe_proxy():
    calibrator = _calibrator()
    financial = replace(
        _episode(
            lows=np.full(504, 1.0, dtype=float),
            closes=np.full(504, 2.0, dtype=float),
        ),
        cash_per_share=None,
        valuation_model=ValuationModel.RESIDUAL_INCOME,
        valuation_route_reason="financial_industry_ocf_not_fcfe",
        industry_code="J66",
        book_value_per_share=10.0,
        normalized_roe=0.15,
        payout_ratio=0.4,
    )

    result = calibrator.evaluate(
        [financial], _parameters(0.10), 0.06, include_rows=True
    )

    row = result["rows"][0]
    assert row["eligible"] is True
    assert row["valuation_model"] == "residual_income"
    assert row["cash_flow_basis"] == "book_value_and_normalized_roe"
    assert row["growth_risk_multiplier"] is None
    assert row["hit_within_one_year"] is True


def test_evaluate_accepts_independent_parameters_by_valuation_model():
    calibrator = _calibrator()
    ordinary = _episode()
    financial = replace(
        _episode(),
        cash_per_share=None,
        valuation_model=ValuationModel.RESIDUAL_INCOME,
        book_value_per_share=10.0,
        normalized_roe=0.15,
        payout_ratio=0.4,
    )
    policy = {
        "fcfe_dcf": _parameters(0.10),
        "residual_income": _parameters(0.90),
    }

    result = calibrator.evaluate(
        [ordinary, financial], policy, 0.06, include_rows=True
    )

    rows = {row["valuation_model"]: row for row in result["rows"]}
    assert rows["fcfe_dcf"]["entry_fair_value_fraction"] == pytest.approx(0.10)
    assert rows["residual_income"]["entry_fair_value_fraction"] == pytest.approx(
        0.90
    )


def test_split_is_normalized_in_the_label_but_not_by_future_dividend_adjustment():
    dates = pd.date_range("2024-01-01", periods=505, freq="B")
    frame = pd.DataFrame(
        {
            "date": dates,
            "raw_open": 10.0,
            "raw_high": 10.0,
            "raw_low": 10.0,
            "raw_close": 10.0,
            "qfq_open": 10.0,
            "qfq_high": 10.0,
            "qfq_low": 10.0,
            "qfq_close": 10.0,
            "qfq_factor": 1.0,
            "volume": 1.0,
            "tradable": True,
        }
    )
    frame.loc[5:, ["raw_open", "raw_high", "raw_low", "raw_close"]] = 4.0
    frame.loc[5:, ["qfq_open", "qfq_high", "qfq_low", "qfq_close"]] = 4.0
    bundle = PriceHistoryBundle(
        code="TEST",
        prices=frame,
        actions=[
            CorporateAction(
                code="TEST",
                action_type="split",
                ex_date=dates[5].date(),
                share_multiplier=2.0,
                source="test",
            )
        ],
    ).validate()

    path = _normalized_future_path(bundle, 0, 504)

    assert path is not None
    lows, closes, adjusted = path
    assert adjusted is True
    assert lows[4] == pytest.approx(8.0)
    assert closes[4] == pytest.approx(8.0)


def test_future_path_counts_tradable_sessions_not_source_rows():
    dates = pd.date_range("2024-01-01", periods=520, freq="B")
    frame = pd.DataFrame(
        {
            "date": dates,
            "raw_open": 10.0,
            "raw_high": 10.0,
            "raw_low": 10.0,
            "raw_close": 10.0,
            "qfq_open": 10.0,
            "qfq_high": 10.0,
            "qfq_low": 10.0,
            "qfq_close": 10.0,
            "qfq_factor": 1.0,
            "volume": 1.0,
            "tradable": True,
        }
    )
    # These are carried-forward suspension rows.  There are enough valid
    # sessions after them; the path must extend past the first 504 raw rows.
    frame.loc[100:109, "tradable"] = False
    bundle = PriceHistoryBundle(code="TEST", prices=frame).validate()

    path = _normalized_future_path(bundle, 0, 504)

    assert path is not None
    lows, closes, _ = path
    assert len(lows) == len(closes) == 504


def test_residual_income_uses_statement_age_not_irrelevant_fcfe_age():
    assert _valuation_financial_age(ValuationModel.RESIDUAL_INCOME, 910, 180) == 180
    assert _valuation_financial_age(ValuationModel.FCFE_DCF, 910, 180) == 910


def test_select_requires_entry_coverage_instead_of_rewarding_never_hit_price():
    config = CapmDcfEntryConfig(
        minimum_hit_count=1,
        minimum_hit_rate=0.5,
        target_success_rate=0.70,
        growth_floors=(0.0,),
        growth_caps=(0.10,),
        entry_fair_value_fractions=(0.55, 0.95),
        growth_profiles=(_parameters().growth_weights,),
    )
    calibrator = _calibrator(config)
    episode = _episode(lows=np.full(504, 3.0, dtype=float))

    selected, metrics, leaderboard = calibrator.select([episode], (0.06,))

    assert selected.entry_fair_value_fraction == pytest.approx(0.95)
    assert metrics[0.06]["hit_count"] == 1
    assert metrics[0.06]["post_entry_success_rate"] == pytest.approx(1.0)
    assert leaderboard[0]["parameters"]["entry_fair_value_fraction"] == pytest.approx(
        0.95
    )


def test_failed_gate_fallback_prioritizes_entry_quality_over_coverage():
    config = CapmDcfEntryConfig(
        minimum_hit_count=2,
        minimum_hit_rate=0.5,
        target_success_rate=0.70,
        growth_floors=(0.0,),
        growth_caps=(0.10,),
        entry_fair_value_fractions=(0.55, 0.95),
        growth_profiles=(_parameters().growth_weights,),
    )
    calibrator = _calibrator(config)
    episode = _episode(
        lows=np.full(504, 2.0, dtype=float),
        closes=np.full(504, 2.5, dtype=float),
    )

    selected, _, leaderboard = calibrator.select([episode], (0.06,))

    assert selected.entry_fair_value_fraction == pytest.approx(0.55)
    assert leaderboard[0]["gate_passes_all_erp_scenarios"] is False


def test_selection_does_not_ignore_a_no_hit_erp_stress_case():
    calibrator = _calibrator()
    complete = {
        0.04: {
            "hit_count": 12,
            "hit_rate": 0.05,
            "post_entry_success_rate": 0.75,
            "post_entry_success_wilson_lower_95": 0.50,
        },
        0.06: {
            "hit_count": 12,
            "hit_rate": 0.05,
            "post_entry_success_rate": 0.75,
            "post_entry_success_wilson_lower_95": 0.50,
        },
    }
    sparse = {
        0.04: {
            "hit_count": 2,
            "hit_rate": 0.01,
            "post_entry_success_rate": 1.0,
            "post_entry_success_wilson_lower_95": 0.34,
        },
        0.06: {
            "hit_count": 0,
            "hit_rate": 0.0,
            "post_entry_success_rate": None,
            "post_entry_success_wilson_lower_95": None,
        },
    }

    assert calibrator._metrics_key(complete) > calibrator._metrics_key(sparse)


def test_unsupported_route_is_fail_closed_without_borrowing_other_parameters():
    calibrator = _calibrator()
    financial = replace(
        _episode(),
        cash_per_share=None,
        valuation_model=ValuationModel.RESIDUAL_INCOME,
        book_value_per_share=10.0,
        normalized_roe=0.15,
        payout_ratio=0.4,
    )

    result = calibrator.evaluate(
        [financial], {"fcfe_dcf": _parameters(0.90)}, 0.06, include_rows=True
    )

    assert result["eligible_count"] == 0
    assert (
        result["rows"][0]["reason"]
        == "valuation_route_not_supported_by_training_gate"
    )


def test_route_selection_returns_a_rejected_policy_when_evidence_is_insufficient():
    calibrator = _calibrator()

    policy, metrics, details = calibrator.select_by_valuation_model(
        [_episode()], (0.04, 0.06)
    )

    assert policy == {}
    assert details["fcfe_dcf"]["route_supported"] is False
    assert details["fcfe_dcf"]["route_support_reason"] == (
        "insufficient_training_episodes"
    )
    assert metrics[0.04]["eligible_count"] == 0


def test_frozen_policy_transfers_to_a_small_pool_without_retraining(monkeypatch):
    calibrator = _calibrator()
    early = _episode(when=date(2021, 4, 30))
    late = _episode(when=date(2023, 4, 30))
    monkeypatch.setattr(
        calibrator,
        "build_episodes",
        lambda symbols=None: ([early, late], {"upstream_missing": 1}),
    )

    report = calibrator.run_frozen_policy(
        policy={"fcfe_dcf": _parameters(0.95)},
        policy_available_from=date(2022, 1, 1),
        erp_scenarios=(0.04, 0.06),
        policy_provenance={"source": "broad-universe"},
    )

    assert report["dataset"]["episode_count"] == 2
    assert report["dataset"]["application_episode_count"] == 1
    assert report["selection"]["frozen_policy"] is True
    assert report["selection"]["policy_provenance"]["source"] == "broad-universe"
    assert report["validation"]["passes_all_erp_scenarios"] is None
    assert report["validation"]["selection_never_read_validation_labels"] is True
    rows = report["validation"]["metrics"][0.06]["rows"]
    assert [row["evaluation_date"] for row in rows] == ["2023-04-30"]


def test_annual_inputs_use_only_disclosed_statements():
    reports = [
        FinancialStatementSnapshot(
            period_end=date(2020, 12, 31),
            published_at=date(2021, 4, 30),
            period_type="year",
            source="test",
            total_shares=100.0,
            revenue=100.0,
            net_income_parent=10.0,
            operating_cash_flow=15.0,
            capital_expenditures=5.0,
            free_cash_flow=10.0,
            reported_roe=10.0,
        ),
        FinancialStatementSnapshot(
            period_end=date(2021, 12, 31),
            published_at=date(2022, 4, 30),
            period_type="year",
            source="test",
            total_shares=100.0,
            revenue=200.0,
            net_income_parent=30.0,
            operating_cash_flow=40.0,
            capital_expenditures=10.0,
            free_cash_flow=30.0,
            reported_roe=20.0,
        ),
    ]

    from src.fundamental_embedding.dcf_entry_calibration import (
        growth_inputs_from_statements,
    )

    inputs, cash, _ = growth_inputs_from_statements(reports, date(2021, 5, 1))

    assert inputs.annual_observation_count == 1
    assert inputs.revenue_cagr is None
    assert cash is None


def test_annual_inputs_roll_historical_shares_to_the_valuation_date():
    """Per-share FCF must share the valuation-date basis of the market price."""
    from src.fundamental_embedding.dcf_entry_calibration import (
        growth_inputs_from_statements,
    )
    from src.instruments.point_in_time import adjust_statement_shares

    prior_report = FinancialStatementSnapshot(
        period_end=date(2019, 12, 31),
        published_at=date(2020, 4, 30),
        period_type="year",
        source="test",
        total_shares=100.0,
        revenue=180.0,
        net_income_parent=18.0,
        operating_cash_flow=22.0,
        capital_expenditures=4.0,
        free_cash_flow=18.0,
        reported_roe=9.0,
    )
    report = FinancialStatementSnapshot(
        period_end=date(2020, 12, 31),
        published_at=date(2021, 4, 30),
        period_type="year",
        source="test",
        total_shares=100.0,
        revenue=200.0,
        net_income_parent=20.0,
        operating_cash_flow=25.0,
        capital_expenditures=5.0,
        free_cash_flow=20.0,
        reported_roe=10.0,
    )
    adjusted = adjust_statement_shares(
        [prior_report, report],
        [
            CorporateAction(
                code="TEST",
                action_type="split",
                ex_date=date(2021, 6, 1),
                share_multiplier=2.0,
                source="test",
            )
        ],
        date(2021, 7, 1),
    )

    _, cash, _ = growth_inputs_from_statements(adjusted, date(2021, 7, 1))

    assert cash == pytest.approx(0.095)


def test_annual_inputs_reject_an_unreconciled_cash_flow_observation():
    reports = [
        FinancialStatementSnapshot(
            period_end=date(year, 12, 31),
            published_at=date(year + 1, 4, 30),
            period_type="year",
            source="test",
            total_shares=100.0,
            revenue=100.0,
            net_income_parent=10.0,
            operating_cash_flow=100.0,
            capital_expenditures=1.0,
            free_cash_flow=99.0,
            reported_roe=10.0,
        )
        for year in (2019, 2020)
    ]

    inputs, cash, _ = growth_inputs_from_statements(reports, date(2021, 5, 1))

    assert cash is None
    assert inputs.fcf_observation_count == 0
