from types import SimpleNamespace

import pandas as pd
from bs4 import BeautifulSoup

from src.notification.email_notifier import EmailNotifier


def _notifier(monkeypatch):
    notifier = EmailNotifier(
        {
            "email": {
                "sender_email": "test@example.com",
                "sender_password": "password",
                "receiver_email": "test@example.com",
            }
        }
    )
    monkeypatch.setattr(
        notifier,
        "_get_server_info",
        lambda: {
            "hostname": "daily-node-01",
            "ip_address": "203.0.113.8",
            "system": "Linux",
            "machine": "x86_64",
            "kernel_version": "6.1",
        },
    )
    return notifier


def _stock_data():
    return pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "stock_name": "<script>alert(1)</script>" + "很长名称" * 20,
                "date": "2026-09-30",
                "quote_status": "realtime",
                "quote_as_of": "2026-09-30T14:59:00+08:00",
                "close": 10.2,
                "open": 10.0,
                "prev_close": 9.75,
                "high": 10.5,
                "low": 9.8,
                "ma60": 10.0,
                "wma20": 10.1,
                "dividend_per_share": 0.2,
                "dividend_yield": 3.2,
                "pe_ratio": 8.1,
                "pb_ratio": 1.1,
                "roe": 12.0,
                "rsi": 52.0,
                "macd_hist": 0.123,
                "vol_ratio": 1.2,
                "adx": 20.0,
                "boll_pct_b": 0.55,
            }
        ]
    )


def _daily_body(notifier, *, alerts=None, signal_scan=None):
    return notifier._build_email_body(
        _stock_data(),
        announcements={
            "601728": [
                {
                    "title": "<b>恶意公告</b>" + "公告标题" * 30,
                    "url": "javascript:alert(1)",
                    "date": pd.Timestamp.now().strftime("%Y-%m-%d"),
                    "exchange": "SSE",
                },
                {
                    "title": "安全公告",
                    "url": "https://example.com/notice",
                    "date": pd.Timestamp.now().strftime("%Y-%m-%d"),
                    "exchange": "SSE",
                },
            ]
        },
    )


def test_daily_matrix_shows_open_previous_close_and_current_price(monkeypatch):
    soup = BeautifulSoup(_daily_body(_notifier(monkeypatch)), "html.parser")
    table = soup.select_one(".watchlist-table")
    headers = table.select("thead th")
    assert [item.get_text() for item in headers[:4]] == ["标的", "开盘", "前收", "报价"]
    assert "股息率" not in table.get_text()
    assert all("mobile-hide" not in item.get("class", []) for item in headers[1:4])
    cells = table.select_one("tbody tr").select("td")
    assert cells[0].select_one(".status-badge").get_text() == (
        "实时 · 截至 2026-09-30 14:59:00+08:00"
    )
    assert [cell.get_text() for cell in cells[1:3]] == ["10.00", "9.75"]
    assert cells[3].strong.get_text() == "10.20"
    assert cells[3].select_one(".price-change").get_text() == "+4.62%"
    assert "#c43c45" in cells[3]["style"]


def test_daily_alert_and_empty_paths_share_one_mobile_document(monkeypatch):
    notifier = _notifier(monkeypatch)
    scan = SimpleNamespace(alerts=[])
    alert_body = _daily_body(
        notifier,
        alerts=[
            {
                "stock_code": "601728",
                "rule_label": "<MA60",
                "condition": "<b>跌破</b>",
                "current_value": "<bad>",
            }
        ],
        signal_scan=scan,
    )
    empty_body = _daily_body(notifier, signal_scan=scan)

    for body in (alert_body, empty_body):
        soup = BeautifulSoup(body, "html.parser")
        assert len(soup.find_all("html")) == 1
        assert len(soup.find_all("head")) == 1
        assert len(soup.find_all("body")) == 1
        assert body.strip().endswith("</html>")
        assert "daily-node-01" in body
        assert "203.0.113.8" in body
        assert "完整行情矩阵" in body
        assert "三市场决策板" not in body
        assert "参考持仓" not in body
        assert "今日需关注" not in body

    assert alert_body.count("<html") == empty_body.count("<html") == 1
    assert "三市场决策板" not in alert_body
    assert "三市场决策板" not in empty_body


def test_daily_data_is_escaped_and_reference_portfolio_is_not_in_body(monkeypatch):
    notifier = _notifier(monkeypatch)
    body = _daily_body(notifier)
    soup = BeautifulSoup(body, "html.parser")

    assert soup.find("script") is None
    # The escaping guarantee is unchanged, but its carrier moved: the stock
    # cards that used to render stock_name were removed from the daily mail,
    # so assert on the announcement title, which still carries provider text.
    assert "&lt;b&gt;恶意公告&lt;/b&gt;" in body
    assert "javascript:" not in body
    assert 'href="https://example.com/notice"' in body
    assert "参考持仓" not in body
    assert "601728" in body
    assert body[body.find("</html>") + len("</html>") :].strip() == ""
    assert len(soup.find_all("table")) >= 3


def test_daily_missing_values_and_long_text_do_not_break_html(monkeypatch):
    notifier = _notifier(monkeypatch)
    sparse = pd.DataFrame(
        [
            {
                "stock_code": "000001",
                "stock_name": "名称" * 100,
                "close": None,
                "quote_status": "unavailable",
                "quote_as_of": None,
                "quote_reason": "行情源缺失 <script>",
                "pe_ratio": 8.0,
                "pb_ratio": 1.2,
                "roe": 15.0,
                "ma60": None,
            }
        ]
    )
    body = notifier._build_email_body(
        sparse,
        announcements={"000001": [{"title": "标题" * 200}]},
    )
    soup = BeautifulSoup(body, "html.parser")
    assert soup.find("script") is None
    assert len(soup.find_all("html")) == 1
    assert body.strip().endswith("</html>")
    assert "000001" in body
    rows = soup.select(".watchlist-table tbody tr")
    assert len(rows) == 1
    cells = rows[0].select("td")
    assert [cell.get_text() for cell in cells[1:3]] == ["—", "—"]
    assert cells[3].strong.get_text() == "—"
    assert cells[3].select_one(".price-change").get_text() == "涨跌未知"
    assert [cell.get_text() for cell in cells[4:]] == ["8.0", "1.20", "14.5%", "15.00"]
    assert "缺口 · 源时间未知" in cells[0].get_text()
    assert "行情源缺失 &lt;script&gt;" in body
