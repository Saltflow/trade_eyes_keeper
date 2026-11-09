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
            "hostname": "daily-density-node",
            "ip_address": "203.0.113.8",
            "system": "Linux",
            "machine": "x86_64",
            "kernel_version": "6.1",
        },
    )
    return notifier


def _report(selection_diagnostics=None):
    return SimpleNamespace(
        strategy_label="策略<script>",
        total_return=4.2,
        excess_return=1.5,
        max_drawdown=-3.0,
        sharpe_ratio=1.2,
        trade_count=8,
        primary_benchmark="510300",
        benchmark_returns={"510300": 2.7},
        selection_diagnostics=selection_diagnostics or {},
        nav_dates=[],
        nav_series=[],
        weekly_nav_ohlc={},
    )


def _stock_data():
    today = "2026-09-30"
    return pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "stock_name": "中国电信<script>",
                "date": today,
                "quote_status": "realtime",
                "quote_as_of": "2026-09-30T14:59:00+08:00",
                "open": 6.1,
                "close": 6.2,
                "ma60": 6.0,
                "pe_ratio": 10.0,
                "pb_ratio": 1.2,
            },
            {
                "stock_code": "00700",
                "stock_name": "腾讯控股",
                "date": today,
                "quote_status": "delayed",
                "quote_as_of": "2026-09-30T14:44:00+08:00",
                "open": 420.0,
                "close": 419.0,
                "ma60": 430.0,
                "pe_ratio": 20.0,
                "pb_ratio": 3.0,
            },
            {
                "stock_code": "VOO",
                "stock_name": "Vanguard 标普500",
                "date": today,
                "quote_status": "last_close",
                "quote_as_of": "2026-09-29T16:00:00-04:00",
                "open": 700.0,
                "close": 701.0,
                "ma60": 680.0,
                "pe_ratio": 25.0,
                "pb_ratio": 4.0,
            },
            {
                "stock_code": "000001",
                "stock_name": "过期数据",
                "date": "2020-01-01",
                "quote_status": "unavailable",
                "quote_as_of": "2020-01-01T15:00:00+08:00",
                "quote_reason": "历史报价已过期 <script>",
                "open": 10.0,
                "close": 10.0,
                "ma60": 10.0,
                "pe_ratio": 8.0,
                "pb_ratio": 1.2,
                "roe": 15.0,
            },
        ]
    )


def _complete_holdout():
    return {
        "time_contract": {
            "holdout_test_months": 9,
            "holdout_window_count": 4,
            "holdout_calendar_span_months": 18,
        },
        "holdout_summary": {
            "return_pct": 2.0,
            "excess_return_pct": 1.0,
            "max_drawdown_pct": -4.0,
            "sharpe_ratio": 0.8,
        },
        "windows": [
            {
                "role": "holdout",
                "role_index": index,
                "return": 2.0,
                "excess_return": 0.5,
                "max_drawdown": -2.0,
                "sharpe_ratio": 0.6,
            }
            for index in range(1, 5)
        ],
    }


def test_daily_matrix_keeps_all_markets_prioritizes_actions_without_badges(monkeypatch):
    notifier = _notifier(monkeypatch)
    SimpleNamespace(alerts=[{"stock_code": "VOO", "rule_label": "<b>买入</b>"}])
    body = notifier._build_email_body(
        _stock_data(),
    )

    assert "完整行情矩阵" in body
    assert "A股 · 2只" in body
    assert "港股 · 1只" in body
    assert "美股 · 1只" in body
    assert all(code in body for code in ("601728", "00700", "VOO", "000001"))
    assert "行动清单" not in body
    assert "ma60 区间" not in body
    soup = BeautifulSoup(body, "html.parser")
    summary = {
        cell.select_one(".summary-label").get_text(): cell.select_one(
            ".summary-value"
        ).get_text()
        for cell in soup.select(".summary-cell")
    }
    assert {
        key: summary[key] for key in ("实时报价", "延迟采样", "最近收盘", "行情缺口")
    } == {key: "1" for key in ("实时报价", "延迟采样", "最近收盘", "行情缺口")}
    rows = soup.select(".watchlist-table tbody tr")
    assert len(rows) == 4
    stale = next(row for row in rows if row.select_one("strong").get_text() == "000001")
    cells = stale.select("td")
    assert [cell.get_text() for cell in cells[1:3]] == ["—", "—"]
    assert cells[3].strong.get_text() == "—"
    assert cells[3].select_one(".price-change").get_text() == "涨跌未知"
    assert [cell.get_text() for cell in cells[4:]] == ["8.0", "1.20", "14.5%", "15.00"]
    assert "缺口" in stale.select_one(".status-badge").get_text()
    assert "2020-01-01 15:00:00+08:00" in stale.get_text()
    assert "&lt;script&gt;" in body
    assert "<script>" not in body
    assert "mobile-hide" in body
    assert "隐含 Ke" in body
    assert "1 ÷ PE + 2%" in body
    assert "锚值" not in body
    assert "周 NAV 箱线图" not in body
    assert "参考持仓" not in body
    matrix = body[body.index("完整行情矩阵") :]
    assert "status-badge status-alert" not in matrix
    assert "status-badge status-signal" not in matrix


