import copy
import json
from datetime import date

import pytest

from src.data.corporate_action_currency import (
    FixedCashValuation,
    validate_cash_currency,
)
from src.data.hk_corporate_actions import match_hkex_cash_notice
from src.data.market_history import CorporateAction


def action(**kwargs):
    return CorporateAction(
        code="09999",
        action_type="cash_dividend",
        ex_date=date(2025, 3, 5),
        published_at=date(2025, 2, 20),
        cash_per_share=0.24405,
        currency="USD",
        source="hkex_dividend_notice",
        **kwargs,
    )


def test_foreign_cash_requires_conversion():
    with pytest.raises(ValueError, match="currency mismatch"):
        validate_cash_currency(action())


@pytest.fixture
def fixed_config(tmp_path):
    import yaml

    from src.search.config import DEFAULT_APPLICATION_PATH, DEFAULT_PATH

    app = yaml.safe_load(DEFAULT_APPLICATION_PATH.read_text(encoding="utf-8"))
    raw = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    profile = app["optimizer"]["markets"]["hk"]["execution_profile"]
    raw["execution_profiles"][profile]["fx_rates"] = {
        "a_share": 1.0,
        "hk": 0.9,
        "us": 7.0,
    }
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return app, raw, profile, path


def test_fixed_conversion_preserves_original_and_never_requests_fx(
    fixed_config, monkeypatch
):
    import requests

    monkeypatch.setattr(
        requests.Session, "request", lambda *a, **kw: pytest.fail("FX network request")
    )
    app, _, _, path = fixed_config
    converter = FixedCashValuation(app, constraints_path=path)
    result = converter.convert(action())
    assert result.cash_per_share == pytest.approx(0.24405 * 7.0 / 0.9)
    assert result.declared_cash_per_share == 0.24405
    assert result.declared_currency == "USD"
    assert "rate_date" not in result.cash_conversion
    assert result.cash_conversion["source_record"]["fx_rates"] == {"us": 7.0, "hk": 0.9}
    assert "not_actual_HKD_receipt" in result.cash_conversion["basis"]
    assert converter.convert(result).dict() == result.dict()
    for field in ("cash_per_share", "rate", "source_record"):
        bad = copy.deepcopy(result)
        if field == "cash_per_share":
            bad.cash_per_share += 0.01
        elif field == "rate":
            bad.cash_conversion["rate"] += 0.01
        else:
            bad.cash_conversion["source_record"]["fx_rates"]["us"] = 8.0
        with pytest.raises(ValueError, match="valuation evidence"):
            validate_cash_currency(bad)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, None])
def test_invalid_fixed_rate_has_no_silent_default(fixed_config, value):
    import yaml

    app, raw, profile, path = fixed_config
    raw["execution_profiles"][profile]["fx_rates"]["us"] = value
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        FixedCashValuation(app, constraints_path=path).convert(action())


