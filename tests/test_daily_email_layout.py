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
                "close": 10.2,
                "open": 10.0,
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


def _daily_body(notifier, *, alerts=None, signal_scan=None, ref_html=""):
    return notifier._build_email_body(
        alerts or [],
        _stock_data(),
        announcements={
            "601728": [
                {
                    "title": "<b>恶意公告</b>" + "公告标题" * 30,
                    "url": "javascript:alert(1)",
                    "date": "2026-08-22",
                    "exchange": "SSE",
                },
                {
                    "title": "安全公告",
                    "url": "https://example.com/notice",
                    "date": "2026-08-22",
                    "exchange": "SSE",
                },
            ]
        },
        signal_scan=signal_scan,
        ref_portfolio_html=ref_html,
        daily_mode=True,
    )


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
        assert "监控标的" in body
        assert "价格 / 锚点" in body

    assert alert_body.count("<html") == empty_body.count("<html") == 1
    assert alert_body.find("策略信号与组合表现") >= 0
    assert empty_body.find("策略信号与组合表现") >= 0


def test_daily_data_is_escaped_and_reference_portfolio_stays_inside_document(monkeypatch):
    notifier = _notifier(monkeypatch)
    session = SimpleNamespace(
        ref_portfolio_status={
            "a_share": {
                "_label": "A股参考组合",
                "inception_date": "2026-01-01",
                "nav": 100000,
                "nav_return_pct": 3.2,
                "trading_days": 100,
                "holdings": [
                    {
                        "code": "601728",
                        "shares": 100,
                        "price": 10.2,
                        "market_value": 1020,
                        "avg_cost": 9.9,
                    }
                ],
                "cash": 98980,
            }
        }
    )
    ref_html = notifier._build_daily_ref_portfolio_html(session)
    body = _daily_body(notifier, ref_html=ref_html)
    soup = BeautifulSoup(body, "html.parser")

    assert soup.find("script") is None
    assert "&lt;script&gt;" in body
    assert "javascript:" not in body
    assert 'href="https://example.com/notice"' in body
    assert body.find("参考持仓") < body.find("</html>")
    assert body[body.find("</html>") + len("</html>") :].strip() == ""
    assert len(soup.find_all("table")) <= 3


def test_daily_missing_values_and_long_text_do_not_break_html(monkeypatch):
    notifier = _notifier(monkeypatch)
    sparse = pd.DataFrame(
        [{
            "stock_code": "000001",
            "stock_name": "名称" * 100,
            "close": None,
            "ma60": None,
        }]
    )
    body = notifier._build_email_body(
        [],
        sparse,
        announcements={"000001": [{"title": "标题" * 200}]},
        daily_mode=True,
    )
    soup = BeautifulSoup(body, "html.parser")
    assert soup.find("script") is None
    assert len(soup.find_all("html")) == 1
    assert body.strip().endswith("</html>")
    assert "000001" in body
    assert "—" in body
