"""Official listing-date evidence and IPO-aware strict market coverage."""

import json
from datetime import date
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from src.data.backtest_data import validate_market_bundle
from src.data.listing_dates import (
    SSE_ELIGIBLE_URL,
    SSE_FUND_QUERY_URL,
    SSE_FUND_URL,
    SSE_STOCK_URL,
    SZSE_FUND_URL,
    SZSE_STOCK_URL,
    ListingDateEvidence,
    ListingDateStore,
    _sse_fund,
    _sse_stock,
    _szse_fund,
    _szse_stock,
)
from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle


def _response(url, *, payload=None, content=b""):
    return SimpleNamespace(
        url=url,
        content=content,
        json=lambda: payload,
        raise_for_status=lambda: None,
    )


def _bundle(code="600150"):
    frame = pd.DataFrame({"date": pd.to_datetime(["2020-01-02", "2020-01-03"])})
    for column in ("open", "high", "low", "close"):
        frame[f"raw_{column}"] = 10.0
        frame[f"qfq_{column}"] = 10.0
    frame["qfq_factor"] = 1.0
    frame["volume"] = 1000
    frame["tradable"] = True
    return PriceHistoryBundle(code=code, prices=frame, source="official_test")


def _evidence(code="600150", listed=date(2020, 1, 2)):
    return ListingDateEvidence(
        code,
        listed,
        SSE_FUND_URL if code.startswith("5") else SSE_STOCK_URL,
        "2026-10-03T00:00:00+00:00",
    )


def test_sse_listing_result_must_match_exact_requested_code():
    class Session:
        def get(self, url, *, params, headers, timeout):
            assert params["STOCK_CODE"] == "600150"
            assert timeout == (30, 60)
            return _response(
                url,
                payload={
                    "result": [
                        {"A_STOCK_CODE": "600151", "LIST_DATE": "1998-05-20"}
                    ]
                },
            )

    with pytest.raises(ValueError, match="no unique official listing date"):
        _sse_stock("600150", Session())


def test_szse_listing_result_matches_zero_padded_code():
    workbook = BytesIO()
    pd.DataFrame(
        {"A股代码": [2594, 2595], "A股上市日期": ["2011-06-30", "2011-07-01"]}
    ).to_excel(workbook, index=False)

    class Session:
        def get(self, url, *, params, timeout):
            assert url == SZSE_STOCK_URL
            assert timeout == (30, 60)
            return _response(url, content=workbook.getvalue())

    listed, source = _szse_stock("002594", Session())
    assert listed == date(2011, 6, 30)
    assert source == SZSE_STOCK_URL


def test_sse_fund_listing_uses_official_eligible_table():
    class Session:
        def get(self, url, *, timeout, **kwargs):
            if url == SSE_FUND_QUERY_URL:
                assert kwargs["params"]["FUND_CODE"] == "515180"
                return _response(url, payload={"result": [], "pageHelp": {"total": 0}})
            assert url == SSE_ELIGIBLE_URL
            return SimpleNamespace(
                url=url,
                text=(
                    "<table><tr><td>515180</td><td>E Fund</td>"
                    "<td>2019/12/20</td></tr></table>"
                ),
                raise_for_status=lambda: None,
            )

    listed, source = _sse_fund("515180", Session())
    assert listed == date(2019, 12, 20)
    assert source == SSE_ELIGIBLE_URL


def test_sse_fund_listing_query_requires_exact_unique_code():
    class Session:
        def get(self, url, *, params, headers, timeout):
            assert url == SSE_FUND_QUERY_URL
            assert params["FUND_CODE"] == "588510"
            return _response(
                url,
                payload={
                    "pageHelp": {"total": 1},
                    "result": [
                        {"FUND_CODE": "588510", "LISTING_DATE": "2026-05-29"}
                    ],
                },
            )

    listed, source = _sse_fund("588510", Session())
    assert listed == date(2026, 5, 29)
    assert source == SSE_FUND_QUERY_URL


