"""
早盘简报测试

锚点择优算法 + 简报邮件渲染 + 交易日过滤
"""

from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import pytest

from src.notification.email_notifier import EmailNotifier

# ════════════════════════════════════════════════════════
# _pick_best_anchor 测试
# ════════════════════════════════════════════════════════


class TestSendBriefReport:
    """简报邮件测试"""

    @pytest.fixture
    def mock_session(self):
        """构建模拟 Session（日期用今天，避免时间漂移导致过滤）"""
        # Fixture models the existing local calendar/date filter, not an instant.
        today = datetime.now()  # noqa: DTZ005
        session = Mock()
        session.signal_scan = None  # 避免 Mock.alerts len() 报错
        session.get_all_dataframe.return_value = pd.DataFrame(
            [
                {
                    "stock_code": "601728",
                    "stock_name": "中国电信",
                    "date": today,
                    "open": 5.70,
                    "close": 5.76,
                    "ma60": 5.91,
                    "wma20": 5.78,
                    "wma30": 5.65,
                    "wma50": 5.50,
                },
                {
                    "stock_code": "VOO",
                    "stock_name": "标普500",
                    "date": today,  # 今天 → 活跃
                    "open": 485.0,
                    "close": 486.5,
                    "ma60": 480.0,
                    "wma20": 485.0,
                    "wma30": 483.0,
                    "wma50": 478.0,
                },
                {
                    "stock_code": "00883",
                    "stock_name": "中海油",
                    "date": today,
                    "open": 18.50,
                    "close": 18.62,
                    "ma60": 18.00,
                    "wma20": 18.40,
                    "wma30": 17.80,
                    "wma50": 17.50,
                },
            ]
        )
        return session

    def test_active_count_displayed(self, mock_session):
        """活跃标的数量正确（3天内=活跃）"""
        notifier = EmailNotifier({"email": {}})
        with patch.object(notifier, "_send_email") as mock_send:
            notifier.send_brief_report(
                mock_session,
                {"id": "test", "label": "测试简报"},
            )
            body = mock_send.call_args[0][1]
            assert "3/3" in body  # 3只都在3天窗口内

    def test_subject_uses_label_and_date(self, mock_session):
        """主题包含标签和日期"""
        notifier = EmailNotifier({"email": {}})
        with patch.object(notifier, "_send_email") as mock_send:
            notifier.send_brief_report(
                mock_session,
                {"id": "morning", "label": "早盘简报"},
            )
            subject = mock_send.call_args[0][0]
            assert "早盘简报" in subject

    def test_empty_session_no_error(self):
        """空 Session 不抛异常"""
        session = Mock()
        session.signal_scan = None  # 避免 Mock.alerts len() 报错
        session.get_all_dataframe.return_value = pd.DataFrame()
        notifier = EmailNotifier({"email": {}})
        with patch.object(notifier, "_send_email") as mock_send:
            notifier.send_brief_report(session, {"id": "test", "label": "空测试"})
            assert mock_send.called


# ════════════════════════════════════════════════════════
# 集成: run_brief_report 测试
# ════════════════════════════════════════════════════════


class TestRunBriefReportIntegration:
    """run_brief_report 集成测试"""

    def test_weekend_skip(self):
        """周末跳过整个简报"""
        from main import run_brief_report

        # 用 MagicMock 替换 main.logging，避免 run_brief_report 的 logger
        # 调用写入 pytest 捕获流后被关闭引发 'I/O operation on closed file'
        with patch("main.logging") as _mock_log, patch(
            "main.load_config"
        ) as mock_config:
            _mock_log.getLogger.return_value = MagicMock()
            mock_config.return_value = {
                "scheduler": {
                    "brief_reports": [{"id": "morning_snapshot", "label": "早盘简报"}]
                },
                "stocks": ["601728"],
            }
            with patch("main.datetime") as mock_dt:
                # 周六
                # Fixture models the existing local calendar/date filter, not an instant.
                mock_dt.now.return_value = datetime(2026, 4, 25, 9, 50)  # noqa: DTZ001
                # Fixture models the existing local calendar/date filter, not an instant.
                mock_dt.date.return_value = datetime(2026, 4, 25).date()  # noqa: DTZ001
                with patch("main.StockDataFetcher") as mock_fetcher:
                    run_brief_report("morning_snapshot")
                    # 周末跳过 → 不应该尝试 fetch
                    mock_fetcher.assert_not_called()
