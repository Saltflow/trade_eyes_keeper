"""Regression coverage for delivered daily and brief email content."""

import base64
from copy import deepcopy
from datetime import date
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.notification.email_notifier import EmailNotifier


@pytest.fixture
def notifier(monkeypatch, tmp_path):
    instance = EmailNotifier(
        {
            "email": {
                "smtp_server": "smtp.invalid",
                "sender_email": "test@example.invalid",
                "sender_password": "test-password",
                "receiver_email": "test@example.invalid",
                "archive_dir": str(tmp_path),
            }
        }
    )
    monkeypatch.setattr(instance, "_get_server_info", dict)
    monkeypatch.setattr(
        "src.notification.email_notifier._build_signal_label_map", lambda group: {}
    )
    return instance


def test_mobile_daily_renders_signal_details_without_evaluation_reports(notifier):
    scan = SimpleNamespace(
        alerts=[
            {
                "stock_code": "VOO",
                "side": "buy",
                "rule_label": "价值策略 BUY",
                "current_value": 0,
                "detail": "估值 < 门槛 & 满足条件",
                "signal_date": "2026-09-23",
            },
            SimpleNamespace(
                stock_code="00700",
                type="strategy_sell",
                rule_label="风险退出 SELL",
                current_value="评分 1.25",
                detail="风险阈值已触发<script>",
                signal_date="2026-09-23",
            ),
        ]
    )
    body = notifier._build_email_body(
        [],
        pd.DataFrame([{"stock_code": "VOO", "stock_name": "标普500<script>"}]),
        signal_scan=scan,
        evaluation_reports={},
        daily_mode=True,
    )

    signal_section = body.split('class="daily-section signal-section"', 1)[1]
    signal_section = signal_section.split("</section>", 1)[0]
    assert "2条 / 2只" in signal_section
    assert "VOO" in signal_section
    assert "00700" in signal_section
    assert "买入" in signal_section
    assert "卖出" in signal_section
    assert "价值策略 BUY" in signal_section
    assert "风险退出 SELL" in signal_section
    assert "当前值：0" in signal_section
    assert "评分 1.25" in signal_section
    assert "估值 &lt; 门槛 &amp; 满足条件" in signal_section
    assert "风险阈值已触发&lt;script&gt;" in signal_section
    assert "标普500&lt;script&gt;" in signal_section
    assert "2026-09-23" in signal_section
    assert "<script>" not in signal_section


def test_mobile_daily_distinguishes_missing_scan_from_no_signals(notifier):
    missing = notifier._build_email_body([], pd.DataFrame(), daily_mode=True)
    completed = notifier._build_email_body(
        [],
        pd.DataFrame(),
        signal_scan=SimpleNamespace(
            alerts=[],
            group_outcomes={
                group: {"status": "completed_no_signal", "reason": ""}
                for group in ("a_share", "hk", "us")
            },
        ),
        daily_mode=True,
    )

    assert "策略扫描结果不可用" in missing
    assert "本次扫描无触发信号" not in missing
    assert "本次扫描无触发信号" in completed


def test_mobile_daily_keeps_every_supplied_signal(notifier):
    alerts = [
        {
            "stock_code": f"600{index:03d}",
            "side": "buy",
            "rule_label": f"触发规则 {index}",
            "current_value": index,
        }
        for index in range(35)
    ]
    body = notifier._build_email_body(
        [], pd.DataFrame(), signal_scan=SimpleNamespace(alerts=alerts), daily_mode=True
    )

    assert body.count('class="signal-row"') == len(alerts)
    assert "触发规则 34" in body


