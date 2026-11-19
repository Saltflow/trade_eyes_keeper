"""Offline report contracts: fundamentals/events remain, Ke curves are retired."""

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.notification import daily_pdf
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.report_builders import report_data_readiness
from src.notification.report_policy import format_roe
from src.notification.telegram_notifier import TelegramNotifier

CONFIG = {}


class RetiredCurvePayload:
    """Old persisted payloads cannot reactivate report rendering or validation."""

    def __getattribute__(self, name):
        raise AssertionError("reports must not consume retired Ke curves")


def session():
    frame = pd.DataFrame(
        [
            {
                "stock_code": "601088",
                "stock_name": "合成公司",
                "date": datetime.now().astimezone().date().isoformat(),
                "close": 10,
                "dividend_yield": 4.2,
                "pe_ratio": 9,
                "pb_ratio": 1.2,
                "roe": 13,
                "rsi": 99,
                "ma60": 88,
                "macd_hist": 66,
            },
            {
                "stock_code": "00700",
                "stock_name": "无均线合成公司",
                "close": 12,
                "date": datetime.now().astimezone().date().isoformat(),
            },
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
        created_at="2026-01-03T19:00:00+08:00",
        config=deepcopy(CONFIG),
        signal_scan=scan,
        evaluation_reports={
            "a_share": SimpleNamespace(strategy_label="OLD_TECH_ENGINE")
        },
        backtest={"a_share": {"total_return": 999}},
        ref_portfolio_status={"a_share": {"_label": "OLD_REFERENCE"}},
        strategy_return_curves=RetiredCurvePayload(),
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
        "Ke策略收益曲线",
        "Ke收益曲线",
        "历史Ke覆盖不完整",
        "历史模拟",
        "A股策略",
        "港股策略",
        "美股策略",
        "cid:chart002",
    ):
        assert forbidden not in text
    assert "601088" in text and "00700" in text
    assert "股息率" not in text and "ROE" in text


@pytest.mark.parametrize(
    "roe,pe,pb,expected",
    [
        (12, 10, 1.5, "12.00"),
        (0, 10, 1.5, "0.00"),
        (None, 10, 1.5, "15.00(估)"),
        (float("nan"), -10, 1.5, "-15.00(估)"),
        (None, 0, 1.5, "—"),
        (None, float("inf"), 1.5, "—"),
        (None, 10, None, "—"),
        (None, 10, -1, "—"),
    ],
)
def test_roe_display_prefers_reported_and_labels_finite_pb_pe_estimates(
    roe, pe, pb, expected
):
    row = {"roe": roe, "pe_ratio": pe, "pb_ratio": pb}
    assert format_roe(row) == expected
    assert row["roe"] is roe


@pytest.mark.parametrize(
    "method", ["send_from_session", "send_daily_report_from_session"]
)
def test_daily_email_ignores_retired_curves_and_retains_fundamentals_events_pdf(
    monkeypatch, method
):
    notifier = EmailNotifier({})
    sent = []
    monkeypatch.setattr(
        notifier,
        "_send_email",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"status": "sent"},
    )
    pdf = b"%PDF-1.7\ncontrolled report fixture\n%%EOF"
    monkeypatch.setattr(notifier, "_generate_daily_pdf", lambda *args: pdf)
    original = session()
    assert getattr(notifier, method)(original)["status"] == "sent"
    body = sent[0][0][1]
    assert "<style>" in body and 'name="viewport"' in body
    assert 'class="shell"' in body and "max-width:680px" in body
    assert "股票量化日报" in body and "完整行情矩阵" in body
    assert "扫描未完成" not in body
    assert_clean(body)
    assert "合成三季报_全文" in body and "fixture.pdf" in body
    assert "隐含 Ke" in body and "13.1%" in body
    assert "portfolio_chart_dict" not in sent[0][1]
    assert sent[0][1]["pdf_bytes"] == pdf
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
    if kind == "email":
        assert "<style>" in text and "检测时间" in text
        assert "<th>ROE%" in text and "股息率" not in text


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
    assert notifier.send_daily_report_from_session(session())["status"] == "sent"
    text = json.dumps(pieces, ensure_ascii=False)
    assert_clean(text)
    assert "合成三季报_全文" in text and "fixture.pdf" in text
    assert "PE" in text and "PB" in text


def test_pdf_uses_original_template_and_keeps_escaped_fundamentals_and_events(
    monkeypatch,
):
    captured = []

    def compile_pdf(path):
        captured.append(path.read_text(encoding="utf-8"))
        return b"%PDF-synthetic"

    monkeypatch.setattr(daily_pdf, "_compile_pdf", compile_pdf)
    source = session()
    result = daily_pdf.generate_daily_pdf(
        source,
        source.get_all_dataframe(),
        get_server_info=dict,
    )
    assert result == b"%PDF-synthetic"
    assert "Stock Quant Report" in captured[0]
    assert "\\VAR{" not in captured[0]
    assert_clean(captured[0])
    assert "三季报\\_全文" in captured[0] and "fixture.pdf" in captured[0]
    assert "\\includegraphics" not in captured[0]
    assert "隐含 Ke" in captured[0] and "13.1\\%" in captured[0]
    assert "数据状态" in captured[0]
    assert "\r" not in captured[0]


def test_disabled_scan_does_not_make_fundamental_data_readiness_blocked():
    source = session()
    source.config["stocks"] = [{"code": "601088"}, {"code": "00700"}]
    source.report_quote_quality = {
        "quotes": {
            code: {"quote_status": "last_close", "quote_reason": ""}
            for code in ("601088", "00700")
        },
        "counts": {"last_close": 2},
    }
    assert report_data_readiness(source)["status"] == "ready"


def test_daily_template_keeps_full_pool_without_retired_curve_placeholder():
    source = session()
    body = EmailNotifier(deepcopy(CONFIG))._build_email_body(
        source.get_all_dataframe(),
    )
    assert "<style>" in body and 'class="shell"' in body
    assert "完整行情矩阵" in body
    assert "暂不可用" not in body and "cid:chart002" not in body
    assert_clean(body)
