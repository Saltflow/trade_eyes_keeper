"""External share prices preserve cash actions and fail on missing evidence."""

from datetime import date

import pandas as pd
import pytest

from src.data.in_kind_market_inputs import (
    attach_in_kind_market_inputs,
    external_asset_frame,
)
from src.data.market_history import CorporateAction, PriceHistoryBundle


def _bundle(code="09618", currency="HKD"):
    dates = pd.date_range("2022-01-03", periods=4, freq="B")
    prices = pd.DataFrame({"date": dates, "volume": 1000, "tradable": True})
    for prefix in ("raw", "qfq"):
        for field in ("open", "high", "low", "close"):
            prices[f"{prefix}_{field}"] = [20.0, 19.0, 19.0, 19.0]
    prices["qfq_factor"] = 1.0
    return PriceHistoryBundle(code=code, prices=prices, currency=currency).validate()


def _resolved_action():
    from src.data.in_kind_distributions import InKindDistributionTerms

    return CorporateAction(
        code="00700",
        action_type="in_kind_distribution",
        ex_date=date(2022, 1, 4),
        published_at=date(2022, 1, 3),
        currency="HKD",
        source="synthetic_unit_fixture",
        in_kind_terms=InKindDistributionTerms(
            asset_code="09618",
            asset_currency="HKD",
            shares_per_parent_share=0.1,
            record_date=date(2022, 1, 4),
            actual_delivery_date=date(2022, 1, 5),
            actual_delivery_published_at=date(2022, 1, 5),
            delivery_source_url="fixture:delivery",
            delivery_source_sha256="b" * 64,
            evidence_published_at=date(2022, 1, 3),
            source_url="fixture:declaration",
            source_sha256="a" * 64,
            minimum_parent_shares=0,
            eligible=True,
            account_election="shares",
            fractional_policy="exact",
            minimum_cash_payment=0,
            fee_per_asset_share=0,
            withholding_per_asset_share=0,
            disposal_policy="hold",
            source_adjustment_factor=0.9,
            adjustment_published_at=date(2022, 1, 3),
            adjustment_source_url="fixture:adjustment",
            adjustment_source_sha256="c" * 64,
        ),
    )


@pytest.mark.parametrize("complete", [False, True])
def test_explicit_receipt_import_survives_full_preparation_without_estimate_repair(
    tmp_path,
    monkeypatch,
    complete,
):
    import hashlib
    import json
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from scripts.import_in_kind_settlement import import_settlement
    from src.data.backtest_data import prepare_backtest_data
    from src.data.market_history import PointInTimeMarketStore

    parent = _bundle("00700")
    action = _resolved_action()
    action.in_kind_terms.settlement_basis = "stock_connect_estimate"
    action.in_kind_terms.estimation_policy_id = "stock_connect_estimate_v1:" + "f" * 64
    action.in_kind_terms.estimated_delivery_date = date(2022, 1, 6)
    parent.actions = [action]
    store = PointInTimeMarketStore(tmp_path / "store")
    paths = store.write(parent)
    store.write(_bundle("09618"))
    terms = {
        "actual_delivery_date": "2022-01-05",
        "actual_delivery_published_at": "2022-01-05",
    }
    if complete:
        terms.update(
            eligible=True,
            account_election="shares",
            disposal_policy="hold",
            fee_per_asset_share=0,
            withholding_per_asset_share=0,
        )
    content = json.dumps(
        {
            "schema": "in-kind-settlement-v1",
            "code": "00700",
            "ex_date": "2022-01-04",
            "asset_code": "09618",
            "source_url": "fixture:synthetic-actual-reconciliation",
            "terms": terms,
        }
    ).encode()
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(content)
    imported = import_settlement(
        store.root,
        "00700",
        date(2022, 1, 4),
        receipt,
        hashlib.sha256(content).hexdigest(),
        apply=True,
        reconcile_estimated=True,
    )
    assert imported["settlement_basis"] == "confirmed"
    assert imported["action_executable"] is complete
    before = {name: path.read_bytes() for name, path in paths.items()}
    provider = Mock()
    monkeypatch.setattr(
        "src.data.backtest_data.MarketHistoryProvider", lambda config: provider
    )
    config = {
        "point_in_time_data": {
            "output_dir": str(store.root),
            "market_history": {"in_kind_estimation": {"enabled": True}},
        }
    }
    prepared = prepare_backtest_data(
        config,
        ["00700"],
        date(2022, 1, 3),
        date(2022, 1, 6),
        purpose="optimizer",
        as_of=datetime(2022, 1, 7, tzinfo=timezone.utc),
    )
    provider.fetch.assert_not_called()
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    if complete:
        assert prepared.ready_codes == ("00700",)
        assert prepared.issues == []
        assert (
            prepared.bundles["00700"].actions[0].in_kind_terms.settlement_basis
            == "confirmed"
        )
    else:
        assert prepared.ready_codes == ()
        assert len(prepared.issues) == 1
        assert prepared.issues[0].category == "confirmed_reconciliation_pending"
        assert "additional actual receipts" in prepared.issues[0].reason
        assert prepared.fetch_attempts[0].status == "confirmed_reconciliation_pending"
        assert (
            store.read("00700").actions[0].in_kind_terms.settlement_basis == "confirmed"
        )


