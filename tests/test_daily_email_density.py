from types import SimpleNamespace

import pandas as pd

from src.notification.chart_generator import generate_portfolio_overview_chart
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
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    return pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "stock_name": "中国电信<script>",
                "date": today,
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
                "open": 10.0,
                "close": 10.0,
                "ma60": 10.0,
            },
        ]
    )


def _complete_holdout():
    return {
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
                "return": 1.0 + index,
                "excess_return": 0.5,
                "max_drawdown": -2.0,
                "sharpe_ratio": 0.6,
            }
            for index in range(1, 5)
        ],
    }


def test_daily_matrix_keeps_all_markets_marks_status_and_stale_data(monkeypatch):
    notifier = _notifier(monkeypatch)
    scan = SimpleNamespace(
        alerts=[{"stock_code": "VOO", "rule_label": "<b>买入</b>"}]
    )
    body = notifier._build_email_body(
        [{"stock_code": "601728", "condition": "<b>跌破</b>"}],
        _stock_data(),
        signal_scan=scan,
        evaluation_reports={"a_share": _report()},
        daily_mode=True,
    )

    assert "完整行情矩阵" in body
    assert "A股 · 2只" in body
    assert "港股 · 1只" in body
    assert "美股 · 1只" in body
    assert all(code in body for code in ("601728", "00700", "VOO", "000001"))
    assert "行动清单" not in body
    assert "ma60 区间" not in body
    assert "未就绪" in body
    assert "&lt;script&gt;" in body
    assert "<script>" not in body
    assert "mobile-hide" in body
    assert "隐含 Ke" in body
    assert "1 ÷ PE + 2%" in body
    assert "锚值" not in body
    assert "周 NAV 箱线图" not in body
    assert "参考持仓" not in body


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
                        "date": pd.Timestamp.now().strftime("%Y-%m-%d"),
                        "open": 100.0,
                        "close": 101.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    body = notifier._build_email_body([], source_data, daily_mode=True)

    assert "完整行情矩阵<span class=\"section-count\">2只" in body
    assert "601728" in body
    assert "01339" in body
    assert "GOOG" not in body
    assert body.count("未就绪") == 1


def test_daily_holdout_renders_only_when_all_four_windows_are_valid(monkeypatch):
    notifier = _notifier(monkeypatch)
    complete = notifier._build_daily_strategy_section(
        {"a_share": _report(_complete_holdout())}
    )
    incomplete = notifier._build_daily_strategy_section(
        {
            "a_share": _report(
                {
                    "holdout_summary": _complete_holdout()["holdout_summary"],
                    "windows": [],
                }
            )
        }
    )

    assert "H1" in complete and "H4" in complete
    assert "H—" not in complete
    assert "84个月 Holdout：报告尚未形成完整 22/16/2/4 产物" in incomplete
    assert "H—" not in incomplete


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
        [],
        _stock_data(),
        announcements=announcements,
        placements={"000001": {"unlock_date": "2026-10-01"}},
        daily_mode=True,
    )

    assert body.count('class="event-row"') == 9
    assert "javascript:" not in body
    assert "<script>" not in body
    assert "PDF 附件" in body


def test_daily_events_render_pending_stock_and_etf_dividends(monkeypatch):
    notifier = _notifier(monkeypatch)
    body = notifier._build_email_body(
        [],
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
        daily_mode=True,
    )

    assert "行动清单" not in body
    assert "未除权" in body
    assert "已除权未派息" in body
    assert "每股/份现金 0.2000" in body
    assert "每股/份现金 USD 1.5000" in body


def test_daily_events_keep_all_event_types_without_truncation(monkeypatch):
    notifier = _notifier(monkeypatch)
    body = notifier._build_email_body(
        [],
        _stock_data(),
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
        daily_mode=True,
    )

    assert body.count('class="event-row"') == 10
    assert "未除权" in body
    assert "未解禁定增" in body
    assert "公告 4" in body


def test_daily_portfolio_overview_is_one_normalized_chart(monkeypatch):
    reports = {}
    for offset, group in enumerate(("a_share", "hk", "us")):
        report = _report()
        report.nav_dates = pd.date_range("2026-01-01", periods=4, freq="B").astype(
            str
        ).tolist()
        report.nav_series = [100000.0, 101000.0 + offset, 99000.0, 103000.0]
        reports[group] = report

    png = generate_portfolio_overview_chart(reports)
    notifier = _notifier(monkeypatch)
    section = notifier._daily_portfolio_chart_section({"overview": png})

    assert png.startswith(b"\x89PNG")
    assert 'src="cid:chart002"' in section
    assert "chart003" not in section
    assert "chart004" not in section
