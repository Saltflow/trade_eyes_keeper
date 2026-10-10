"""HKEX source replay and strict cross-market IPO coverage; no live test calls."""

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data import listing_dates as ld
from src.data.backtest_data import prepare_backtest_data, validate_market_bundle
from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle

FIXTURE = Path(__file__).parent / "fixtures/listing_dates/hkex_2020_page61.txt"
META = json.loads(FIXTURE.with_suffix(".source.json").read_text(encoding="utf8"))


def evidence(code="09999"):
    parsed = ld._parse_hkex_listing_page(code, FIXTURE.read_text(encoding="utf8"), 2020)
    return ld.ListingDateEvidence(
        code=code,
        source_url=META["source_url"],
        source_sha256=META["pdf_sha256"],
        source_page=META["pdf_page"],
        retrieved_at="2026-10-10T00:00:00+00:00",
        method=ld.HKEX_LISTING_METHOD,
        **parsed,
    ).validate(code)


def bundle():
    frame = pd.DataFrame({"date": pd.to_datetime(["2020-06-11", "2020-06-12"])})
    for col in ("open", "high", "low", "close"):
        frame[f"raw_{col}"] = frame[f"qfq_{col}"] = 123.0
    frame["qfq_factor"] = 1.0
    frame["volume"] = 1000
    frame["tradable"] = True
    return PriceHistoryBundle(code="09999", prices=frame, currency="HKD", source="test")


def test_actual_hkex_page_matches_exact_company_code_and_date():
    result = evidence()
    assert result.company_name == "NetEase, Inc. – S"
    assert result.listing_date == date(2020, 6, 11)
    assert evidence("09618").listing_date == date(2020, 6, 18)
    assert evidence("09618").company_name == "JD.com, Inc. – SW"
    assert (
        ld._parse_hkex_listing_page("09998", FIXTURE.read_text(encoding="utf8"), 2020)
        is None
    )
    assert (
        ld._parse_hkex_listing_page("06812", FIXTURE.read_text(encoding="utf8"), 2020)
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"code": "09998"},
        {"company_name": "Different Company"},
        {"listing_date": date(2020, 6, 12)},
        {"source_url": "https://www.hkex.com.hk.evil.test/FB_2020.pdf"},
        {"source_url": ld.SSE_STOCK_URL},
        {"source_sha256": "missing"},
        {"source_page": 0},
        {"method": "first_price_bar"},
    ],
)
def test_hk_cached_evidence_rejects_identity_source_date_or_provenance_mutation(change):
    with pytest.raises(ValueError):
        replace(evidence(), **change).validate("09999")


def test_table_header_and_unique_actual_listing_date_are_required():
    text = FIXTURE.read_text(encoding="utf8")
    row = evidence().source_quote
    with pytest.raises(ValueError, match="conflicting rows"):
        ld._parse_hkex_listing_page("09999", text + "\n" + row, 2020)
    with pytest.raises(ValueError, match="ambiguous date"):
        ld._parse_hkex_listing_page("09999", text, 2021)
    assert ld._parse_hkex_listing_page("09999", row, 2020) is None
    assert (
        ld._parse_hkex_listing_page("09999", "Proposed listing\n" + row, 2020) is None
    )


def test_hk_route_cache_reuses_verified_result_and_not_ashare(tmp_path, monkeypatch):
    calls = []

    def fetch(code, session, year, root):
        calls.append((code, year))
        return evidence(code)

    monkeypatch.setattr(ld, "_hkex_stock", fetch)
    monkeypatch.setattr(ld, "_szse_stock", lambda *_: pytest.fail("HK routed to SZSE"))
    store = ld.ListingDateStore(tmp_path)
    assert store.resolve("09999", listing_year=2020) == evidence()
    assert store.resolve("09999") == evidence()
    assert calls == [("09999", 2020)]
    cache = tmp_path / "listing_dates/hk/09999.json"
    payload = json.loads(cache.read_text(encoding="utf8"))
    payload["listing_date"] = "2020-06-12"
    cache.write_text(json.dumps(payload), encoding="utf8")
    with pytest.raises(ValueError, match="code/company/date"):
        store.read("09999")


