"""HKEX routing, bounded discovery, classification and report integration."""

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from src.data import announcement_fetcher
from src.data.announcement_fetcher import AnnouncementFetcher
from src.data.hk_announcements import HkexAnnouncementProvider
from src.notification.email_notifier import EmailNotifier


@pytest.fixture
def hkex_http():
    http = Mock()
    http.get.return_value.json.return_value = {
        "list": [{"c": "700", "i": "7609", "s": "wrong-security-id"}]
    }
    return http


@pytest.fixture
def hkex_excerpt():
    return (
        Path(__file__).parent / "fixtures/hkex/announcements_tencent_excerpt.html"
    ).read_text("utf-8")


@pytest.fixture
def fetcher(monkeypatch):
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(announcement_fetcher, "datetime", clock)
    return AnnouncementFetcher({})


def result_page(rows, total=None):
    count = len(rows) if total is None else total
    return (
        f'<div class="total-records">共有 {count} 紀錄</div>'
        f"<table>{''.join(rows)}</table>"
    )


def result_row(day="30/10/2026", code="00700", title="中期股息", suffix="1"):
    return (
        f'<tr><td class="release-time">{day} 18:05</td>'
        f'<td class="stock-short-code">{code}</td><td>'
        '<div class="headline">公告及通告 - [股息或分派]</div>'
        f'<div class="doc-link"><a href="/listedco/{suffix}.pdf">{title}</a>'
        "</div></td></tr>"
    )


def test_real_excerpt_and_internal_stock_identifier(hkex_http, hkex_excerpt):
    hkex_http.post.return_value.text = hkex_excerpt
    provider = HkexAnnouncementProvider(http=hkex_http)
    records = provider.fetch("00700", date(2025, 9, 5), date(2026, 10, 30))
    assert len(records) == 4
    assert records[0]["published_at"] == "2026-10-22 12:03:00"
    assert records[0]["date"] == "2026-10-22"
    assert records[0]["exchange"] == "hkex"
    assert records[0]["url"].endswith("/2026102200203_c.pdf")
    assert all(row["stock_code"] == "00700" for row in records)
    data = hkex_http.post.call_args.kwargs["data"]
    assert data["stockId"] == "7609" and data["lang"] == "ZH"
    assert data["t1code"] == "-2"
    provider.fetch("00700", date(2025, 9, 5), date(2026, 10, 30))
    hkex_http.get.assert_called_once()  # Reuse the official issuer directory.


def test_truncated_window_is_split_sorted_and_deduplicated(hkex_http):
    newest = result_row(suffix="new")
    oldest = result_row("29/10/2026", suffix="old")
    hkex_http.post.side_effect = [
        Mock(text=result_page([newest], total=3)),
        Mock(text=result_page([newest, newest])),
        Mock(text=result_page([oldest])),
    ]
    rows = HkexAnnouncementProvider(http=hkex_http).fetch(
        "00700", date(2026, 10, 29), date(2026, 10, 30)
    )
    assert [row["date"] for row in rows] == ["2026-10-30", "2026-10-29"]
    ranges = [
        (c.kwargs["data"]["from"], c.kwargs["data"]["to"])
        for c in hkex_http.post.call_args_list
    ]
    assert ranges == [
        ("20261029", "20261030"),
        ("20261030", "20261030"),
        ("20261029", "20261029"),
    ]


@pytest.mark.parametrize(
    "page",
    [
        "<html>Service unavailable</html>",
        result_page([], total=1),
        result_page([result_row(code="00883")]),
        result_page([result_row(day="31/10/2026")]),
        result_page([result_row(day="unknown")]),
        result_page([result_row(title="")]),
        result_page([result_row().replace('class="release-time"', 'class="changed"')]),
        result_page(
            [result_row().replace("/listedco/1.pdf", "https://evil.test/1.pdf")]
        ),
        result_page(
            [result_row().replace("/listedco/1.pdf", "http://www.hkexnews.hk/1.pdf")]
        ),
        result_page([result_row()], total=0),
        result_page([result_row()], total=2),  # Single day cannot be split further.
    ],
)
def test_bad_or_incomplete_search_is_rejected(hkex_http, page):
    hkex_http.post.return_value.text = page
    with pytest.raises(ValueError):
        HkexAnnouncementProvider(http=hkex_http).fetch(
            "00700", date(2026, 10, 30), date(2026, 10, 30)
        )


def test_search_budget_is_bounded(hkex_http):
    hkex_http.post.return_value.text = result_page([result_row()], total=200)
    provider = HkexAnnouncementProvider(http=hkex_http)
    provider.MAX_SEARCHES = 1
    with pytest.raises(ValueError, match="limit exceeded"):
        provider.fetch("00700", date(2025, 9, 5), date(2026, 10, 30))
    hkex_http.post.assert_called_once()


def test_verified_empty_results_and_unknown_issuer(hkex_http):
    hkex_http.post.return_value.text = result_page([])
    provider = HkexAnnouncementProvider(http=hkex_http)
    assert provider.fetch("00700", date(2026, 10, 30), date(2026, 10, 30)) == []
    with pytest.raises(ValueError, match="identifier not found"):
        provider.fetch("00883", date(2026, 10, 30), date(2026, 10, 30))
    hkex_http.post.assert_called_once()


