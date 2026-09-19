import json
from datetime import date, datetime, timezone

import pandas as pd

from src.data.dividend_events import (
    DIVIDEND_EX_UNPAID,
    DIVIDEND_PENDING,
    DailyDividendEventResolver,
)


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_resolver_classifies_structured_stock_dividend_dates():
    resolver = DailyDividendEventResolver(
        {}, http_get=lambda *args, **kwargs: _Response("")
    )
    events = resolver.resolve(
        {
            "601728": [
                {
                    "llm_extracted_dividend": {
                        "success": True,
                        "cash_dividend_per_share": 0.2,
                        "ex_rights_date": "2026-09-24",
                        "payment_date": "2026-09-30",
                    }
                },
                {
                    "llm_extracted_dividend": {
                        "success": True,
                        "cash_dividend_per_share": 0.3,
                        "ex_rights_date": "2026-09-10",
                        "payment_date": "2026-09-25",
                    }
                },
            ]
        },
        pd.DataFrame([{"stock_code": "601728"}]),
        as_of=date(2026, 9, 19),
    )

    assert [item["status"] for item in events] == [
        DIVIDEND_EX_UNPAID,
        DIVIDEND_PENDING,
    ]
    assert [item["cash_per_share"] for item in events] == [0.3, 0.2]


def test_resolver_includes_etf_distribution_with_explicit_payment_dates():
    html = """
    <table><tr><th>年份</th><th>权益登记日</th><th>除息日</th>
    <th>每10份分红</th><th>分红发放日</th></tr>
    <tr><td>2026年</td><td>2026-09-20</td><td>2026-09-21</td>
    <td>每10份派现金1.2500元</td><td>2026-09-28</td></tr>
    <tr><td>2026年</td><td>2026-09-01</td><td>2026-09-02</td>
    <td>每10份派现金0.5000元</td><td>2026-09-22</td></tr></table>
    """
    resolver = DailyDividendEventResolver(
        {}, http_get=lambda *args, **kwargs: _Response(html)
    )
    events = resolver.resolve(
        {},
        pd.DataFrame([{"stock_code": "510300"}]),
        as_of=date(2026, 9, 19),
    )

    assert [(item["status"], item["cash_per_share"]) for item in events] == [
        (DIVIDEND_EX_UNPAID, 0.05),
        (DIVIDEND_PENDING, 0.125),
    ]
    assert all(item["currency"] == "CNY" for item in events)


def test_resolver_includes_hk_stock_with_explicit_ex_and_payment_dates():
    html = """
    <table><tr><td>公布日期</td><td>財政年度</td><td>事項</td><td>除淨日</td>
    <td>截止過戶日期由</td><td>截止過戶日期至</td><td>派送日</td></tr>
    <tr><td>2026/08/26</td><td>2026/12</td><td>中期息港元 0.94</td>
    <td>2026/09/10</td><td>2026/09/14</td><td>2026/09/18</td>
    <td>2026/10/16</td></tr></table>
    """
    resolver = DailyDividendEventResolver(
        {}, http_get=lambda *args, **kwargs: _Response(html)
    )
    events = resolver.resolve(
        {},
        pd.DataFrame([{"stock_code": "00883"}]),
        as_of=date(2026, 9, 19),
    )

    assert events == [
        {
            "code": "00883",
            "ex_date": "2026-09-10",
            "payment_date": "2026-10-16",
            "cash_per_share": 0.94,
            "currency": "HKD",
            "status": DIVIDEND_EX_UNPAID,
            "source": "hk_dividend_schedule",
        }
    ]


def test_resolver_reads_yahoo_svelte_calendar_for_overseas_etf():
    ex_date = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()
    payment_date = datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()
    payload = {
        "quoteSummary": {
            "result": [
                {
                    "calendarEvents": {
                        "exDividendDate": {"raw": ex_date},
                        "dividendDate": {"raw": payment_date},
                    }
                }
            ]
        }
    }
    page = (
        '<script data-sveltekit-fetched type="application/json">'
        + json.dumps({"status": 200, "body": json.dumps(payload)})
        + "</script>"
    )
    resolver = DailyDividendEventResolver(
        {}, http_get=lambda *args, **kwargs: _Response(page)
    )
    events = resolver.resolve(
        {},
        pd.DataFrame([{"stock_code": "VOO"}]),
        as_of=date(2026, 9, 19),
    )

    assert events == [
        {
            "code": "VOO",
            "ex_date": "2026-09-24",
            "payment_date": "2026-09-30",
            "cash_per_share": None,
            "currency": None,
            "status": DIVIDEND_PENDING,
            "source": "yahoo_calendar",
        }
    ]


def test_resolver_omits_events_without_both_state_dates():
    resolver = DailyDividendEventResolver(
        {}, http_get=lambda *args, **kwargs: _Response("")
    )
    events = resolver.resolve(
        {
            "601728": [
                {
                    "llm_extracted_dividend": {
                        "success": True,
                        "cash_dividend_per_share": 0.2,
                        "ex_rights_date": "2026-09-10",
                        "payment_date": None,
                    }
                }
            ]
        },
        pd.DataFrame([{"stock_code": "601728"}]),
        as_of=date(2026, 9, 19),
    )

    assert events == []


def test_resolver_omits_future_ex_date_without_explicit_payment_date():
    resolver = DailyDividendEventResolver(
        {}, http_get=lambda *args, **kwargs: _Response("")
    )
    events = resolver.resolve(
        {
            "601728": [
                {
                    "llm_extracted_dividend": {
                        "success": True,
                        "cash_dividend_per_share": 0.2,
                        "ex_rights_date": "2026-09-24",
                        "payment_date": None,
                    }
                }
            ]
        },
        pd.DataFrame([{"stock_code": "601728"}]),
        as_of=date(2026, 9, 19),
    )

    assert events == []
