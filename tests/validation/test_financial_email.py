import os
from pathlib import Path

import pytest

from src.email_notifier import EmailNotifier
from tests.validation.test_utils import RandomTestParameterGenerator


def _build_notifier(tmp_path):
    # 使用绝对路径，避免工作目录漂移
    archive_dir = tmp_path / "archive"
    config = {
        "email": {
            "archive_dir": str(archive_dir),
            "sender_email": "test@example.com",
            "sender_password": "pwd",
            "receiver_email": "recv@example.com",
        }
    }
    return EmailNotifier(config)


def test_financial_analysis_placeholder(tmp_path):
    notifier = _build_notifier(tmp_path)

    html = notifier._build_financial_analysis_section({})

    assert "财报分析" in html
    assert "暂无可用财报分析数据" in html


def test_financial_analysis_render_card(tmp_path):
    notifier = _build_notifier(tmp_path)

    sample = {
        "600000": [
            {
                "stock_code": "600000",
                "report_type": "年度报告",
                "period_date": "2024-12-31",
                "analysis": {
                    "cost_structure": "成本下降",
                    "profit_changes": "利润提升",
                    "liquidation_value": "资产稳健",
                    "audit_risks": "风险可控",
                    "overall_assessment": "表现良好",
                },
            }
        ]
    }

    html = notifier._build_financial_analysis_section(sample)

    assert "600000" in html
    assert "年度报告" in html
    assert "成本下降" in html
    assert "利润提升" in html


def test_save_email_copy_uses_absolute_archive(tmp_path):
    rng = RandomTestParameterGenerator()
    notifier = _build_notifier(tmp_path)

    subject = f"测试主题-{rng.random_stock_code()}"
    body = "<p>正文</p>"

    path = notifier._save_email_copy(subject, body)

    assert path is not None
    saved = Path(path)
    assert saved.exists()
    # 确认使用绝对路径
    assert saved.is_absolute()
    assert str(tmp_path) in str(saved)