def test_external_dividend_and_share_action_remain_explicit_gross_inputs():
    external = _bundle()
    external.actions = [
        CorporateAction(
            code=external.code,
            action_type="cash_and_bonus",
            ex_date=date(2022, 1, 4),
            published_at=date(2022, 1, 3),
            cash_per_share=1.0,
            share_multiplier=2.0,
            currency="HKD",
            source="fixture",
        )
    ]
    frame = external_asset_frame(
        external, _bundle("00700"), external.prices["date"], {}
    )
    assert frame.external_cash_dividend.tolist() == [0.0, 1.0, 0.0, 0.0]
    assert frame.external_share_multiplier.tolist() == [1.0, 2.0, 1.0, 1.0]
    assert frame.raw_close.tolist() == [20.0, 19.0, 19.0, 19.0]
    assert (frame.fx_rate == 1.0).all()


@pytest.mark.parametrize(
    "code,currency,reason",
    [
        ("TME", "USD", "cross-market"),
        ("09618", None, "currencies"),
        ("09618", "USD", "cross-currency"),
    ],
)
def test_external_quotes_never_guess_currency_or_foreign_close_availability(
    code,
    currency,
    reason,
):
    external = _bundle(code, currency)
    with pytest.raises(ValueError, match=reason):
        external_asset_frame(external, _bundle("00700"), external.prices.date, {})


def test_missing_first_external_price_cannot_be_backfilled_from_future():
    external = _bundle()
    dates = pd.date_range("2021-12-31", "2022-01-06", freq="B")
    with pytest.raises(ValueError, match="coverage"):
        external_asset_frame(external, _bundle("00700"), dates, {})


def test_missing_internal_session_has_value_but_cannot_be_sold():
    external = _bundle()
    dates = external.prices.date.copy()
    external.prices = external.prices.drop(index=1)
    frame = external_asset_frame(external, _bundle("00700"), dates, {})
    assert frame.loc[1, "raw_close"] == 20.0
    assert not frame.loc[1, "tradable"]


def test_first_target_suspension_uses_earlier_known_quote_without_sale():
    external = _bundle()
    dates = external.prices.date.iloc[1:].copy()
    external.prices = external.prices.drop(index=1)
    frame = external_asset_frame(external, _bundle("00700"), dates, {})
    assert frame.loc[0, "raw_close"] == 20.0
    assert not frame.loc[0, "tradable"]


def test_unresolved_entitlement_does_not_fetch_irrelevant_external_quotes():
    parent = _bundle("00700")
    parent.actions = [
        CorporateAction(
            code="00700",
            action_type="in_kind_distribution",
            ex_date=date(2022, 1, 4),
            published_at=date(2021, 12, 23),
            source="fixture",
        )
    ]

    class NoNetwork:
        def read(self, *args, **kwargs):
            pytest.fail("unresolved settlement must not read an external bundle")

        def fetch(self, *args, **kwargs):
            pytest.fail("unresolved settlement must not fetch external quotes")

    result = attach_in_kind_market_inputs(
        parent, {}, NoNetwork(), NoNetwork(), date(2022, 1, 6)
    )
    assert result is parent
    assert result.external_prices == {}


def test_reference_pending_external_shares_use_their_own_currency_profile():
    from unittest.mock import Mock

    from main import _reference_portfolio_prices
    from src.core.ref_portfolio import RefPortfolio

    portfolio = RefPortfolio(market_group="hk")
    portfolio.in_kind_positions = {
        "in_kind:00700:2022-01-04:TME": {
            "asset_code": "TME",
            "quantity": 2.0,
            "fraction": 0.0,
        }
    }
    quote = Mock(return_value={"close": 10.0})
    prices = _reference_portfolio_prices(
        portfolio, pd.DataFrame(), 0.9, quote, external_fx_rates={"us": 7.0}
    )
    assert prices == {"TME": 70.0}
    assert portfolio.total_market_value(prices) == 140.0