@pytest.mark.parametrize("with_charts", [False, True])
@pytest.mark.parametrize("with_pdf", [False, True])
def test_sent_mime_has_one_encoding_and_decodable_inline_images(
    notifier, monkeypatch, with_charts, with_pdf
):
    transport = MagicMock()
    monkeypatch.setattr("smtplib.SMTP_SSL", transport)
    monkeypatch.setattr(
        "src.notification.email_notifier.channel_enabled", lambda *args: True
    )
    monkeypatch.setattr(notifier, "_save_email_copy", lambda *args: "mock.html")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZk"
        "AAAAASUVORK5CYII="
    )
    body = "<html><body>买入信号 &amp; 图表</body></html>"
    if with_charts:
        body = body.replace(
            "</body>", '<img src="cid:chart001"><img src="cid:chart002"></body>'
        )
    notifier._send_email(
        "中文日报",
        body,
        chart_png_bytes=png if with_charts else None,
        portfolio_chart_dict={"overview": png} if with_charts else None,
        pdf_bytes=b"%PDF-test" if with_pdf else None,
    )

    sent = transport.return_value.__enter__.return_value.send_message.call_args.args[0]
    parsed = BytesParser(policy=policy.default).parsebytes(sent.as_bytes())
    html_parts = [
        part for part in parsed.walk() if part.get_content_type() == "text/html"
    ]
    assert len(html_parts) == 1
    html_part = html_parts[0]
    assert len(html_part.get_all("Content-Transfer-Encoding")) == 1
    assert html_part.get_content() == body
    image_parts = [
        part for part in parsed.walk() if part.get_content_type() == "image/png"
    ]
    assert [part["Content-ID"] for part in image_parts] == (
        ["<chart001>", "<chart002>"] if with_charts else []
    )
    assert all(part.get_payload(decode=True) == png for part in image_parts)
    pdf_parts = [
        part for part in parsed.walk() if part.get_content_type() == "application/pdf"
    ]
    assert len(pdf_parts) == int(with_pdf)
    assert not any(part.defects for part in parsed.walk())


def _reference_status(**overrides):
    status = {
        "_label": "美股（停单）",
        "inception_date": "2026-07-14",
        "nav": 100_000.0,
        "nav_return_pct": 0.0,
        "trading_days": 12,
        "cash": 100_000.0,
        "holdings": [],
        "_trading_blocked_reason": "未绑定运行，需手动重置",
        "requires_manual_reset": True,
    }
    status.update(overrides)
    return status


def test_brief_reference_displays_block_reason_without_mutating_account(notifier):
    status = _reference_status()
    original = deepcopy(status)
    session = SimpleNamespace(ref_portfolio_status={"us": status})

    body = notifier._build_ref_portfolio_html(session, date(2026, 9, 23))

    assert "停单原因：未绑定运行，需手动重置" in body
    assert "/ref_date YYYY-MM-DD" in body
    assert "清空三市场持仓" in body
    assert status == original


def test_brief_reference_missing_quotes_do_not_render_as_zero_or_fake_loss(notifier):
    status = _reference_status(
        nav=None,
        nav_return_pct=None,
        cash=10_000.0,
        valuation_complete=False,
        missing_price_codes=["QQQ"],
        valuation_reason="缺少 QQQ 行情",
        holdings=[
            {
                "code": "QQQ",
                "shares": 100,
                "price": None,
                "market_value": None,
                "avg_cost": 400.0,
                "price_available": False,
            }
        ],
    )
    body = notifier._build_ref_portfolio_html(
        SimpleNamespace(ref_portfolio_status={"us": status}), date(2026, 9, 23)
    )

    assert "净值: —" in body
    assert "回报: —" in body
    assert "估值不完整：缺少 QQQ 行情" in body
    assert "<td>QQQ</td><td>100</td><td>—</td><td>—</td>" in body
    assert "<td>400.00</td>" in body
    assert "现金: 10,000.00" in body
    assert "-90.00%" not in body


def test_reference_data_block_does_not_recommend_destructive_reset(notifier):
    body = notifier._build_ref_portfolio_html(
        SimpleNamespace(
            ref_portfolio_status={
                "us": _reference_status(
                    _trading_blocked_reason="公司行为数据不就绪<script>",
                    requires_manual_reset=False,
                )
            }
        ),
        date(2026, 9, 23),
    )

    assert "停单原因：公司行为数据不就绪&lt;script&gt;" in body
    assert "<script>" not in body
    assert "/ref_date" not in body


def test_bound_reference_contract_block_does_not_recommend_reset(notifier):
    body = notifier._build_ref_portfolio_html(
        SimpleNamespace(
            ref_portfolio_status={
                "us": _reference_status(
                    _trading_blocked_reason="固定运行或执行合同不可恢复",
                    requires_manual_reset=False,
                    strategy_run_id="bound-run",
                )
            }
        ),
        date(2026, 9, 23),
    )

    assert "停单原因：固定运行或执行合同不可恢复" in body
    assert "恢复该账户绑定的策略产物及执行合同" in body
    assert "/ref_date" not in body
