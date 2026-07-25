"""日报完整链路端到端测试。

验证 run_daily_task 跑通后邮件 HTML 的策略结果段非空、含真数据、收益非零。
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
def test_daily_report_strategy_section_not_empty():
    """run_daily_task 完成后，最新邮件 HTML 的策略段不能是空占位。"""
    os.environ["SKIP_EMAIL"] = "true"
    os.environ["BOT_FORCE"] = "1"

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

    # 数据关键词存在
    found = any(
        kw in section
        for kw in ["验证期涨幅", "最大回撤", "超额", "夏普", "平均现金仓位", "基准"]
    )
    assert found, (
        f"策略结果段为空：只含占位标记。"
        f"邮件: {latest.name}\n"
        f"段内容: {section[:300]}"
    )

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