def test_reference_external_fx_is_not_assumed_from_parent():
    from unittest.mock import Mock

    from main import _reference_portfolio_prices
    from src.core.ref_portfolio import RefPortfolio

    portfolio = RefPortfolio(market_group="hk")
    portfolio.in_kind_positions = {
        "in_kind:00700:2022-01-04:TME": {
            "asset_code": "TME",
            "quantity": 2.0,
            "fraction": 0.0,
        }
    }
    quote = Mock(return_value={"close": 10.0})
    prices = _reference_portfolio_prices(portfolio, pd.DataFrame(), 0.9, quote)
    assert prices == {}
    quote.assert_not_called()
    assert portfolio.nav(prices) is None


def test_reference_source_inputs_survive_parent_sale(monkeypatch):
    from unittest.mock import Mock

    from main import (
        _reference_portfolio_actions,
        _reference_portfolio_external_prices,
    )
    from src.core.ref_portfolio import RefPortfolio

    parent = _bundle("00700")
    parent.actions = [_resolved_action()]
    parent.external_prices = {"09618": _bundle().prices}
    store = Mock()
    store.read.return_value = parent
    monkeypatch.setattr(
        "src.data.market_history.PointInTimeMarketStore", lambda root: store
    )
    portfolio = RefPortfolio(market_group="hk")
    portfolio.in_kind_positions = {
        "in_kind:00700:2022-01-04:09618": {
            "asset_code": "09618",
            "quantity": 1.0,
            "fraction": 0.5,
        }
    }
    assert portfolio.holdings == {}
    assert _reference_portfolio_actions({}, portfolio) == parent.actions
    assert "09618" in _reference_portfolio_external_prices({}, portfolio)
    assert all(call.args == ("00700",) for call in store.read.call_args_list)