def test_market_routing_keeps_hk_before_mainland_prefixes(fetcher, monkeypatch):
    exchange_fetch = Mock(return_value=[])
    monkeypatch.setattr(fetcher, "_fetch_from_exchange", exchange_fetch)
    fetcher.fetch_announcements(
        [
            "00700",
            "00883",
            "01816",
            "00728",
            "01339",
            "18000",
            "52000",
            "601088",
            "000333",
            "002594",
            "510300",
            "159655",
            "VOO",
            "BRK.B",
        ]
    )
    assert [(c.args[0], c.args[1]) for c in exchange_fetch.call_args_list] == [
        ("00700", "hkex"),
        ("00883", "hkex"),
        ("01816", "hkex"),
        ("00728", "hkex"),
        ("01339", "hkex"),
        ("18000", "hkex"),
        ("52000", "hkex"),
        ("601088", "sse"),
        ("000333", "szse"),
        ("002594", "szse"),
        ("VOO", "sec"),
        ("BRK.B", "sec"),
    ]


@pytest.mark.parametrize(
    "title,kind",
    [
        ("中期股息", ""),
        ("利潤分配方案", ""),
        ("以股代息", ""),
        ("INTERIM DIVIDEND", ""),
        ("截至六月三十日止期間", "股息或分派"),
    ],
)
def test_hk_dividend_window_uses_traditional_titles_and_categories(
    fetcher, title, kind
):
    item = {"exchange": "hkex", "title": title, "type": kind, "date": "2025-09-05"}
    ordinary = {**item, "title": "董事名單", "type": ""}
    expired = {**item, "date": "2025-09-04"}
    future = {**item, "date": "2026-10-31"}
    assert fetcher._filter_announcements_by_window(
        [item, ordinary, expired, future], 7, 420
    ) == [item]


def test_hk_source_joins_existing_selection_and_email(fetcher, hkex_http, monkeypatch):
    hkex_http.post.return_value.text = result_page(
        [
            result_row("20/10/2026", title="截至六月三十日止六個月的中期股息"),
            result_row("30/10/2026", title="董事名單", suffix="other").replace(
                "股息或分派", "董事名單"
            ),
            result_row("20/10/2026", title="更改地址", suffix="expired").replace(
                "股息或分派", "更改地址"
            ),
        ]
    )
    fetcher._hk_announcements = HkexAnnouncementProvider(http=hkex_http)
    enrich = Mock(
        side_effect=AssertionError("HK cash must retain its currency authority")
    )
    monkeypatch.setattr(fetcher, "_enrich_announcements", enrich)
    announcements = fetcher.get_recent_important_announcements(["00700"], 7, 420)
    assert len(announcements["00700"]) == 1
    item = announcements["00700"][0]
    assert item["title"] == "截至六月三十日止六個月的中期股息"
    assert "llm_extracted_dividend" not in item
    enrich.assert_not_called()
    frame = pd.DataFrame([{"stock_code": "00700", "stock_name": "騰訊控股"}])
    notifier = EmailNotifier({"email": {}})
    body = notifier._build_daily_events_section(announcements, {}, frame)
    assert item["title"] in body and item["url"] in body and "00700" in body


@pytest.mark.parametrize(
    "title,kind",
    [
        ("截至九月三十日止三個月", "季度業績"),
        ("年度報告", ""),
        ("自願公告 — 購回股份", ""),
        ("PROFIT WARNING", ""),
    ],
)
def test_hk_important_selection_and_five_item_limit(fetcher, monkeypatch, title, kind):
    item = {"exchange": "hkex", "title": title, "type": kind}
    monkeypatch.setattr(
        fetcher, "fetch_announcements", lambda *_: {"00700": [item] * 7}
    )
    assert fetcher.get_recent_important_announcements(["00700"])["00700"] == [item] * 5


def test_hk_other_fallback_limit(fetcher, monkeypatch):
    item = {"exchange": "hkex", "title": "董事名單", "type": ""}
    monkeypatch.setattr(
        fetcher, "fetch_announcements", lambda *_: {"00700": [item] * 7}
    )
    assert fetcher.get_recent_important_announcements(["00700"])["00700"] == [item] * 3


def test_hk_provider_is_initialized_lazily_and_reused(fetcher, monkeypatch):
    provider = Mock()
    provider.fetch.return_value = []
    constructor = Mock(return_value=provider)
    monkeypatch.setattr(
        "src.data.hk_announcements.HkexAnnouncementProvider", constructor
    )
    assert fetcher.fetch_announcements(["00700", "00883"]) == {
        "00700": [],
        "00883": [],
    }
    constructor.assert_called_once_with(fetcher.config)
    assert provider.fetch.call_count == 2
    assert provider.fetch.call_args_list[0].args == (
        "00700",
        date(2025, 9, 5),
        date(2026, 10, 30),
    )


def test_hk_failure_does_not_stop_other_monitored_stocks(fetcher, monkeypatch, caplog):
    fetcher._hk_announcements = Mock()
    fetcher._hk_announcements.fetch.side_effect = requests.Timeout("HKEX unavailable")
    item = {"stock_code": "601088", "title": "利润分配", "date": "2026-10-30"}
    monkeypatch.setattr(fetcher, "_fetch_from_sse_backup", lambda *_: [item])
    assert fetcher.fetch_announcements(["00700", "601088"]) == {
        "00700": [],
        "601088": [item],
    }
    assert "HKEX unavailable" in caplog.text