def test_unsupported_us_route_is_specific_and_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ld.requests, "Session", lambda: pytest.fail("unexpected network")
    )
    with pytest.raises(ValueError, match="no official us listing-date evidence route"):
        ld.ListingDateStore(tmp_path).resolve("ABNB")


def test_hk_history_still_requires_exact_verified_listing_day():
    source = bundle()
    with pytest.raises(ValueError, match="official listing evidence required"):
        validate_market_bundle(source, "09999", date(2018, 1, 1), date(2020, 6, 12))
    source.listing_evidence = evidence().as_dict()
    assert validate_market_bundle(source, "09999", date(2018, 1, 1), date(2020, 6, 12))
    source.prices = source.prices.iloc[1:]
    with pytest.raises(ValueError, match="official listing date"):
        validate_market_bundle(source, "09999", date(2018, 1, 1), date(2020, 6, 12))


@pytest.mark.parametrize("cached", [False, True])
def test_backtest_hk_short_history_uses_year_as_lookup_hint_then_persists_evidence(
    tmp_path, monkeypatch, cached
):
    source = bundle()
    if cached:
        PointInTimeMarketStore(tmp_path).write(source)
    monkeypatch.setattr(
        "src.data.backtest_data.resolve_market_data_cutoff",
        lambda *_args, **_kwargs: SimpleNamespace(
            effective_end=date(2020, 6, 12), as_dict=dict
        ),
    )

    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, *_args):
            assert not cached
            return source

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    calls = []

    def resolve(_self, code, *, listing_year):
        calls.append((code, listing_year))
        return evidence()

    monkeypatch.setattr(ld.ListingDateStore, "resolve", resolve)
    result = prepare_backtest_data(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        ["09999"],
        "2018-04-13",
        "2020-06-12",
        purpose="optimizer",
        as_of=datetime(2020, 6, 12, tzinfo=timezone.utc),
    )
    assert result.ready, result.issues
    assert calls == [("09999", 2020)]
    assert (
        PointInTimeMarketStore(tmp_path).read("09999").listing_evidence
        == evidence().as_dict()
    )


def test_original_hkex_pdf_replay_if_available(tmp_path):
    original = Path("data/source_validation/hkex_listing/FB_2020.pdf")
    if not original.exists():
        pytest.skip(
            "optional original HKEX download unavailable; page replay still runs"
        )
    raw = original.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == META["pdf_sha256"]
    (tmp_path / "hkex_factbook_2020.pdf").write_bytes(raw)
    (tmp_path / "hkex_factbook_2020.receipt.json").write_text(
        json.dumps({"source_url": META["source_url"], "sha256": META["pdf_sha256"]}),
        encoding="utf8",
    )
    session = SimpleNamespace(
        get=lambda *_args, **_kwargs: pytest.fail("must reuse PDF")
    )
    result = ld._hkex_stock("09999", session, 2020, tmp_path)
    assert result.listing_date == date(2020, 6, 11)
    assert result.source_page == 61
    assert result.company_name == evidence().company_name


def test_disclosed_hk_provider_checks_listing_before_corporate_action_lookup(
    tmp_path, monkeypatch
):
    from src.data.disclosed_market_history import DisclosedMarketHistoryProvider

    observed = {}
    monkeypatch.setattr(
        "src.data.tencent_market_history.TencentMarketHistoryProvider.fetch",
        lambda _self, code, start, end: bundle(),
    )

    def resolve(_self, code, *, listing_year):
        observed.update(code=code, lookup_year=listing_year)
        return evidence()

    monkeypatch.setattr(ld.ListingDateStore, "resolve", resolve)

    def actions(_self, code, start, end):
        observed["action_start"] = start
        return []

    monkeypatch.setattr(
        "src.data.hk_corporate_actions.HkCorporateActionProvider.fetch", actions
    )
    monkeypatch.setattr(
        "src.data.disclosed_market_history.apply_disclosed_adjustments",
        lambda source, _actions, *, evidence: source,
    )
    result = DisclosedMarketHistoryProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    ).fetch("09999", date(2018, 1, 1), date(2020, 6, 12))
    assert observed == {
        "code": "09999",
        "lookup_year": 2020,
        "action_start": date(2020, 6, 11),
    }
    assert result.listing_evidence["listing_date"] == "2020-06-11"
