"""US recovery must fail closed on partial pages and unevidenced actions."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date

import numpy as np
import pytest
import requests

from src.data.market_history import CorporateAction, corporate_action_issues
from src.data.us_market_history import (
    CorporateActionEvidence,
    DividendInvestorPublicationProvider,
    DividendPublication,
    NasdaqMarketHistoryProvider,
    SplitHistoryProvider,
    StockScanPublicationProvider,
    VanguardDistributionProvider,
    _berkshire_dividend_year_from_text,
    _english_date,
)

START = date(2026, 9, 21)
END = date(2026, 9, 23)
SOURCE_URL = "https://issuer.example/investors/actual-actions"


class Response:
    def __init__(self, body, *, status=200):
        self.body = body
        self.status = status
        self.url = "https://api.nasdaq.com/public-test-response"
        self.content = json.dumps(body).encode()

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))

    def json(self):
        return deepcopy(self.body)


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def row(day, *, close=100, opening=99, high=101, low=98, volume="1,000"):
    return {
        "date": day,
        "close": f"${close}",
        "open": f"${opening}",
        "high": f"${high}",
        "low": f"${low}",
        "volume": volume,
    }


def prices():
    return [
        row("09/23/2026", close=101, high=102),
        row("09/22/2026", close=99, opening=98, high=100, low=97),
        row("09/21/2026"),
    ]


def page(rows=None, *, total=3, symbol="VOO"):
    return Response(
        {
            "status": {"rCode": 200},
            "data": {
                "symbol": symbol,
                "totalRecords": total,
                "tradesTable": {"rows": prices() if rows is None else rows},
            },
        }
    )


def evidence(actions=(), **changes):
    values = {
        "code": "VOO",
        "start": START,
        "end": END,
        "actions": tuple(actions),
        "source_urls": (SOURCE_URL,),
        "dividends_complete": True,
        "splits_complete": True,
    }
    values.update(changes)
    return CorporateActionEvidence(**values)


def dividend(**changes):
    values = {
        "code": "VOO",
        "action_type": "cash_dividend",
        "ex_date": date(2026, 9, 22),
        "published_at": START,
        "cash_per_share": 2.0,
        "source": "issuer",
        "currency": "USD",
        "source_url": SOURCE_URL,
    }
    values.update(changes)
    return CorporateAction(**values)


def test_nasdaq_pagination_normalizes_share_class_and_retains_evidence():
    session = Session(
        page(prices()[:2], symbol="BRK/B"),
        page(prices()[2:], symbol="BRK/B"),
    )
    provider = NasdaqMarketHistoryProvider(http=session)
    provider.page_size = 2
    frame = provider.fetch_raw("BRK.B", START, END, assetclass="stocks")
    assert frame["date"].dt.date.tolist() == [START, date(2026, 9, 22), END]
    assert frame["raw_close"].tolist() == [100, 99, 101]
    assert frame["volume"].tolist() == [1000, 1000, 1000]
    assert [call[1]["params"]["offset"] for call in session.calls] == [0, 2]
    assert session.calls[0][0].endswith("/BRK.B/historical")
    assert len(frame.attrs["pages"]) == 2
    assert all(len(item["sha256"]) == 64 for item in frame.attrs["pages"])
    assert "qfq_close" not in frame


@pytest.mark.parametrize(
    "second,match",
    [
        (page([], total=3), "ended before"),
        (page(prices()[:1], total=3), "repeated a date"),
        (page(prices()[2:], total=4), "changed during pagination"),
    ],
)
def test_partial_or_unstable_pages_are_not_accepted(second, match):
    provider = NasdaqMarketHistoryProvider(http=Session(page(prices()[:2]), second))
    with pytest.raises(ValueError, match=match):
        provider.fetch_raw("VOO", START, END, assetclass="etf")


@pytest.mark.parametrize(
    "field,value",
    [
        ("close", None),
        ("open", "N/A"),
        ("high", "nan"),
        ("low", "-1"),
        ("volume", "--"),
        ("volume", "1.5"),
        ("high", "1"),
        ("low", "1000"),
    ],
)
def test_missing_or_invalid_reported_values_fail_instead_of_becoming_prices(
    field, value
):
    rows = prices()
    rows[0][field] = value
    provider = NasdaqMarketHistoryProvider(http=Session(page(rows)))
    with pytest.raises(ValueError):
        provider.fetch_raw("VOO", START, END, assetclass="etf")


def test_middle_session_gap_is_rejected_even_when_both_endpoints_exist():
    provider = NasdaqMarketHistoryProvider(http=Session(page(prices()[::2], total=2)))
    with pytest.raises(ValueError, match="missing=1.*2026-09-22"):
        provider.fetch_raw("VOO", START, END, assetclass="etf")


def test_real_us_holiday_is_not_a_missing_price():
    rows = [row("07/06/2026"), row("07/02/2026")]
    provider = NasdaqMarketHistoryProvider(http=Session(page(rows, total=2)))
    frame = provider.fetch_raw(
        "VOO", date(2026, 7, 2), date(2026, 7, 6), assetclass="etf"
    )
    assert len(frame) == 2  # July 3 is the observed Independence Day holiday.


def test_calendar_year_boundary_on_a_weekend_does_not_reject_long_history():
    rows = [row("01/04/2023"), row("01/03/2023"), row("12/30/2022")]
    provider = NasdaqMarketHistoryProvider(http=Session(page(rows)))
    frame = provider.fetch_raw(
        "VOO", date(2022, 12, 30), date(2023, 1, 4), assetclass="etf"
    )
    assert len(frame) == 3


def test_wrong_symbol_and_provider_errors_fail_closed():
    provider = NasdaqMarketHistoryProvider(http=Session(page(symbol="SPY")))
    with pytest.raises(ValueError, match="symbol does not match"):
        provider.fetch_raw("VOO", START, END, assetclass="etf")
    provider = NasdaqMarketHistoryProvider(http=Session(Response({"data": None})))
    with pytest.raises(ValueError, match="history unavailable"):
        provider.fetch_raw("VOO", START, END, assetclass="etf")
    provider = NasdaqMarketHistoryProvider(http=Session(Response({}, status=403)))
    with pytest.raises(requests.HTTPError):
        provider.fetch_raw("VOO", START, END, assetclass="etf")


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"dividends_complete": False}, "complete dividend"),
        ({"splits_complete": False}, "complete dividend"),
        ({"source_urls": ()}, "source URLs"),
        ({"start": date(2026, 9, 22)}, "does not cover"),
        ({"code": "SPY"}, "different symbol"),
    ],
)
def test_empty_actions_require_actual_coverage_evidence(changes, match):
    session = Session()
    provider = NasdaqMarketHistoryProvider(http=session)
    with pytest.raises(ValueError, match=match):
        provider.fetch(
            "VOO", START, END, assetclass="etf", action_evidence=evidence(**changes)
        )
    assert session.calls == []


def test_no_default_empty_action_contract_and_no_fabricated_publication_date():
    provider = NasdaqMarketHistoryProvider(http=Session())
    with pytest.raises(ValueError, match="independent corporate action evidence"):
        provider.fetch("VOO", START, END, assetclass="etf")
    with pytest.raises(ValueError, match="lacks causal publication date"):
        provider.fetch(
            "VOO",
            START,
            END,
            assetclass="etf",
            action_evidence=evidence([dividend(published_at=None)]),
        )


def test_verified_cash_actions_adjust_all_ohlc_and_preserve_raw_execution_prices():
    provider = NasdaqMarketHistoryProvider(http=Session(page()))
    bundle = provider.fetch(
        "VOO", START, END, assetclass="etf", action_evidence=evidence([dividend()])
    )
    assert bundle.prices["raw_close"].tolist() == [100, 99, 101]
    np.testing.assert_allclose(bundle.prices["qfq_factor"], [0.98, 1.0, 1.0])
    for field in ("open", "high", "low", "close"):
        np.testing.assert_allclose(
            bundle.prices[f"qfq_{field}"],
            bundle.prices[f"raw_{field}"] * bundle.prices["qfq_factor"],
        )
    assert bundle.actions[0].published_at == START
    assert bundle.currency == "USD"


def test_split_history_is_not_double_adjusted_without_source_price_basis():
    provider = NasdaqMarketHistoryProvider(http=Session())
    action = dividend(cash_per_share=None, share_multiplier=2, action_type="split")
    with pytest.raises(ValueError, match="unverified price basis"):
        provider.fetch(
            "VOO", START, END, assetclass="etf", action_evidence=evidence([action])
        )


def vanguard_row(**changes):
    values = {
        "typeCode": "INC",
        "amount": 2.0,
        "exDividendDate": "2026-09-22",
        "recordDate": "2026-09-22",
        "payableDate": "2026-09-25",
        "reinvestDate": "2026-09-22",
        "reinvestPrice": 99.0,
    }
    values.update(changes)
    return values


def test_vanguard_keeps_unknown_publication_and_official_cash_dates():
    provider = VanguardDistributionProvider(http=Session(Response([vanguard_row()])))
    actions = provider.fetch("VOO", "0968", START, END)
    assert actions[0].cash_per_share == 2.0
    assert actions[0].published_at is None
    assert actions[0].ex_date == date(2026, 9, 22)
    assert actions[0].record_date == date(2026, 9, 22)
    assert actions[0].payable_date == date(2026, 9, 25)
    assert "lacks causal publication date" in corporate_action_issues(actions)[0]


def test_vanguard_attaches_only_corroborated_announcement_with_provenance():
    provider = VanguardDistributionProvider(http=Session(Response([vanguard_row()])))
    publication = DividendPublication(date(2026, 9, 22), 2.0, START, SOURCE_URL)
    actions = provider.fetch(
        "VOO", "0968", START, END, publications={publication.ex_date: publication}
    )
    assert actions[0].published_at == START
    assert corporate_action_issues(actions) == ()
    assert f"publication_evidence:{SOURCE_URL}" in actions[0].diagnostics


@pytest.mark.parametrize("cash,published", [(2.01, START), (2.0, END)])
def test_vanguard_rejects_mismatched_or_late_announcement(cash, published):
    provider = VanguardDistributionProvider(http=Session(Response([vanguard_row()])))
    publication = DividendPublication(date(2026, 9, 22), cash, published, SOURCE_URL)
    with pytest.raises(ValueError, match="does not match official"):
        provider.fetch(
            "VOO", "0968", START, END, publications={publication.ex_date: publication}
        )


class HtmlResponse(Response):
    def __init__(self, html, url):
        super().__init__({})
        self.content = html.encode("utf-8")
        self.url = url


def statmuse_html(*rows):
    headers = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    return (
        "<title>VOO Stock Price In September 2026 | StatMuse Money</title>"
        "<table><thead><tr>"
        + "".join(f"<th>{value}</th>" for value in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table>"
    )


@pytest.mark.parametrize("independent_open,accepted", [(99, True), (97, False)])
def test_impossible_nasdaq_high_requires_matching_independent_bar(
    independent_open, accepted
):
    rows = prices()
    rows[2]["high"] = "$95"
    independent = HtmlResponse(
        statmuse_html(
            [
                "September 21 2026",
                f"${independent_open}",
                "$101",
                "$98",
                "$100",
                "1,002",
            ]
        ),
        "https://www.statmuse.com/money/ask/voo-stock-price-in-september-2026",
    )
    provider = NasdaqMarketHistoryProvider(http=Session(page(rows), independent))
    if not accepted:
        with pytest.raises(ValueError, match="independent correction unavailable"):
            provider.fetch_raw("VOO", START, END, assetclass="etf")
        return
    frame = provider.fetch_raw("VOO", START, END, assetclass="etf")
    assert frame.loc[0, "raw_high"] == 101
    assert frame.attrs["ohlc_repairs"][0]["nasdaq_value"] == 95
    assert frame.attrs["ohlc_repairs"][0]["verified_value"] == 101
    assert len(frame.attrs["ohlc_repairs"][0]["response_sha256"]) == 64


HISTORY_URL = "https://www.dividendinvestor.com/pdy/?symbol=VOO&proj_div_yield=10"
NEWS_URL = "https://www.dividendinvestor.com/dividend-news/?symbol=voo"
ARTICLE_URL = (
    "https://www.dividendinvestor.com/dividend-news/20260921/"
    "issuer-nyse-voo-declared-a-dividend-of-$2.0000-per-share/"
)


def test_public_date_parser_accepts_legacy_hyphenated_month_dates():
    assert _english_date("Sep-09-2016") == date(2016, 9, 9)


def test_berkshire_dividend_policy_survives_pdf_spacing_loss():
    assert (
        _berkshire_dividend_year_from_text(
            "Dividends\nBerkshirehasnotdeclaredacashdividendsince1967."
        )
        == 1967
    )
    assert (
        _berkshire_dividend_year_from_text(
            "A subsidiary has not declared a cash dividend since 1967."
        )
        is None
    )


def history_html(*rows):
    headers = [
        "Year",
        "Declaration Date",
        "Ex-Dividend Date",
        "Record Date",
        "Payable Date",
        "Dividend $ Amount",
    ]
    cells = lambda values: "<tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"
    return (
        '<a href="/dividend-history-detail/voo/">VOO Dividend History</a>'
        + "<table>"
        + cells(headers)
        + "".join(cells(row) for row in rows)
        + cells(["2026 Total :", "", "", "", "", "2.0000"])
        + "</table>"
    )


def actual_news_html():
    return """<title>VOO Dividend Announcement $2.0000/Share 9/21/2026</title>
    <div class="rdate">Dividend Declaration Date: September 21, 2026<br>
    Dividend Ex Date: September 22, 2026<br>
    Dividend Record Date: September 22, 2026<br>
    Dividend Payment Date: September 25, 2026<br>
    Dividend Amount: $ 2.0000</div>"""


def test_public_history_matches_exact_cash_and_all_issuer_dates():
    response = HtmlResponse(
        history_html(
            [
                "2026",
                "Sep 21, 2026",
                "Sep 22, 2026",
                "Sep 22, 2026",
                "Sep 25, 2026",
                "2.0000",
            ]
        ),
        HISTORY_URL,
    )
    (publication,) = DividendInvestorPublicationProvider.parse_history("VOO", response)
    assert publication.published_at == START
    assert publication.record_date == date(2026, 9, 22)
    assert len(publication.response_sha256) == 64
    action = VanguardDistributionProvider(
        http=Session(Response([vanguard_row()]))
    ).fetch(
        "VOO",
        "0968",
        START,
        END,
        publications={publication.ex_date: publication},
    )[0]
    assert action.published_at == START
    assert (
        f"publication_response_sha256:{publication.response_sha256}"
        in action.diagnostics
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_date", date(2026, 9, 23)),
        ("payable_date", date(2026, 9, 24)),
        ("cash_per_share", 2.0005),
    ],
)
def test_announcements_must_match_record_payment_and_exact_cash(field, value):
    publication = DividendInvestorPublicationProvider.parse_article(
        "VOO", HtmlResponse(actual_news_html(), ARTICLE_URL)
    )
    provider = VanguardDistributionProvider(http=Session(Response([vanguard_row()])))
    with pytest.raises(ValueError, match="does not match official distribution"):
        provider.fetch(
            "VOO",
            "0968",
            START,
            END,
            publications={publication.ex_date: replace(publication, **{field: value})},
        )


@pytest.mark.parametrize(
    "change,match",
    [
        (
            lambda s: s.replace("VOO Dividend Announcement", "Dividend Announcement"),
            "stub",
        ),
        (lambda s: s.replace("9/21/2026", "9/20/2026"), "dates disagree"),
        (lambda s: s.replace("Dividend Amount", "Unknown field"), "incomplete"),
        (
            lambda s: s.replace("September 21, 2026", "September 23, 2026"),
            "inconsistent",
        ),
    ],
)
def test_title_only_or_inconsistent_news_never_establishes_publication(change, match):
    with pytest.raises(ValueError, match=match):
        DividendInvestorPublicationProvider.parse_article(
            "VOO", HtmlResponse(change(actual_news_html()), ARTICLE_URL)
        )


def test_new_issuer_event_is_found_via_public_ticker_index_without_guessed_dates():
    old_day = date(2025, 6, 30)
    old_action = dividend(
        ex_date=old_day,
        record_date=old_day,
        payable_date=date(2025, 7, 2),
        cash_per_share=1.0,
    )
    new_action = dividend(record_date=date(2026, 9, 22), payable_date=date(2026, 9, 25))
    history = HtmlResponse(
        history_html(
            [
                "2025",
                "Jun 26, 2025",
                "Jun 30, 2025",
                "Jun 30, 2025",
                "Jul 2, 2025",
                "1.0000",
            ]
        ),
        HISTORY_URL,
    )
    relative = ARTICLE_URL.split("/dividend-news/", 1)[1]
    index = HtmlResponse(
        f'<h1>Fund (VOO) Dividend News</h1><a href="{relative}">Actual news</a>'
        f'<a href="https://unrelated.example/{relative}">Ignore other host</a>',
        NEWS_URL,
    )
    session = Session(history, index, HtmlResponse(actual_news_html(), ARTICLE_URL))
    publications = DividendInvestorPublicationProvider(http=session).fetch(
        "VOO", [old_action, new_action]
    )
    assert set(publications) == {old_day, new_action.ex_date}
    assert publications[new_action.ex_date].basis == "contemporaneous_dividend_news"
    assert session.calls[-1][0] == ARTICLE_URL
    assert len(session.calls) == 3


def test_missing_new_event_stays_missing_when_index_has_no_actual_announcement():
    old_day = date(2025, 6, 30)
    history = HtmlResponse(
        history_html(
            [
                "2025",
                "Jun 26, 2025",
                "Jun 30, 2025",
                "Jun 30, 2025",
                "Jul 2, 2025",
                "1.0000",
            ]
        ),
        HISTORY_URL,
    )
    index = HtmlResponse("<h1>Fund (VOO) Dividend News</h1>", NEWS_URL)
    publications = DividendInvestorPublicationProvider(
        http=Session(history, index)
    ).fetch("VOO", [dividend()])
    assert old_day not in publications
    assert publications == {}


def test_stale_dividend_article_does_not_block_a_valid_later_article():
    stale_url = (
        "https://www.dividendinvestor.com/dividend-news/20160909/"
        "issuer-nyse-voo-declared-a-dividend-of-$0.8830-per-share/"
    )
    stale_html = (
        actual_news_html()
        .replace("9/21/2026", "9/9/2016")
        .replace("September 21, 2026", "September 9, 2016")
    )
    history = HtmlResponse(
        history_html(
            [
                "2025",
                "Jun 26, 2025",
                "Jun 30, 2025",
                "Jun 30, 2025",
                "Jul 2, 2025",
                "1.0000",
            ]
        ),
        HISTORY_URL,
    )
    index = HtmlResponse(
        "<h1>Fund (VOO) Dividend News</h1>"
        f'<a href="{stale_url}">Stale</a>'
        f'<a href="{ARTICLE_URL}">Valid</a>',
        NEWS_URL,
    )
    session = Session(
        history,
        index,
        HtmlResponse(stale_html, stale_url),
        HtmlResponse(actual_news_html(), ARTICLE_URL),
    )
    result = DividendInvestorPublicationProvider(http=session).fetch(
        "VOO", [dividend(record_date=date(2026, 9, 22), payable_date=date(2026, 9, 25))]
    )
    assert set(result) == {date(2026, 9, 22)}
    assert result[date(2026, 9, 22)].source_url == ARTICLE_URL


def test_stale_dividend_article_alone_does_not_establish_publication():
    stale_url = (
        "https://www.dividendinvestor.com/dividend-news/20160909/"
        "issuer-nyse-voo-declared-a-dividend-of-$0.8830-per-share/"
    )
    stale_html = (
        actual_news_html()
        .replace("9/21/2026", "9/9/2016")
        .replace("September 21, 2026", "September 9, 2016")
    )
    history = HtmlResponse(
        history_html(
            [
                "2025",
                "Jun 26, 2025",
                "Jun 30, 2025",
                "Jun 30, 2025",
                "Jul 2, 2025",
                "1.0000",
            ]
        ),
        HISTORY_URL,
    )
    index = HtmlResponse(
        f'<h1>Fund (VOO) Dividend News</h1><a href="{stale_url}">Stale</a>',
        NEWS_URL,
    )
    session = Session(history, index, HtmlResponse(stale_html, stale_url))
    result = DividendInvestorPublicationProvider(http=session).fetch(
        "VOO", [dividend(record_date=date(2026, 9, 22), payable_date=date(2026, 9, 25))]
    )
    assert result == {}


def stockscan_html(*rows):
    headers = [
        "Ex/EFF Date",
        "Type",
        "Cash amount",
        "Declaration date",
        "Record date",
        "Payment date",
    ]
    cells = lambda values: "<tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"
    return (
        "<title>Vanguard S P 500 Etf Stock (VOO) Dividend History: "
        "Date, Type, Amount - StockScan</title>"
        '<table class="prediction-table dividend-table"><thead><tr>'
        + "".join(f"<th>{value}</th>" for value in headers)
        + "</tr></thead><tbody>"
        + "".join(cells(row) for row in rows)
        + "</tbody></table>"
    )


def test_full_history_fills_old_issuer_matched_declaration_only():
    old = dividend(
        ex_date=date(2018, 6, 28),
        record_date=date(2018, 6, 29),
        payable_date=date(2018, 7, 3),
        cash_per_share=1.1573,
    )
    valid = ["06/28/2018", "CD", "$1.1573", "06/26/2018", "06/29/2018", "07/03/2018"]
    response = HtmlResponse(
        stockscan_html(valid),
        "https://stockscan.io/stocks/VOO/dividend-history",
    )
    result = StockScanPublicationProvider(http=Session(response)).fetch("VOO", [old])
    assert result[old.ex_date].published_at == date(2018, 6, 26)
    assert result[old.ex_date].basis == "secondary_full_history_declaration_date"
    assert len(result[old.ex_date].response_sha256) == 64


@pytest.mark.parametrize(
    "row",
    [
        ["06/28/2018", "CD", "$1.1572", "06/26/2018", "06/29/2018", "07/03/2018"],
        ["06/28/2018", "CD", "$1.1573", "01/01/2018", "06/29/2018", "07/03/2018"],
        ["06/28/2018", "CD", "$1.1573", "06/26/2018", "06/30/2018", "07/03/2018"],
    ],
)
def test_full_history_rejects_mismatched_or_implausible_rows(row):
    old = dividend(
        ex_date=date(2018, 6, 28),
        record_date=date(2018, 6, 29),
        payable_date=date(2018, 7, 3),
        cash_per_share=1.1573,
    )
    response = HtmlResponse(
        stockscan_html(row),
        "https://stockscan.io/stocks/VOO/dividend-history",
    )
    assert (
        StockScanPublicationProvider(http=Session(response)).fetch("VOO", [old]) == {}
    )


def split_html(symbol="VOO", day="10/24/2013", ratio="1 for 2", count=1):
    return (
        f"<title>{symbol} Split History</title>"
        f"has {count} split in our {symbol} split history database"
        f"<table><tr><td>{symbol} Split History Table</td></tr>"
        "<tr><td>Date</td><td>Ratio</td></tr>"
        f"<tr><td>{day}</td><td>{ratio}</td></tr></table>"
    )


def test_counted_split_history_supports_no_splits_in_window_and_keeps_evidence():
    session = Session(HtmlResponse(split_html(), "https://www.splithistory.com/voo/"))
    proof = SplitHistoryProvider(http=session).fetch("VOO", START, END)
    proof.require_no_splits()
    assert proof.splits == ((date(2013, 10, 24), 0.5),)
    assert len(proof.response_sha256) == 64
    assert proof.observed_at.tzinfo is not None


@pytest.mark.parametrize(
    "html,match",
    [
        (split_html(count=2), "record count"),
        (split_html(symbol="SPY"), "requested symbol"),
        ("<title>VOO Split History</title>", "complete record count"),
        (split_html(ratio="N/A"), "invalid ratio"),
    ],
)
def test_incomplete_or_wrong_split_history_never_counts_as_no_splits(html, match):
    provider = SplitHistoryProvider(
        http=Session(HtmlResponse(html, "https://www.splithistory.com/voo/"))
    )
    with pytest.raises(ValueError, match=match):
        provider.fetch("VOO", START, END)


def test_new_split_is_detected_and_prevents_unverified_price_adjustment():
    provider = SplitHistoryProvider(
        http=Session(
            HtmlResponse(
                split_html(day="09/22/2026", ratio="2 for 1"),
                "https://www.splithistory.com/voo/",
            )
        )
    )
    with pytest.raises(ValueError, match="unverified price basis"):
        provider.fetch("VOO", START, END).require_no_splits()
