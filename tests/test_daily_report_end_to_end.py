"""日报完整链路端到端测试。

验证 run_daily_task 跑通后邮件 HTML 的策略结果段要么包含 active 策略，
要么明确报告市场未激活；不能静默套用旧策略。
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.mark.skipif(
    os.getenv("CI") == "true", reason="CI 环境不跑完整日报数据拉取"
)
def test_daily_report_strategy_section_is_active_or_fail_closed(monkeypatch):
    """日报没有 active entry 时必须显式 fail closed。"""
    # Avoid leaking transport/test flags into later notifier tests in the same
    # pytest process. monkeypatch restores both variables after this test.
    monkeypatch.setenv("SKIP_EMAIL", "true")
    monkeypatch.setenv("SKIP_FEISHU", "true")
    monkeypatch.setenv("SKIP_TELEGRAM", "true")
    monkeypatch.setenv("BOT_FORCE", "1")

    from main import run_daily_task

    run_daily_task(force=True)

    archive = Path("data/email_archive")
    files = list(archive.glob("*.html"))
    if not files:
        pytest.fail("日报未生成邮件存档")

    latest = max(files, key=lambda p: p.stat().st_mtime)
    html = latest.read_text(encoding="utf-8")

    # 策略结果段不是空占位（如果为空 → 只有 <!-- Strategy Search Results --> 后直接跟下一段）
    marker = "<!-- Strategy Search Results -->"
    start = html.find(marker)
    assert start >= 0, f"HTML 缺少策略段标记。邮件: {latest.name}"

    # 取策略段到下一个段标记之间的内容
    next_section = html.find("<!-- Alerts -->", start)
    if next_section < 0:
        next_section = html.find("<!-- Monitoring -->", start)
    section = html[start:next_section] if next_section > start else html[start:]

    # 没有 v4 active entry 时，严格按市场 fail closed；不能读取旧配置。
    found = any(
        kw in section
        for kw in ["验证期涨幅", "最大回撤", "超额", "夏普", "平均现金仓位", "基准"]
    )
    if not found:
        assert any(
            marker in section for marker in ["未激活", "没有有效策略", "无 active"]
        ), f"策略结果段既无 active 数据也无 fail-closed 提示: {latest.name}"
        return

    # 验证期涨幅 ≠ 0.0%（零交易的特征）
    m = re.search(
        r"验证期涨幅[^<]*<span[^>]*>([+-][\d.]+)%</span>", section
    )
    if m:
        ret_val = float(m.group(1))
        assert ret_val != 0.0, (
            f"策略收益为 0.0%（疑似全现金零交易）。"
            f"邮件: {latest.name}"
        )