def test_daily_matrix_uses_configured_pool_and_keeps_missing_data(monkeypatch):
    notifier = _notifier(monkeypatch)
    notifier.config["stocks"] = ["601728", "01339"]
    source_data = pd.concat(
        [
            _stock_data(),
            pd.DataFrame(
                [
                    {
                        "stock_code": "GOOG",
                        "stock_name": "Google",
                        "date": "2026-09-30",
                        "quote_status": "delayed",
                        "quote_as_of": "2026-09-30T10:00:00-04:00",
                        "open": 100.0,
                        "close": 101.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    body = notifier._build_email_body(source_data)

    assert '完整行情矩阵<span class="section-count">2只' in body
    assert "601728" in body
    assert "01339" in body
    assert "GOOG" not in body
    soup = BeautifulSoup(body, "html.parser")
    rows = soup.select(".watchlist-table tbody tr")
    assert [row.select_one("strong").get_text() for row in rows] == ["601728", "01339"]
    missing = rows[1].select("td")
    assert "源时间未知" in missing[0].get_text()
    assert [cell.get_text() for cell in missing[1:3]] == ["—", "—"]
    assert missing[3].strong.get_text() == "—"
    assert [cell.get_text() for cell in missing[4:]] == ["—"] * 4
    assert rows[0].select_one(".current-quote strong").get_text() == "6.20"
    gap = next(
        cell
        for cell in soup.select(".summary-cell")
        if cell.select_one(".summary-label").get_text() == "行情缺口"
    )
    assert gap.select_one(".summary-value").get_text() == "1"


def test_daily_events_are_complete_and_urls_are_sanitized(monkeypatch):
    notifier = _notifier(monkeypatch)
    announcements = {
        "601728": [
            {
                "date": f"2026-09-{day:02d}",
                "title": f"公告 {day} <script>",
                "url": "javascript:alert(1)",
            }
            for day in range(1, 9)
        ]
    }
    body = notifier._build_email_body(
        _stock_data(),
        announcements=announcements,
        placements={"000001": {"unlock_date": "2026-10-01"}},
        report_as_of="2026-09-30T19:00:00+08:00",
    )

    assert body.count('class="event-row"') == 9
    assert "javascript:" not in body
    assert "<script>" not in body
    assert "公告展示近2个自然月" in body


def test_daily_events_render_pending_stock_and_etf_dividends(monkeypatch):
    notifier = _notifier(monkeypatch)
    body = notifier._build_email_body(
        _stock_data(),
        dividend_events=[
            {
                "code": "601728",
                "status": "未除权",
                "ex_date": "2026-09-24",
                "payment_date": "2026-09-30",
                "cash_per_share": 0.2,
            },
            {
                "code": "VOO",
                "status": "已除权未派息",
                "ex_date": "2026-09-18",
                "payment_date": "2026-09-22",
                "cash_per_share": 1.5,
                "currency": "USD",
            },
        ],
    )

    assert "行动清单" not in body
    assert "未除权" in body
    assert "除权 2026-09-24 · 派息 2026-09-30" in body
    assert "除权 2026-09-18 · 派息 2026-09-22" in body
    assert "每股/份现金 0.2000" in body
    assert "每股/份现金 USD 1.5000" in body


def test_daily_events_keep_all_event_types_without_truncation(monkeypatch):
    notifier = _notifier(monkeypatch)
    body = notifier._build_email_body(
        _stock_data(),
        report_as_of="2026-09-30T19:00:00+08:00",
        announcements={
            "601728": [
                {
                    "date": f"2026-09-{day:02d}",
                    "title": f"公告 {day}",
                    "url": "https://example.com/notice",
                }
                for day in range(1, 5)
            ]
        },
        placements={
            f"00000{index}": {"unlock_date": f"2026-10-{index:02d}"}
            for index in range(1, 4)
        },
        dividend_events=[
            {
                "code": f"51030{index}",
                "status": "未除权",
                "ex_date": f"2026-09-{20 + index:02d}",
                "payment_date": "2026-10-01",
                "cash_per_share": 0.1,
            }
            for index in range(1, 4)
        ],
    )

    assert body.count('class="event-row"') == 10
    assert "未除权" in body
    assert "未解禁定增" in body
    assert "公告 4" in body