@pytest.mark.parametrize(
    "payload",
    [
        {"pageHelp": {"total": 1}, "result": [
            {"FUND_CODE": "588511", "LISTING_DATE": "2026-05-29"}
        ]},
        {"pageHelp": {"total": 2}, "result": [
            {"FUND_CODE": "588510", "LISTING_DATE": "2026-05-29"}
        ]},
    ],
)
def test_sse_fund_listing_rejects_mismatched_or_nonunique_result(payload):
    class Session:
        def get(self, url, *, params, headers, timeout):
            return _response(url, payload=payload)

    with pytest.raises(ValueError, match="no unique match"):
        _sse_fund("588510", Session())


def test_listing_cache_is_exact_and_preserves_source(tmp_path, monkeypatch):
    from src.data import listing_dates

    calls = []

    def fetch(code, _session):
        calls.append(code)
        return date(2020, 1, 2), SSE_STOCK_URL

    monkeypatch.setattr(listing_dates, "_sse_stock", fetch)
    store = ListingDateStore(tmp_path)
    assert store.resolve("600150").listing_date == date(2020, 1, 2)
    assert store.resolve("600150").source_url == SSE_STOCK_URL
    assert calls == ["600150"]
    with pytest.raises(ValueError, match="different/invalid code"):
        _evidence("600151").validate("600150")
    with pytest.raises(ValueError, match="non-official source"):
        ListingDateEvidence(
            "600150", date(2020, 1, 2), "https://evil.test/", "2026-10-03"
        ).validate("600150")


def test_listing_timeout_retries_only_official_lookup(tmp_path, monkeypatch):
    from src.data import listing_dates

    calls = []
    sleeps = []

    def fetch(code, _session):
        calls.append(code)
        if len(calls) == 1:
            raise requests.ReadTimeout("slow download")
        return date(2020, 1, 2), SSE_STOCK_URL

    monkeypatch.setattr(listing_dates, "_sse_stock", fetch)
    monkeypatch.setattr(listing_dates.time, "sleep", sleeps.append)
    result = ListingDateStore(tmp_path).resolve("600150")
    assert result.listing_date == date(2020, 1, 2)
    assert calls == ["600150", "600150"]
    assert sleeps == [2]


def test_listing_http_403_is_not_retried(tmp_path, monkeypatch):
    from src.data import listing_dates

    calls = []

    def fetch(code, _session):
        calls.append(code)
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError("forbidden", response=response)

    monkeypatch.setattr(listing_dates, "_sse_stock", fetch)
    with pytest.raises(requests.HTTPError, match="forbidden"):
        ListingDateStore(tmp_path).resolve("600150")
    assert calls == ["600150"]


def test_listing_http_429_retries_with_bounded_retry_after(tmp_path, monkeypatch):
    from src.data import listing_dates

    calls = []
    sleeps = []

    def fetch(code, _session):
        calls.append(code)
        if len(calls) == 1:
            response = requests.Response()
            response.status_code = 429
            response.headers["Retry-After"] = "120"
            raise requests.HTTPError("rate limited", response=response)
        return date(2020, 1, 2), SSE_STOCK_URL

    monkeypatch.setattr(listing_dates, "_sse_stock", fetch)
    monkeypatch.setattr(listing_dates.time, "sleep", sleeps.append)
    assert ListingDateStore(tmp_path).resolve("600150").listing_date == date(
        2020, 1, 2
    )
    assert calls == ["600150", "600150"]
    assert sleeps == [60.0]


def test_short_history_requires_real_listing_evidence_and_retains_it(tmp_path):
    bundle = _bundle()
    with pytest.raises(ValueError, match="official listing evidence required"):
        validate_market_bundle(bundle, "600150", date(2018, 1, 1), date(2020, 1, 3))
    bundle.listing_evidence = _evidence().as_dict()
    accepted = validate_market_bundle(
        bundle, "600150", date(2018, 1, 1), date(2020, 1, 3)
    )
    store = PointInTimeMarketStore(tmp_path)
    store.write(accepted)
    assert store.read("600150").listing_evidence == _evidence().as_dict()


@pytest.mark.parametrize(
    "listed", [date(2019, 1, 1), date(2020, 1, 1), date(2020, 1, 3)]
)
def test_listing_evidence_does_not_hide_missing_or_prelisting_prices(listed):
    bundle = _bundle()
    bundle.listing_evidence = _evidence(listed=listed).as_dict()
    with pytest.raises(ValueError, match="official listing date"):
        validate_market_bundle(bundle, "600150", date(2018, 1, 1), date(2020, 1, 3))