def test_ledger_report_keeps_escapes_and_non_cash_diagnostics():
    from src.utils.in_kind_reporting import render_in_kind_ledger

    rendered = render_in_kind_ledger(
        [
            {
                "event_id": "<script>alert(1)</script>",
                "asset_code": "09618",
                "quantity": 10,
                "fractional_quantity": 0.5,
                "delivered": False,
                "value": 200,
            }
        ],
        [
            {
                "event_id": "e1",
                "asset_code": "09618",
                "date": "2022-03-25",
                "kind": "fractional_cash_forfeited",
                "amount": 50,
                "affects_cash": False,
            }
        ],
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "fractional_cash_forfeited" in rendered
    assert "False" in rendered
    assert "交付前不可交易" in rendered


@pytest.mark.parametrize(
    "execution, expected, absent",
    [
        (
            {
                "model": "target_weight",
                "per_symbol_cap": 0.15,
                "total_exposure_cap": 0.6,
            },
            "目标仓位: 单标的上限 15% / 总仓位上限 60%",
            "单笔现金上限",
        ),
        (
            {"model": "cash_ladder", "buy_cash_limit": 1000, "sell_cash_limit": 2000},
            "单笔现金上限: 买入 1000 / 卖出 2000 元",
            "目标仓位",
        ),
    ],
)
def test_optimizer_notification_uses_actual_execution_model(
    execution, expected, absent
):
    from src.notification.report_builders import build_optimizer_summary
    from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary

    summary = OptimizerRunSummary(
        "fixture",
        "fixture",
        "2026-10-31",
        1,
        {
            "a_share": OptimizerGroupSummary(
                "a_share",
                execution=execution,
                status="completed",
                wf_score=0.0,
            )
        },
    )
    body = build_optimizer_summary(summary)
    assert expected in body
    assert absent not in body


def test_stale_cached_external_quotes_are_refreshed_when_parent_window_grows():
    from unittest.mock import Mock

    parent = _bundle("00700")
    parent.actions = [_resolved_action()]
    parent.external_prices = {
        "09618": pd.DataFrame({"date": pd.date_range("2022-01-04", "2022-01-05")})
    }
    store = Mock()
    store.read.return_value = _bundle()
    provider = Mock()
    result = attach_in_kind_market_inputs(parent, {}, store, provider, date(2022, 1, 6))
    assert result.external_prices["09618"].date.max().date() == date(2022, 1, 6)
    store.read.assert_called_once_with("09618", config={})
    provider.fetch.assert_not_called()


def test_complete_cached_frame_cannot_bypass_source_currency_evidence():
    from unittest.mock import Mock

    parent = _bundle("00700")
    parent.actions = [_resolved_action()]
    parent.external_prices = {"09618": _bundle().prices}
    store = Mock()
    store.read.return_value = _bundle(currency="USD")
    with pytest.raises(ValueError, match="currency conflicts"):
        attach_in_kind_market_inputs(parent, {}, store, Mock(), date(2022, 1, 6))


def test_disabled_policy_blocks_cached_estimates_on_real_reference_entry(monkeypatch):
    from unittest.mock import Mock

    from main import _reference_portfolio_actions
    from src.core.ref_portfolio import Holding, RefPortfolio

    parent = _bundle("00700")
    action = _resolved_action()
    action.in_kind_terms.settlement_basis = "stock_connect_estimate"
    action.in_kind_terms.estimation_policy_id = "stock_connect_estimate_v1:" + "a" * 64
    parent.actions = [action]
    store = Mock()
    store.read.return_value = parent
    monkeypatch.setattr(
        "src.data.market_history.PointInTimeMarketStore", lambda root: store
    )
    portfolio = RefPortfolio(market_group="hk", cash=1000)
    portfolio.holdings["00700"] = Holding("00700", 100, 20)
    before = portfolio.to_dict()
    with pytest.raises(ValueError, match="estimated policy disabled"):
        _reference_portfolio_actions({}, portfolio)
    assert portfolio.to_dict() == before


def test_in_kind_qfq_uses_verified_factor_instead_of_invented_cash():
    from src.data.price_adjustments import apply_disclosed_adjustments

    parent = _bundle("00700")
    result = apply_disclosed_adjustments(
        parent, [_resolved_action()], evidence="fixture"
    )
    assert result.prices.qfq_factor.tolist() == [0.9, 1.0, 1.0, 1.0]
    assert result.prices.loc[0, "qfq_close"] == 18.0
    assert result.actions[0].cash_per_share is None


def test_in_kind_qfq_rejects_missing_official_adjustment_factor():
    from src.data.price_adjustments import apply_disclosed_adjustments

    action = _resolved_action()
    action.in_kind_terms.source_adjustment_factor = None
    with pytest.raises(ValueError, match="adjustment"):
        apply_disclosed_adjustments(_bundle("00700"), [action], evidence="fixture")


def test_independent_vendor_qfq_is_preserved_without_guessing_cash_from_price_gap():
    from src.data.price_adjustments import apply_disclosed_adjustments

    parent = _bundle("00700")
    parent.source = "tencent_raw_qfq"
    parent.diagnostics = ["tencent_window_request:synthetic_unit_fixture"]
    action = _resolved_action()
    action.in_kind_terms.source_adjustment_factor = None
    expected = parent.prices.copy(deep=True)
    result = apply_disclosed_adjustments(parent, [action], evidence="fixture")
    pd.testing.assert_frame_equal(result.prices, expected)
    assert result.actions[0].cash_per_share is None
    assert result.actions[0].share_multiplier is None
    assert any(
        "qfq_preserved_from_independent_provider_requests" in value
        for value in result.diagnostics
    )


def test_vendor_qfq_cannot_bypass_raw_factor_identity():
    from src.data.price_adjustments import apply_disclosed_adjustments

    parent = _bundle("00700")
    parent.source = "tencent_raw_qfq"
    parent.diagnostics = ["tencent_window_request:synthetic_unit_fixture"]
    parent.prices.loc[0, "qfq_factor"] = 0.5
    action = _resolved_action()
    action.in_kind_terms.source_adjustment_factor = None
    with pytest.raises(ValueError, match="qfq_close"):
        apply_disclosed_adjustments(parent, [action], evidence="fixture")


def test_optimizer_validation_snapshot_preserves_ledger_through_notification():
    from main import _optimizer_validation_snapshot
    from src.notification.report_builders import build_optimizer_summary
    from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary
    from src.strategy.api import EvaluationReport

    report = EvaluationReport(
        group="hk",
        engine_name="fixture",
        strategy_label="fixture",
        timestamp="fixture",
        total_return=0,
        excess_return=0,
        max_drawdown=0,
        sharpe_ratio=0,
        trade_count=0,
        avg_cash_pct=100,
        in_kind_positions=[
            {
                "event_id": "ledger-snapshot",
                "asset_code": "09618",
                "quantity": 10,
                "value": 200,
            }
        ],
    )
    snapshot = _optimizer_validation_snapshot(report)
    assert snapshot["in_kind_positions"] == report.in_kind_positions
    summary = OptimizerRunSummary(
        "fixture",
        "fixture",
        "fixture",
        0,
        {
            "hk": OptimizerGroupSummary(
                group="hk", status="completed", wf_score=0.0, validation=snapshot
            )
        },
    )
    body = build_optimizer_summary(summary)
    assert "ledger-snapshot" in body
    assert "09618" in body


def test_reference_status_renderer_includes_external_entitlement():
    from src.notification.email_notifier import EmailNotifier

    rendered = EmailNotifier._build_ref_portfolio_status_note(
        {
            "in_kind_positions": [
                {
                    "event_id": "reference-ledger",
                    "asset_code": "09618",
                    "quantity": 10,
                    "delivered": False,
                    "value": 200,
                }
            ],
            "in_kind_cashflows": [],
        }
    )
    assert "reference-ledger" in rendered
    assert "09618" in rendered