def test_missing_fixed_rate_has_no_silent_default(fixed_config):
    import yaml

    app, raw, profile, path = fixed_config
    del raw["execution_profiles"][profile]["fx_rates"]["us"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(KeyError):
        FixedCashValuation(app, constraints_path=path).convert(action())


def test_rate_change_revalues_original_and_old_hkma_conversion(fixed_config):
    import yaml

    app, raw, profile, path = fixed_config
    old = FixedCashValuation(app, constraints_path=path).convert(action())
    old.cash_conversion["method"] = "declared-cash+causal-hkma-valuation/1"
    old.cash_per_share = 0.24405 * 7.778
    raw["execution_profiles"][profile]["fx_rates"]["us"] = 7.2
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = FixedCashValuation(app, constraints_path=path).convert(old)
    assert result.cash_per_share == pytest.approx(0.24405 * 8.0)
    assert old.cash_per_share == pytest.approx(0.24405 * 7.778)
    assert result.declared_cash_per_share == 0.24405


def test_unknown_currency_not_guessed():
    a = action()
    a.currency = "EUR"
    with pytest.raises(ValueError, match="unsupported"):
        FixedCashValuation().convert(a)


def test_native_a_us_no_conversion():
    for code, currency in [("600000", "CNY"), ("AAPL", "USD")]:
        a = action()
        a.code = code
        a.currency = currency
        validate_cash_currency(a)
        assert a.cash_per_share == 0.24405


def test_usd_form_ordinary_share_not_ads():
    a = action()
    a.published_at = None
    text = "Stock code 09999 Cash Dividend Announcement for Equity Issuer Dividend declared USD 0.24405 pershare Ex-dividend date 05 March 2025"
    result = match_hkex_cash_notice(
        text,
        a,
        published_at=date(2025, 2, 20),
        source_url="https://www.hkexnews.hk/form.pdf",
    )
    assert result.currency == "USD" and result.cash_per_share == 0.24405
    assert (
        match_hkex_cash_notice(
            text.replace("pershare", "per ADS"),
            a,
            published_at=date(2025, 2, 20),
            source_url="https://www.hkexnews.hk/form.pdf",
        )
        is None
    )


def test_execution_refuses_foreign_currency():
    from src.backtest.execution import build_corporate_action_schedule

    with pytest.raises(ValueError, match="currency mismatch"):
        build_corporate_action_schedule([action()], ["2025-03-05"], ["09999"])


def test_usd_form_future_announcement_still_rejected():
    a = action()
    text = "Stock code 09999 Cash Dividend Announcement for Equity Issuer Announcement date 06 March 2025 Dividend declared USD 0.24405 pershare Ex-dividend date 05 March 2025"
    with pytest.raises(ValueError, match="predates"):
        match_hkex_cash_notice(
            text,
            a,
            published_at=date(2025, 2, 20),
            source_url="https://www.hkexnews.hk/form.pdf",
        )


@pytest.mark.parametrize(
    "filename,ex_day,pub_day,amount,payment",
    [
        ("2025022000534.txt", "2025-03-05", "2025-02-20", 0.24405, "2025-03-17"),
        ("2020111900807.txt", "2020-12-03", "2020-11-19", 0.039, "2020-12-15"),
        ("prospectus_page_38.txt", "2020-06-11", "2020-06-02", 0.0464, "2020-06-23"),
    ],
)
def test_official_usd_disclosures_replay(filename, ex_day, pub_day, amount, payment):
    from pathlib import Path

    source = Path(__file__).parent / "fixtures/hk_dividends" / filename
    a = action()
    a.ex_date = date.fromisoformat(ex_day)
    a.cash_per_share = amount
    a.payable_date = date.fromisoformat(payment)
    a.published_at = None
    result = match_hkex_cash_notice(
        source.read_text(encoding="utf-8"),
        a,
        published_at=date.fromisoformat(pub_day),
        source_url=next(
            row["url"]
            for row in json.loads(
                (source.parent / "sources.json").read_text(encoding="utf-8")
            )["sources"]
            if row["fixture"] == filename
        ),
        listed_code="09999",
    )
    assert (
        result is not None
        and result.cash_per_share == amount
        and result.currency == "USD"
    )
    assert result.published_at == date.fromisoformat(pub_day)


def test_cached_bundle_revalues_both_cash_and_qfq_without_disk_rewrite(tmp_path):
    import pandas as pd

    from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle
    from src.data.price_adjustments import apply_disclosed_adjustments

    days = pd.to_datetime(["2025-03-04", "2025-03-05"])
    frame = pd.DataFrame({"date": days, "volume": [100, 100], "tradable": [True, True]})
    for prefix in ("raw", "qfq"):
        for field in ("open", "high", "low", "close"):
            frame[f"{prefix}_{field}"] = [100.0, 100.0]
    frame["qfq_factor"] = 1.0
    bundle = PriceHistoryBundle(
        code="09999", prices=frame, actions=[], currency="HKD", source="fixture"
    )
    converted = FixedCashValuation().convert(action())
    bundle = apply_disclosed_adjustments(
        bundle, [converted], evidence="fixture_complete_disclosures"
    )
    store = PointInTimeMarketStore(tmp_path)
    paths = store.write(bundle)
    payload = json.loads(paths["actions"].read_text(encoding="utf-8"))
    payload[0]["cash_per_share"] = 0.24405 * 7.778
    payload[0]["cash_conversion"]["method"] = "declared-cash+causal-hkma-valuation/1"
    paths["actions"].write_text(json.dumps(payload), encoding="utf-8")
    before = {k: p.read_bytes() for k, p in paths.items()}
    result = store.read("09999")
    cash = FixedCashValuation().convert(action()).cash_per_share
    assert result.actions[0].cash_per_share == pytest.approx(cash)
    assert result.prices["qfq_close"].iloc[0] == pytest.approx(100.0 - cash)
    assert result.prices["qfq_close"].iloc[1] == 100.0
    assert before == {k: p.read_bytes() for k, p in paths.items()}


@pytest.mark.parametrize(
    "group,compatible", [("a_share", True), ("us", True), ("hk", False)]
)
def test_previous_hkma_version_only_preserves_unchanged_native_markets(
    group, compatible
):
    from src.search.artifacts import execution_contract_compatibility

    old = {
        "execution_contracts": {
            "currency_cash_version": "declared-cash+causal-hkma-valuation/1"
        }
    }
    assert execution_contract_compatibility(old, group)["compatible"] is compatible
    assert (
        execution_contract_compatibility(
            {"execution_contracts": {"currency_cash_version": "future/999"}}, group
        )["compatible"]
        is False
    )
