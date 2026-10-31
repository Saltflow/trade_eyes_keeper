"""Real issuer terms stay separate from account-specific settled entitlements."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.data.in_kind_distributions import (
    InKindDistributionTerms,
    match_hkex_in_kind_completion_notice,
    match_hkex_in_kind_notice,
    verify_settlement_evidence,
)
from src.data.market_history import (
    CorporateAction,
    PointInTimeMarketStore,
    PriceHistoryBundle,
    corporate_action_issues,
)

FIXTURES = Path(__file__).parent / "fixtures" / "in_kind_distributions"
SOURCES = {
    item["id"]: item
    for item in json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
}


def source(name):
    text = (FIXTURES / (name + ".txt")).read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode()).hexdigest() == SOURCES[name]["text_sha256"]
    return text


def declaration(name, published, ex_date):
    action = CorporateAction(
        code="00700",
        action_type="in_kind_distribution",
        ex_date=ex_date,
        published_at=published,
        source="etnet_dividend_history",
    )
    result = match_hkex_in_kind_notice(
        source(name),
        action,
        published_at=published,
        source_url=SOURCES[name]["url"],
        listed_code="00700",
    )
    assert result is not None
    assert action.in_kind_terms is None  # caller-owned source remains unchanged
    return result


@pytest.mark.parametrize(
    "name,published,ex_date,asset,denominator,record,expected",
    [
        (
            "jd_declaration",
            date(2021, 12, 23),
            date(2022, 1, 20),
            "09618",
            21,
            date(2022, 1, 25),
            date(2022, 3, 25),
        ),
        (
            "meituan_declaration",
            date(2022, 11, 16),
            date(2023, 1, 5),
            "03690",
            10,
            date(2023, 1, 10),
            date(2023, 3, 24),
        ),
    ],
)
def test_real_ordinary_share_terms_do_not_prove_account_settlement(
    name,
    published,
    ex_date,
    asset,
    denominator,
    record,
    expected,
):
    action = declaration(name, published, ex_date)
    terms = action.in_kind_terms
    assert terms.asset_code == asset
    assert terms.shares_per_parent_share == pytest.approx(1 / denominator)
    assert terms.asset_currency == "HKD"
    assert terms.record_date == record
    assert terms.expected_delivery_date == expected
    assert terms.minimum_cash_payment == 100
    assert terms.fractional_policy == "cash_in_lieu"
    assert terms.actual_delivery_date is None
    assert terms.cash_in_lieu_per_asset_share is None
    assert terms.eligible is None
    assert terms.fee_per_asset_share is None
    assert any(
        "missing verified" in value for value in corporate_action_issues([action])
    )


def test_real_tme_cash_tiers_are_not_a_linear_dividend():
    action = declaration("tme_declaration", date(2018, 12, 13), date(2018, 12, 28))
    terms = action.in_kind_terms
    assert terms.allocation_rule == "tiered_ads_election"
    assert terms.ratio_denominator == 3900
    assert terms.minimum_parent_shares == 3900
    assert terms.cash_parent_lot == 500
    assert terms.cash_per_lot == 13
    assert terms.minimum_cash_parent_shares == 1500
    assert terms.minimum_cash_payment == 39
    assert terms.declared_cash_election_per_unit == 101
    assert terms.expected_delivery_date == date(2019, 2, 20)
    assert terms.asset_code == "TME"
    assert terms.asset_currency == "USD"
    assert terms.cash_currency == "HKD"
    assert terms.minimum_sale_lock_days == 40
    assert terms.lock_anchor == "last_distribution_issuance"
    assert any(
        "missing verified tiered" in issue
        for issue in corporate_action_issues([action])
    )


@pytest.mark.parametrize(
    "name,published,ex_date,completed",
    [
        ("jd", date(2021, 12, 23), date(2022, 1, 20), date(2022, 3, 25)),
        ("meituan", date(2022, 11, 16), date(2023, 1, 5), date(2023, 3, 24)),
    ],
)
def test_real_completion_confirms_dispatch_not_custody_or_net_cash(
    name,
    published,
    ex_date,
    completed,
):
    action = declaration(name + "_declaration", published, ex_date)
    result = match_hkex_in_kind_completion_notice(
        source(name + "_completion"),
        action,
        published_at=completed,
        source_url=SOURCES[name + "_completion"]["url"],
        listed_code="00700",
    )
    assert result.in_kind_terms.share_certificate_dispatch_date == completed
    assert result.in_kind_terms.actual_delivery_date is None
    assert result.in_kind_terms.cash_in_lieu_payment_date is None
    assert result.in_kind_terms.cash_in_lieu_per_asset_share is None
    assert corporate_action_issues([result])


@pytest.mark.parametrize("listed_code", [None, "00941"])
def test_no_issuer_identity_cannot_bind_investee_document(listed_code):
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    assert (
        match_hkex_in_kind_notice(
            source("jd_declaration"),
            action,
            published_at=date(2021, 12, 23),
            source_url=SOURCES["jd_declaration"]["url"],
            listed_code=listed_code,
        )
        is None
    )


def test_unofficial_or_late_or_wrong_event_notice_is_not_evidence():
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    with pytest.raises(ValueError, match="official HTTPS"):
        match_hkex_in_kind_notice(
            source("jd_declaration"),
            action,
            published_at=date(2021, 12, 23),
            source_url="https://evil.example/notice.pdf",
            listed_code="00700",
        )
    for published, text in [
        (date(2022, 1, 21), source("jd_declaration")),
        (date(2021, 12, 23), source("meituan_declaration")),
    ]:
        assert (
            match_hkex_in_kind_notice(
                text,
                action,
                published_at=published,
                source_url=SOURCES["jd_declaration"]["url"],
                listed_code="00700",
            )
            is None
        )


def test_explicit_ex_date_cannot_be_shifted_within_record_window():
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    action.ex_date = date(2022, 1, 21)
    assert (
        match_hkex_in_kind_notice(
            source("jd_declaration"),
            action,
            published_at=date(2021, 12, 23),
            source_url=SOURCES["jd_declaration"]["url"],
            listed_code="00700",
        )
        is None
    )


def test_declared_terms_survive_cache_round_trip_without_inventing_cash(tmp_path):
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2022-01-19", "2022-01-20"]),
            "volume": [100, 100],
            "tradable": [True, True],
            "qfq_factor": [1, 1],
            **{
                f"{kind}_{field}": [100, 100]
                for kind in ("raw", "qfq")
                for field in ("open", "high", "low", "close")
            },
        }
    )
    external = pd.DataFrame(
        {
            "date": prices["date"],
            "raw_close": [10.0, 11.0],
            "raw_low": [9.0, 10.0],
            "fx_rate": [1.0, 1.0],
            "tradable": [True, True],
            "external_cash_dividend": [0.0, 0.0],
            "external_share_multiplier": [1.0, 1.0],
        }
    )
    bundle = PriceHistoryBundle(
        code="00700",
        prices=prices,
        actions=[action],
        currency="HKD",
        external_prices={"09618": external},
    )
    store = PointInTimeMarketStore(tmp_path)
    store.write(bundle)
    loaded = store.read("00700")
    assert loaded.actions[0].in_kind_terms == action.in_kind_terms
    pd.testing.assert_frame_equal(loaded.external_prices["09618"], external)
    assert corporate_action_issues(loaded.actions)


def test_schema_does_not_default_zero_fees_or_eligibility():
    terms = InKindDistributionTerms()
    assert terms.fee_per_asset_share is None
    assert terms.withholding_per_asset_share is None
    assert terms.minimum_cash_payment is None
    assert terms.eligible is None


def test_hkex_provider_binds_official_release_not_etnet_early_reference(monkeypatch):
    from src.data.hk_corporate_actions import HkexDividendEvidenceProvider

    provider = HkexDividendEvidenceProvider()
    raw = CorporateAction(
        code="00700",
        action_type="in_kind_distribution",
        ex_date=date(2018, 12, 28),
        published_at=date(2018, 12, 3),
        source="etnet_dividend_history",
        diagnostics=["etnet_announcement_date=2018-12-03"],
    )
    official = SOURCES["tme_declaration"]
    calls = []

    def announcements(code, start, end, title):
        calls.append(title)
        return (
            [
                {
                    "url": official["url"],
                    "published_at": date(2018, 12, 13),
                    "listed_code": "00700",
                }
            ]
            if title == "distribution"
            else []
        )

    monkeypatch.setattr(provider, "_announcements", announcements)
    monkeypatch.setattr(
        provider, "_cached_notice", lambda url: source("tme_declaration")
    )
    (resolved,) = provider.refine([raw])
    assert resolved.published_at == date(2018, 12, 13)
    assert resolved.in_kind_terms.evidence_published_at == date(2018, 12, 13)
    assert raw.in_kind_terms is None
    assert len(calls) <= 4
    assert corporate_action_issues([resolved])


def receipt_bytes(**updates):
    fields = {
        "eligible": True,
        "account_election": "shares",
        "actual_delivery_date": "2022-03-28",
        "actual_delivery_published_at": "2022-03-28",
        "fee_per_asset_share": 0,
        "withholding_per_asset_share": 0,
        "disposal_policy": "hold",
        "cash_in_lieu_per_asset_share": 100.7,
        "cash_in_lieu_payment_date": "2022-04-04",
        "cash_in_lieu_published_at": "2022-04-04",
    }
    fields.update(updates)
    return json.dumps(
        {
            "schema": "in-kind-settlement-v1",
            "code": "00700",
            "ex_date": "2022-01-20",
            "asset_code": "09618",
            "source_url": "fixture:explicit-synthetic-unit-receipt",
            "terms": fields,
        }
    ).encode()


def test_explicit_receipt_import_binds_bytes_without_mutating_declaration():
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    content = receipt_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    result = verify_settlement_evidence(content, checksum, action)
    assert result.in_kind_terms.delivery_source_sha256 == checksum
    assert result.in_kind_terms.cash_in_lieu_source_sha256 == checksum
    assert corporate_action_issues([result]) == ()
    assert action.in_kind_terms.actual_delivery_date is None


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"actual_delivery_date": "2022-01-19"}, "inconsistent"),
        ({"actual_delivery_published_at": "2022-04-04"}, "inconsistent"),
        ({"cash_in_lieu_payment_date": "2022-01-19"}, "inconsistent"),
        ({"fee_per_asset_share": -1}, "invalid settlement"),
        ({"withholding_per_asset_share": float("nan")}, "invalid settlement"),
        ({"shares_per_parent_share": 1}, "cannot change declaration"),
    ],
)
def test_inconsistent_receipt_cannot_change_causal_or_declared_facts(changes, reason):
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    content = receipt_bytes(**changes)
    with pytest.raises(ValueError, match=reason):
        verify_settlement_evidence(content, hashlib.sha256(content).hexdigest(), action)


def test_receipt_byte_and_event_identity_must_match():
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_settlement_evidence(receipt_bytes(), "0" * 64, action)
    value = json.loads(receipt_bytes())
    value["asset_code"] = "03690"
    content = json.dumps(value).encode()
    with pytest.raises(ValueError, match="instrument/event mismatch"):
        verify_settlement_evidence(content, hashlib.sha256(content).hexdigest(), action)


def test_partial_receipt_adds_proven_delivery_but_keeps_cash_and_fees_unknown():
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    value = json.loads(receipt_bytes())
    value["terms"] = {
        "actual_delivery_date": "2022-03-28",
        "actual_delivery_published_at": "2022-03-28",
    }
    content = json.dumps(value).encode()
    result = verify_settlement_evidence(
        content, hashlib.sha256(content).hexdigest(), action
    )
    assert result.in_kind_terms.actual_delivery_date == date(2022, 3, 28)
    assert result.in_kind_terms.fee_per_asset_share is None
    assert result.in_kind_terms.cash_in_lieu_per_asset_share is None
    assert corporate_action_issues([result])


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"source_sha256": "x"}, "invalid source_sha256"),
        ({"ratio_denominator": 20}, "numerator/denominator conflict"),
        ({"evidence_published_at": date(2021, 12, 24)}, "publication dates conflict"),
        ({"source_adjustment_factor": float("inf")}, "finite and positive"),
        (
            {
                "source_adjustment_factor": 0.9,
                "adjustment_published_at": date(2022, 1, 21),
            },
            "causal source evidence",
        ),
    ],
)
def test_term_consistency_checks_reject_conflicting_hash_ratio_or_adjustment(
    updates, reason
):
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    for key, value in updates.items():
        setattr(action.in_kind_terms, key, value)
    assert any(reason in issue for issue in corporate_action_issues([action]))


def test_explicit_import_entry_preserves_prices_other_actions_and_preview(tmp_path):
    from scripts.import_in_kind_settlement import import_settlement

    original = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    raw = original.copy(deep=True)
    raw.in_kind_terms = None
    dates = pd.to_datetime(["2022-01-19", "2022-01-20"])
    prices = pd.DataFrame(
        {
            "date": dates,
            "volume": [1, 1],
            "tradable": [True, True],
            "qfq_factor": [1.0, 1.0],
            **{
                f"{kind}_{field}": [100.0, 100.0]
                for kind in ("raw", "qfq")
                for field in ("open", "high", "low", "close")
            },
        }
    )
    other = CorporateAction(
        code="00700",
        action_type="cash_dividend",
        ex_date=date(2022, 1, 19),
        published_at=date(2022, 1, 1),
        cash_per_share=1,
        share_multiplier=1,
        currency="HKD",
        source="fixture:cash",
    )
    store = PointInTimeMarketStore(tmp_path / "store")
    paths = store.write(
        PriceHistoryBundle(
            code="00700",
            prices=prices,
            actions=[other, raw],
            currency="HKD",
        )
    )
    before = {name: path.read_bytes() for name, path in paths.items()}
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(receipt_bytes())
    decl = tmp_path / "declaration.json"
    decl.write_text(original.json(), encoding="utf-8")
    kwargs = {
        "store_root": store.root,
        "code": "00700",
        "ex_date": date(2022, 1, 20),
        "receipt_path": receipt,
        "expected_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "declaration_json": decl,
        "declaration_source": FIXTURES / "jd_declaration.txt",
    }
    preview = import_settlement(**kwargs)
    assert preview["action_executable"] and not preview["applied"]
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    result = import_settlement(**kwargs, apply=True)
    assert result["applied"] and not result["bundle_validated"]
    assert paths["prices"].read_bytes() == before["prices"]
    assert paths["metadata"].read_bytes() == before["metadata"]
    loaded = store.read("00700")
    assert loaded.actions[0] == other
    assert loaded.actions[1].in_kind_terms.actual_delivery_date == date(2022, 3, 28)
    assert Path(result["backup_path"]).read_bytes() == before["actions"]
    assert corporate_action_issues(loaded.actions) == ()
    # An event for another code cannot be routed into this cache.
    with pytest.raises((ValueError, FileNotFoundError)):
        import_settlement(**{**kwargs, "code": "00941"}, apply=True)


def estimated_bundle(action, start, end, *, code="00700", close=100.0):
    dates = pd.bdate_range(start, end)
    prices = pd.DataFrame(
        {
            "date": dates,
            "volume": 1,
            "tradable": True,
            "qfq_factor": 1.0,
            **{
                f"{kind}_{field}": close if field != "low" else close - 1
                for kind in ("raw", "qfq")
                for field in ("open", "high", "low", "close")
            },
        }
    )
    return PriceHistoryBundle(
        code=code,
        prices=prices,
        actions=[action] if action else [],
        currency="HKD",
    )


def estimate_execution(monkeypatch):
    from types import SimpleNamespace

    import src.search.config

    execution = SimpleNamespace(withholding_rates={"hk": 0.20}, commission_rate=0.005)
    monkeypatch.setattr(
        src.search.config,
        "get_market_optimizer_config",
        lambda market, config: SimpleNamespace(execution=execution),
    )


def test_disabled_estimation_does_not_change_or_fetch_input():
    from src.data.in_kind_estimation import resolve_in_kind_estimates

    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    bundle = estimated_bundle(action, "2022-01-19", "2022-04-20")

    def no_fetch(*args):
        raise AssertionError("disabled estimation must not fetch")

    assert resolve_in_kind_estimates(bundle, {}, no_fetch) is bundle


def test_authorized_stock_connect_estimate_keeps_actual_receipt_unknown(monkeypatch):
    from src.data.in_kind_estimation import resolve_in_kind_estimates

    estimate_execution(monkeypatch)
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    bundle = estimated_bundle(action, "2022-01-19", "2022-04-20")
    external = estimated_bundle(
        None, "2022-01-10", "2022-04-20", code="09618", close=201.0
    )
    calls = []

    def loader(code, start, end):
        calls.append(code)
        return external

    config = {
        "point_in_time_data": {
            "market_history": {"in_kind_estimation": {"enabled": True}}
        }
    }
    resolved = resolve_in_kind_estimates(bundle, config, loader)
    terms = resolved.actions[0].in_kind_terms
    assert corporate_action_issues(resolved.actions) == ()
    assert calls == ["09618"]
    assert terms.settlement_basis == "stock_connect_estimate"
    assert terms.execution_delivery_date == date(2022, 3, 29)
    assert terms.execution_withholding_per_asset_share == pytest.approx(40.2)
    assert terms.execution_fee_per_asset_share == 1.0
    assert terms.execution_cash_in_lieu_gross_per_asset_share == 201.0
    assert terms.execution_cash_in_lieu_per_asset_share == pytest.approx(160.8)
    assert (
        terms.execution_cash_in_lieu_published_at
        == terms.execution_cash_in_lieu_payment_date
    )
    assert terms.actual_delivery_date is None
    assert terms.delivery_source_sha256 is None
    assert terms.cash_in_lieu_per_asset_share is None
    assert terms.cash_in_lieu_source_sha256 is None
    assert bundle.actions[0].in_kind_terms.settlement_basis == "confirmed"
    assert "09618" in resolved.external_prices


def test_stock_connect_tiered_cash_uses_no_us_price_or_fake_delivery(monkeypatch):
    from src.backtest.in_kind import tiered_allocation
    from src.data.in_kind_estimation import resolve_in_kind_estimates

    estimate_execution(monkeypatch)
    action = declaration("tme_declaration", date(2018, 12, 13), date(2018, 12, 28))
    bundle = estimated_bundle(action, "2018-12-27", "2019-03-01")

    def no_fetch(*args):
        raise AssertionError("cash election has no foreign-security holding")

    config = {
        "point_in_time_data": {
            "market_history": {"in_kind_estimation": {"enabled": True}}
        }
    }
    resolved = resolve_in_kind_estimates(bundle, config, no_fetch)
    terms = resolved.actions[0].in_kind_terms
    assert corporate_action_issues(resolved.actions) == ()
    assert terms.eligible is True and terms.account_election == "cash"
    assert terms.execution_cash_settlement_date == date(2019, 2, 22)
    assert terms.execution_cash_settlement_withholding_rate == 0.20
    assert terms.execution_cash_settlement_withholding_amount == 0.0
    assert terms.execution_cash_fx_rate == 1.0
    assert terms.actual_delivery_date is None and terms.execution_delivery_date is None
    assert terms.cash_settlement_source_sha256 is None
    assert terms.execution_allocation_rule == "nominee_pro_rata_cash"
    assert terms.execution_nominee_cash_per_parent_share == pytest.approx(101 / 3900)
    assert terms.execution_minimum_cash_payment == 0
    assert terms.minimum_cash_payment == 39
    assert tiered_allocation(3900, terms) == (0.0, 101.0)
    assert tiered_allocation(1499, terms) == (0.0, 0.0)


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None])
def test_estimation_activation_requires_explicit_boolean(enabled):
    from src.data.in_kind_estimation import resolve_in_kind_estimates

    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    bundle = estimated_bundle(action, "2022-01-19", "2022-04-20")
    config = {
        "point_in_time_data": {
            "market_history": {"in_kind_estimation": {"enabled": enabled}}
        }
    }
    with pytest.raises(ValueError, match="must be a boolean"):
        resolve_in_kind_estimates(bundle, config, lambda *args: None)


def test_real_jd_no_dividend_decisions_prove_history_before_first_cash_payment():
    from src.data.hk_corporate_actions import parse_etnet_dividend_html

    content = (FIXTURES / "jd_etnet_dividend.html").read_bytes()
    metadata = json.loads(
        (FIXTURES / "jd_etnet_source.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
    # The first paid dividend was May 2022. Earlier dated explicit no-dividend
    # rows establish coverage; raw price changes are never used as evidence.
    assert (
        parse_etnet_dividend_html(
            content,
            "09618",
            date(2022, 1, 10),
            date(2022, 4, 20),
            source_url=metadata["url"],
        )
        == []
    )


def cached_nominee_estimate(monkeypatch):
    from src.data.in_kind_estimation import resolve_in_kind_estimates

    estimate_execution(monkeypatch)
    action = declaration("tme_declaration", date(2018, 12, 13), date(2018, 12, 28))
    bundle = estimated_bundle(action, "2018-12-27", "2019-03-01")
    config = {
        "point_in_time_data": {
            "market_history": {"in_kind_estimation": {"enabled": True}}
        }
    }
    resolved = resolve_in_kind_estimates(bundle, config, lambda *args: None)
    return resolved, config


def test_same_cached_policy_is_accepted_without_changing_values(monkeypatch):
    from src.data.in_kind_estimation import (
        current_in_kind_estimation_policy_id,
        validate_cached_in_kind_estimates,
    )

    bundle, config = cached_nominee_estimate(monkeypatch)
    before = bundle.actions[0].json()
    prices = bundle.prices.copy(deep=True)
    assert (
        current_in_kind_estimation_policy_id(config)
        == bundle.actions[0].in_kind_terms.estimation_policy_id
    )
    assert validate_cached_in_kind_estimates(bundle, config) is bundle
    assert bundle.actions[0].json() == before
    pd.testing.assert_frame_equal(bundle.prices, prices)


@pytest.mark.parametrize(
    "config",
    [
        {},
        {
            "point_in_time_data": {
                "market_history": {"in_kind_estimation": {"enabled": False}}
            }
        },
    ],
)
def test_disabled_policy_cannot_be_reenabled_by_estimated_cache(monkeypatch, config):
    from src.data.in_kind_estimation import validate_cached_in_kind_estimates

    bundle, _ = cached_nominee_estimate(monkeypatch)
    with pytest.raises(ValueError, match="estimated policy disabled; reprepare"):
        validate_cached_in_kind_estimates(bundle, config)


def test_changed_lag_rejects_cache_even_when_this_event_dates_and_prices_are_same(
    monkeypatch,
):
    from copy import deepcopy

    from src.data.in_kind_estimation import (
        resolve_in_kind_estimates,
        validate_cached_in_kind_estimates,
    )

    bundle, config = cached_nominee_estimate(monkeypatch)
    changed = deepcopy(config)
    # This field affects linear distributions, not this nominee-cash event;
    # coincident execution values cannot make two different policies identical.
    changed["point_in_time_data"]["market_history"]["in_kind_estimation"][
        "cash_lag_sessions"
    ] = 11
    new = resolve_in_kind_estimates(bundle, changed, lambda *args: None)
    assert (
        new.actions[0].in_kind_terms.execution_cash_settlement_date
        == bundle.actions[0].in_kind_terms.execution_cash_settlement_date
    )
    pd.testing.assert_frame_equal(new.prices, bundle.prices)
    with pytest.raises(ValueError, match="estimated policy changed; reprepare"):
        validate_cached_in_kind_estimates(bundle, changed)


def test_changed_execution_tax_rejects_old_cache_without_a_price_change(monkeypatch):
    from types import SimpleNamespace

    import src.search.config
    from src.data.in_kind_estimation import validate_cached_in_kind_estimates

    bundle, config = cached_nominee_estimate(monkeypatch)
    original = bundle.actions[0].json()
    monkeypatch.setattr(
        src.search.config,
        "get_market_optimizer_config",
        lambda market, config: SimpleNamespace(
            execution=SimpleNamespace(
                withholding_rates={"hk": 0.10},
                commission_rate=0.005,
            )
        ),
    )
    with pytest.raises(ValueError, match="estimated policy changed; reprepare"):
        validate_cached_in_kind_estimates(bundle, config)
    assert bundle.actions[0].json() == original


def test_changed_public_policy_definition_invalidates_estimated_cache(monkeypatch):
    import src.data.in_kind_estimation as module

    bundle, config = cached_nominee_estimate(monkeypatch)
    parse = module.yaml.safe_load

    def changed_definition(text):
        policy = parse(text)
        policy["delivery_lag_sessions"] += 1
        return policy

    monkeypatch.setattr(module.yaml, "safe_load", changed_definition)
    with pytest.raises(ValueError, match="estimated policy changed; reprepare"):
        module.validate_cached_in_kind_estimates(bundle, config)


def test_source_only_bundle_does_not_require_estimation_configuration():
    from src.data.in_kind_estimation import validate_cached_in_kind_estimates

    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    bundle = estimated_bundle(action, "2022-01-19", "2022-04-20")
    assert validate_cached_in_kind_estimates(bundle, {}) is bundle


def estimated_jd_receipt_input(monkeypatch):
    from src.data.in_kind_estimation import resolve_in_kind_estimates

    estimate_execution(monkeypatch)
    action = declaration("jd_declaration", date(2021, 12, 23), date(2022, 1, 20))
    original = estimated_bundle(action, "2022-01-19", "2022-04-20")
    source = estimated_bundle(
        None, "2022-01-10", "2022-04-20", code="09618", close=201.0
    )
    config = {
        "point_in_time_data": {
            "market_history": {"in_kind_estimation": {"enabled": True}}
        }
    }
    return resolve_in_kind_estimates(original, config, lambda *args: source)


@pytest.mark.parametrize("changes", [{}, {"actual_delivery_date": "2022-01-19"}])
def test_actual_receipt_cannot_silently_update_an_estimated_event(monkeypatch, changes):
    action = estimated_jd_receipt_input(monkeypatch).actions[0]
    before = action.json()
    content = receipt_bytes(**changes)
    with pytest.raises(ValueError, match="requires explicit confirmed reconciliation"):
        verify_settlement_evidence(content, hashlib.sha256(content).hexdigest(), action)
    assert action.json() == before


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"actual_delivery_date": "2022-01-19"}, "inconsistent"),
        ({"actual_delivery_published_at": "2022-04-04"}, "inconsistent"),
        ({"fee_per_asset_share": -1.0}, "invalid settlement"),
        ({"withholding_per_asset_share": float("nan")}, "invalid settlement"),
        ({"cash_fx_rate": 0.0}, "invalid settlement"),
        ({"cash_in_lieu_gross_per_asset_share": 99.0}, "net cash exceeds gross"),
    ],
)
def test_reconciled_receipt_validates_actual_facts_without_estimated_fallback(
    monkeypatch,
    changes,
    reason,
):
    action = estimated_jd_receipt_input(monkeypatch).actions[0]
    before = action.json()
    content = receipt_bytes(**changes)
    with pytest.raises(ValueError, match=reason):
        verify_settlement_evidence(
            content,
            hashlib.sha256(content).hexdigest(),
            action,
            reconcile_estimated=True,
        )
    assert action.json() == before


def test_complete_reconciliation_uses_actual_execution_and_discards_estimate(
    monkeypatch,
):
    action = estimated_jd_receipt_input(monkeypatch).actions[0]
    assert action.in_kind_terms.execution_delivery_date == date(2022, 3, 29)
    content = receipt_bytes()
    result = verify_settlement_evidence(
        content,
        hashlib.sha256(content).hexdigest(),
        action,
        reconcile_estimated=True,
    )
    terms = result.in_kind_terms
    assert terms.settlement_basis == "confirmed"
    assert terms.execution_delivery_date == date(2022, 3, 28)
    assert terms.actual_delivery_date == date(2022, 3, 28)
    assert terms.execution_cash_in_lieu_payment_date == date(2022, 4, 4)
    assert terms.estimated_delivery_date is None
    assert terms.estimation_policy_id is None
    assert terms.estimation_assumptions == []
    assert terms.estimation_sources == []
    assert terms.source_sha256 == action.in_kind_terms.source_sha256
    assert corporate_action_issues([result]) == ()
    assert action.in_kind_terms.settlement_basis == "stock_connect_estimate"


def test_partial_actual_reconciliation_is_confirmed_but_non_executable(monkeypatch):
    action = estimated_jd_receipt_input(monkeypatch).actions[0]
    # A previously imported or hand-populated execution value on an estimated
    # object must not be promoted to a confirmed fact during reconciliation.
    action.in_kind_terms.fee_per_asset_share = 0.0
    action.in_kind_terms.withholding_per_asset_share = 0.0
    value = json.loads(receipt_bytes())
    value["terms"] = {
        "actual_delivery_date": "2022-03-28",
        "actual_delivery_published_at": "2022-03-28",
    }
    content = json.dumps(value).encode()
    result = verify_settlement_evidence(
        content,
        hashlib.sha256(content).hexdigest(),
        action,
        reconcile_estimated=True,
    )
    terms = result.in_kind_terms
    assert terms.settlement_basis == "confirmed"
    assert terms.execution_delivery_date == date(2022, 3, 28)
    assert terms.execution_cash_in_lieu_payment_date is None
    assert terms.eligible is None and terms.account_election is None
    assert terms.disposal_policy is None
    assert terms.fee_per_asset_share is None
    assert terms.withholding_per_asset_share is None
    assert terms.estimation_policy_id is None and terms.estimated_delivery_date is None
    assert corporate_action_issues([result])


@pytest.mark.parametrize("complete", [False, True])
def test_actual_reconciliation_remains_confirmed_under_enabled_estimation(
    monkeypatch,
    complete,
):
    from dataclasses import replace

    from src.data.in_kind_estimation import resolve_in_kind_estimates

    bundle = estimated_jd_receipt_input(monkeypatch)
    value = json.loads(receipt_bytes())
    if not complete:
        value["terms"] = {
            "actual_delivery_date": "2022-03-28",
            "actual_delivery_published_at": "2022-03-28",
        }
    content = json.dumps(value).encode()
    reconciled = verify_settlement_evidence(
        content,
        hashlib.sha256(content).hexdigest(),
        bundle.actions[0],
        reconcile_estimated=True,
    )
    actual_bundle = replace(bundle, actions=[reconciled])
    before = reconciled.json()
    config = {
        "point_in_time_data": {
            "market_history": {"in_kind_estimation": {"enabled": True}}
        }
    }

    def no_fetch(*args):
        raise AssertionError(
            "actual reconciliation cannot fetch inputs to rebuild estimates"
        )

    result = resolve_in_kind_estimates(actual_bundle, config, no_fetch)
    assert result.actions[0].json() == before
    assert result.actions[0].in_kind_terms.settlement_basis == "confirmed"
    assert result.actions[0].in_kind_terms.estimation_policy_id is None
    assert bool(corporate_action_issues(result.actions)) is not complete


def test_estimated_cache_import_preview_and_apply_require_actual_reconciliation(
    monkeypatch,
    tmp_path,
):
    from scripts.import_in_kind_settlement import import_settlement

    bundle = estimated_jd_receipt_input(monkeypatch)
    store = PointInTimeMarketStore(tmp_path / "store")
    paths = store.write(bundle)
    before = {name: path.read_bytes() for name, path in paths.items()}
    receipt = tmp_path / "receipt.json"
    kwargs = {
        "store_root": store.root,
        "code": "00700",
        "ex_date": date(2022, 1, 20),
        "receipt_path": receipt,
    }
    invalid = receipt_bytes(actual_delivery_date="2022-01-19")
    receipt.write_bytes(invalid)
    checksum = hashlib.sha256(invalid).hexdigest()
    for reconcile in (False, True):
        with pytest.raises(ValueError):
            import_settlement(
                **kwargs,
                expected_sha256=checksum,
                apply=True,
                reconcile_estimated=reconcile,
            )
        assert {name: path.read_bytes() for name, path in paths.items()} == before
    value = json.loads(receipt_bytes())
    value["terms"] = {
        "actual_delivery_date": "2022-03-28",
        "actual_delivery_published_at": "2022-03-28",
    }
    content = json.dumps(value).encode()
    receipt.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    preview = import_settlement(
        **kwargs, expected_sha256=checksum, reconcile_estimated=True
    )
    assert preview["settlement_basis"] == "confirmed"
    assert preview["previous_settlement_basis"] == "stock_connect_estimate"
    assert (
        not preview["action_executable"]
        and not preview["confirmed_reconciliation_complete"]
    )
    assert not preview["applied"]
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    applied = import_settlement(
        **kwargs, expected_sha256=checksum, reconcile_estimated=True, apply=True
    )
    assert applied["applied"] and not applied["action_executable"]
    assert paths["prices"].read_bytes() == before["prices"]
    assert paths["metadata"].read_bytes() == before["metadata"]
    loaded = store.read("00700")
    assert loaded.actions[0].in_kind_terms.settlement_basis == "confirmed"
    assert loaded.actions[0].in_kind_terms.execution_delivery_date == date(2022, 3, 28)
    assert corporate_action_issues(loaded.actions)