def test_disclosed_fund_checks_actions_from_verified_listing_date(
    tmp_path, monkeypatch
):
    from src.data.disclosed_market_history import DisclosedMarketHistoryProvider

    observed = {}
    monkeypatch.setattr(
        "src.data.tencent_market_history.TencentMarketHistoryProvider.fetch",
        lambda _self, code, _start, _end: _bundle(code),
    )
    monkeypatch.setattr(
        "src.data.disclosed_market_history.ListingDateStore.resolve",
        lambda _self, code: _evidence(code),
    )

    def actions(_self, code, start, end, *, market_prices):
        observed.update(code=code, start=start, end=end)
        assert len(market_prices) == 2
        return []

    monkeypatch.setattr(
        "src.data.fund_corporate_actions.SseFundCorporateActionProvider.fetch_actions",
        actions,
    )
    monkeypatch.setattr(
        "src.data.disclosed_market_history.apply_disclosed_adjustments",
        lambda bundle, _actions, *, evidence: bundle,
    )
    result = DisclosedMarketHistoryProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    ).fetch("510880", date(2018, 1, 1), date(2020, 1, 3))
    assert observed["start"] == date(2020, 1, 2)
    assert result.listing_evidence["listing_date"] == "2020-01-02"


def test_disclosed_fund_drops_prelisting_nav_rows(tmp_path, monkeypatch):
    from src.data.disclosed_market_history import DisclosedMarketHistoryProvider

    def prices(_self, code, _start, _end):
        bundle = _bundle(code)
        earlier = bundle.prices.iloc[[0]].copy()
        earlier["date"] = pd.Timestamp("2020-01-01")
        bundle.prices = pd.concat([earlier, bundle.prices], ignore_index=True)
        return bundle

    monkeypatch.setattr(
        "src.data.tencent_market_history.TencentMarketHistoryProvider.fetch",
        prices,
    )
    monkeypatch.setattr(
        "src.data.disclosed_market_history.ListingDateStore.resolve",
        lambda _self, code: _evidence(code),
    )
    monkeypatch.setattr(
        "src.data.fund_corporate_actions.SseFundCorporateActionProvider.fetch_actions",
        lambda _self, code, start, end, *, market_prices: [],
    )
    monkeypatch.setattr(
        "src.data.disclosed_market_history.apply_disclosed_adjustments",
        lambda bundle, _actions, *, evidence: bundle,
    )
    result = DisclosedMarketHistoryProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    ).fetch("510880", date(2018, 1, 1), date(2020, 1, 3))
    assert result.prices["date"].min() == pd.Timestamp("2020-01-02")
    assert result.listing_evidence["listing_date"] == "2020-01-02"


def test_disclosed_fund_rejects_gap_on_official_listing_day(tmp_path, monkeypatch):
    from src.data.disclosed_market_history import DisclosedMarketHistoryProvider

    def prices(_self, code, _start, _end):
        bundle = _bundle(code)
        earlier = bundle.prices.iloc[[0]].copy()
        earlier["date"] = pd.Timestamp("2019-12-31")
        bundle.prices = pd.concat([earlier, bundle.prices], ignore_index=True)
        return bundle

    monkeypatch.setattr(
        "src.data.tencent_market_history.TencentMarketHistoryProvider.fetch",
        prices,
    )
    monkeypatch.setattr(
        "src.data.disclosed_market_history.ListingDateStore.resolve",
        lambda _self, code: _evidence(code, date(2020, 1, 1)),
    )
    with pytest.raises(ValueError, match="no exchange price on official listing date"):
        DisclosedMarketHistoryProvider(
            {"point_in_time_data": {"output_dir": str(tmp_path)}}
        ).fetch("510880", date(2018, 1, 1), date(2020, 1, 3))

SZSE_FUND_SOURCE = json.loads(
    (
        Path(__file__).parent / "fixtures/listing_dates/szse_fund_159655.source.json"
    ).read_text("utf-8")
)


def fund_workbook(rows=None, *, drop=()):
    frame = pd.DataFrame(SZSE_FUND_SOURCE["rows"] if rows is None else rows)
    frame = frame.drop(columns=list(drop))
    workbook = BytesIO()
    frame.to_excel(workbook, index=False)
    return workbook.getvalue()


