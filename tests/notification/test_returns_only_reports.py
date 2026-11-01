"""Offline renderer contracts with labelled synthetic NAV and fundamentals."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from src.notification import daily_pdf
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.report_builders import (
    build_strategy_text_summary,
    report_data_readiness,
)
from src.notification.report_policy import (
    build_return_curves_text,
    generate_return_curve_png,
    report_view,
    return_curve_markets,
    technical_signals_enabled,
)
from src.notification.telegram_notifier import TelegramNotifier

CONFIG = {
    "report": {
        "technical_signals": {"enabled": False},
        "strategy_display": "returns_only",
    }
}


def curves():
    return {
        "strategy_id": "ke_threshold_allocation",
        "markets": {
            "a_share": {
                "dates": ["2026-01-01", "2026-01-02", "2026-01-03"],
                "values": [990, 1050, 1100],
                "initial_capital": 1000,
                "coverage": {"status": "complete"},
                "data_as_of": "2026-01-03",
            },
            "hk": {
                "dates": ["2026-01-01", "2026-01-02"],
                "values": [1000, 900],
                "initial_capital": 1000,
                "coverage": {"status": "partial", "message": "合成历史Ke缺口"},
            },
            "us": {
                "dates": ["2026-01-01", "2026-01-02"],
                "values": [1000, 1000],
                "initial_capital": 1000,
                "coverage": {"status": "unavailable"},
            },
        },
    }


def session():
    frame = pd.DataFrame(
        [
            {
                "stock_code": "601088",
                "stock_name": "合成公司",
                "date": "2026-01-01",
                "close": 10,
                "dividend_yield": 4.2,
                "pe_ratio": 9,
                "pb_ratio": 1.2,
                "roe": 13,
                "rsi": 99,
                "ma60": 88,
                "macd_hist": 66,
            },
            {"stock_code": "00700", "stock_name": "无均线合成公司", "close": 12},
        ]
    )
    scan = SimpleNamespace(
        alerts=[
            SimpleNamespace(
                stock_code="601088", rule_label="OLD_TECH_BUY", current_value=99
            )
        ],
        indicator_snapshot={"601088": {"rsi": 99}},
        group_outcomes={"hk": {"status": "blocked"}},
    )
    return SimpleNamespace(
        config=deepcopy(CONFIG),
        signal_scan=scan,
        evaluation_reports={
            "a_share": SimpleNamespace(strategy_label="OLD_TECH_ENGINE")
        },
        backtest={"a_share": {"total_return": 999}},
        ref_portfolio_status={"a_share": {"_label": "OLD_REFERENCE"}},
        strategy_return_curves=curves(),
        stocks_data={"601088": {}, "00700": {}},
        announcements={
            "601088": [
                {
                    "title": "合成三季报_全文",
                    "date": "2026-01-01",
                    "url": "https://disclosure.example.test/fixture.pdf",
                }
            ]
        },
        placements={},
        dividend_events=[],
        get_all_dataframe=lambda: frame,
        get_alerts_as_dicts=lambda: [
            {"stock_code": "601088", "rule_label": "OLD_TECH_BUY"}
        ],
    )


def assert_clean(text):
    for forbidden in (
        "OLD_TECH",
        "OLD_REFERENCE",
        "RSI",
        "MACD",
        "锚点",
        "偏离",
        "买入阈值",
        "三市场决策板",
    ):
        assert forbidden not in text
    assert "601088" in text and "00700" in text
    assert "股息率" in text and "ROE" in text


def test_default_legacy_policy_and_non_mutating_stale_session_projection():
    assert technical_signals_enabled({})
    original = session()
    projected = report_view(original)
    assert projected is not original
    assert projected.signal_scan is None
    assert projected.evaluation_reports == {}
    assert projected.ref_portfolio_status == {}
    assert original.signal_scan.alerts[0].rule_label == "OLD_TECH_BUY"
    assert original.evaluation_reports
    assert projected.announcements is original.announcements
    assert report_view(original, {}) is original


def test_return_identity_coverage_and_initial_capital_are_not_inferred():
    payload = curves()
    text = build_return_curves_text(payload)
    assert "+10.00%" in text  # 1100 / initial 1000, never 1100 / first NAV 990.
    assert "-10.00%" in text
    assert "历史Ke覆盖不完整" in text
    assert "美股：暂不可用" in text and "+0.00%" not in text
    payload["strategy_id"] = "technical_ensemble"
    assert all(
        item["status"] == "unavailable" for item in return_curve_markets(payload)
    )
    assert generate_return_curve_png(payload) is None
    assert "999" not in build_strategy_text_summary(session())


@pytest.mark.parametrize(
    "field,value",
    [
        ("values", [1000, float("nan"), 1001]),
        ("values", [1000, 1001]),
        ("initial_capital", 0),
        ("dates", ["2026-01-02", "2026-01-01", "2026-01-03"]),
        ("dates", ["2026-01-01"] * 3),
        ("coverage", {}),
    ],
)
def test_malformed_curve_fails_closed(field, value):
    payload = curves()
    payload["markets"]["a_share"][field] = value
    assert return_curve_markets(payload)[0]["status"] == "unavailable"


def test_png_has_partial_watermark_and_no_unavailable_zero_return_line(monkeypatch):
    import matplotlib.pyplot as plt

    original = plt.subplots
    captured = []

    def subplots(*args, **kwargs):
        figure, axes = original(*args, **kwargs)
        captured.extend(axes)
        return figure, axes

    monkeypatch.setattr(plt, "subplots", subplots)
    assert generate_return_curve_png(curves()).startswith(b"\x89PNG")
    assert captured[0].lines[0].get_ydata().tolist() == [-1, 5, pytest.approx(10)]
    assert any("覆盖不完整" in text.get_text() for text in captured[1].texts)
    assert not captured[2].lines


@pytest.mark.parametrize(
    "method", ["send_from_session", "send_daily_report_from_session"]
)
def test_daily_email_uses_only_ke_curve_and_retains_full_fundamentals_events(
    monkeypatch, method
):
    notifier = EmailNotifier(deepcopy(CONFIG))
    sent = []
    monkeypatch.setattr(
        notifier,
        "_send_email",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"status": "sent"},
    )
    monkeypatch.setattr(notifier, "_generate_daily_pdf", lambda *args: None)
    monkeypatch.setattr(
        "src.notification.email_notifier.generate_return_curve_png",
        lambda payload: b"synthetic-png",
    )
    monkeypatch.setattr(
        "src.notification.email_notifier.generate_portfolio_overview_chart",
        lambda *args: pytest.fail("old technical chart requested"),
    )
    original = session()
    assert getattr(notifier, method)(original)["status"] == "sent"
    body = sent[0][0][1]
    assert_clean(body)
    assert "合成三季报_全文" in body and "fixture.pdf" in body
    assert "cid:chart002" in body and "Ke策略收益曲线" in body
    assert sent[0][1]["portfolio_chart_dict"] == {"overview": b"synthetic-png"}
    assert original.signal_scan is not None


@pytest.mark.parametrize("kind", ["email", "feishu", "telegram"])
def test_manual_brief_has_full_price_fundamental_pool_without_technical_fields(kind):
    cls = {
        "email": EmailNotifier,
        "feishu": FeishuNotifier,
        "telegram": TelegramNotifier,
    }[kind]
    result = cls(deepcopy(CONFIG))._prepare_brief_report(
        session(), {"label": "手动简报"}
    )
    text = (
        json.dumps(result, ensure_ascii=False)
        if isinstance(result, dict)
        else str(result)
    )
    assert_clean(text)
    assert "Ke策略收益曲线" in text
    assert "暂不可用" in text


@pytest.mark.parametrize("cls", [FeishuNotifier, TelegramNotifier])
def test_bot_daily_retains_events_and_never_calls_legacy_technical_builder(
    monkeypatch, cls
):
    notifier = cls(deepcopy(CONFIG))
    pieces = []
    monkeypatch.setattr(
        notifier,
        "_send",
        lambda *args, **kwargs: pieces.append((args, kwargs)) or {"status": "sent"},
    )
    legacy = (
        "_build_report_sections" if cls is FeishuNotifier else "_build_daily_sections"
    )
    monkeypatch.setattr(
        notifier,
        legacy,
        lambda *args, **kwargs: pytest.fail("legacy technical builder called"),
    )
    assert notifier.send_daily_report_from_session(session())["status"] == "sent"
    text = json.dumps(pieces, ensure_ascii=False)
    assert_clean(text)
    assert "合成三季报_全文" in text and "fixture.pdf" in text
    assert "历史Ke覆盖不完整" in text


def test_pdf_uses_returns_branch_and_keeps_escaped_fundamentals_and_events(monkeypatch):
    captured = []

    def compile_pdf(path):
        captured.append(path.read_text(encoding="utf-8"))
        return b"%PDF-synthetic"

    monkeypatch.setattr(daily_pdf, "_compile_pdf", compile_pdf)
    monkeypatch.setattr(
        "src.notification.report_policy.generate_return_curve_png",
        lambda payload: b"synthetic-png",
    )
    source = session()
    result = daily_pdf.generate_daily_pdf(
        source,
        [],
        source.signal_scan,
        source.backtest,
        source.get_all_dataframe(),
        chart_deviation_timeline=lambda *args, **kwargs: pytest.fail(
            "old indicator chart requested"
        ),
        pick_best_anchor=lambda *args: pytest.fail("old anchor requested"),
        get_server_info=dict,
    )
    assert result == b"%PDF-synthetic"
    assert_clean(captured[0])
    assert "三季报\\_全文" in captured[0] and "fixture.pdf" in captured[0]
    assert "\\includegraphics" in captured[0] and "历史Ke覆盖不完整" in captured[0]
    assert "\r" not in captured[0]


def test_disabled_scan_does_not_make_fundamental_data_readiness_blocked():
    source = session()
    source.config["stocks"] = [{"code": "601088"}, {"code": "00700"}]
    assert report_data_readiness(source)["status"] == "ready"