def fund_session(content=None, source_url=None):
    class Session:
        def get(self, url, *, params, headers, timeout):
            assert url == SZSE_FUND_URL
            assert params == {
                "SHOWTYPE": "xlsx",
                "CATALOGID": "1000_lf",
                "TABKEY": "tab1",
            }
            assert (
                headers["Referer"]
                == "https://fund.szse.cn/marketdata/fundslist/index.html"
            )
            assert timeout == (30, 60)
            return _response(
                source_url or SZSE_FUND_SOURCE["source_url"],
                content=fund_workbook() if content is None else content,
            )

    return Session()


def test_real_szse_fund_workbook_row_selects_listing_not_inception():
    assert SZSE_FUND_SOURCE["workbook_total_rows"] == 1083
    assert len(SZSE_FUND_SOURCE["workbook_sha256"]) == 64
    assert {"基金代码", "上市日期"}.issubset(SZSE_FUND_SOURCE["columns"])
    listed, source = _szse_fund("159655", fund_session())
    assert listed == date(2022, 10, 25)
    assert source == SZSE_FUND_SOURCE["source_url"]


@pytest.mark.parametrize(
    "mutation", ["wrong_code", "duplicate", "no_date", "fractional_code"]
)
def test_szse_fund_row_must_be_exact_unique_and_dated(mutation):
    rows = [dict(SZSE_FUND_SOURCE["rows"][0])]
    if mutation == "wrong_code":
        rows[0]["基金代码"] = 159656
    elif mutation == "duplicate":
        rows *= 2
    elif mutation == "no_date":
        rows[0]["上市日期"] = None
    else:
        rows[0]["基金代码"] = 159655.5
    with pytest.raises(ValueError, match="no unique official fund listing date"):
        _szse_fund("159655", fund_session(fund_workbook(rows)))


@pytest.mark.parametrize("oversized", [False, True])
def test_szse_fund_list_requires_bounded_actual_workbook(oversized):
    content = (
        b"PK\x03\x04" + b"x" * (10 * 1024 * 1024)
        if oversized
        else b"<html>access denied</html>"
    )
    reason = "exceeds 10 MiB" if oversized else "not an XLSX"
    with pytest.raises(ValueError, match=reason):
        _szse_fund("159655", fund_session(content))


def test_szse_fund_list_cannot_substitute_contract_effective_date():
    row = dict(SZSE_FUND_SOURCE["rows"][0])
    row["基金合同生效日"] = "2022-10-12"
    with pytest.raises(ValueError, match="listing-date columns"):
        _szse_fund("159655", fund_session(fund_workbook([row], drop=("上市日期",))))


@pytest.mark.parametrize(
    "url",
    [
        "https://etf.sse.com.cn/fundlist/",
        "http://fund.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1000_lf&TABKEY=tab1",
        "https://fund.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1",
        "https://fund.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1000_lf&TABKEY=tab2",
        "https://fund.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1000_lf&CATALOGID=1110&TABKEY=tab1",
    ],
)
def test_szse_fund_cache_and_redirect_require_its_exact_official_report(url):
    with pytest.raises(ValueError, match="wrong (source|report)|non-official"):
        ListingDateEvidence("159655", date(2022, 10, 25), url, "2026-10-31").validate(
            "159655"
        )
    with pytest.raises(ValueError):
        _szse_fund("159655", fund_session(source_url=url))


@pytest.mark.parametrize("code", ["159655", "160001"])
def test_listing_store_routes_szse_funds_without_stock_or_sse_fallback(
    tmp_path,
    monkeypatch,
    code,
):
    from src.data import listing_dates

    calls = []

    def fetch(requested, _session):
        calls.append(requested)
        return date(2022, 10, 25), SZSE_FUND_SOURCE["source_url"]

    monkeypatch.setattr(listing_dates, "_szse_fund", fetch)
    monkeypatch.setattr(
        listing_dates, "_szse_stock", lambda *_: pytest.fail("stock route")
    )
    monkeypatch.setattr(listing_dates, "_sse_fund", lambda *_: pytest.fail("SSE route"))
    store = ListingDateStore(tmp_path)
    evidence = store.resolve(code)
    assert evidence.listing_date == date(2022, 10, 25)
    assert store.read(code) == evidence
    assert store.resolve(code) == evidence
    assert calls == [code]
