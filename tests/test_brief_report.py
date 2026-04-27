"""
早盘简报测试

锚点择优算法 + 简报邮件渲染 + 交易日过滤
"""

import pytest
import pandas as pd
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from src.email_notifier import EmailNotifier


# ════════════════════════════════════════════════════════
# _pick_best_anchor 测试
# ════════════════════════════════════════════════════════

class TestPickBestAnchor:
    """锚点择优算法测试"""

    def test_picks_shortest_window_when_multiple_in_range(self):
        """多个锚点在警报范围内 → 选实际回溯最短的 (ma60=60天 < wma20≈100天)"""
        anchors = {
            "ma60": 10.0,
            "wma20": 9.8,
            "wma30": 9.5,
            "wma50": 9.2,
        }
        close = 9.3
        # ma60=-7% (在区间), wma20=-5.1% (在区间)
        # ma60 实际回溯 60 天 < wma20≈100 天 → 选 ma60
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "ma60"

    def test_picks_only_anchor_in_range(self):
        """全都在范围内 → 选实际回溯最短的 ma60"""
        anchors = {"ma60": 10.0, "wma20": 10.1, "wma30": 10.2, "wma50": 9.9}
        close = 9.3
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "ma60"

    def test_no_anchor_in_range_returns_none(self):
        """所有锚点偏离都不在警报范围内 → None"""
        anchors = {"ma60": 10.0, "wma20": 10.01}
        close = 10.03  # ma60=+0.3%([0,5)跳过), wma20=+0.2%([0,5)跳过)
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is None

    def test_same_window_picks_smaller_deviation(self):
        """所有锚点都在范围内 → 实际回溯最短的 ma60 优先"""
        anchors = {"ma60": 10.0, "wma20": 9.5, "wma30": 9.2, "wma50": 8.8}
        close = 11.0
        # ma60=+10%[10,15), wma20=+15.8%(>=15), wma30=+19.6%(>=15)
        # ma60 实际回溯 60 天 < wma20≈100 天 → 选 ma60
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "ma60"

    def test_handles_none_anchors(self):
        """某些锚点为 None → 跳过"""
        anchors = {"ma60": 10.0, "wma20": None, "wma30": None, "wma50": None}
        close = 9.3  # ma60=-7% → 在范围内
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "ma60"

    def test_handles_nan_anchors(self):
        """NaN 锚点值 → 跳过，其他锚点中 ma60 回溯最短"""
        anchors = {"ma60": float("nan"), "wma20": 9.5, "wma30": None, "wma50": 9.0}
        close = 9.3
        # wma20=(9.3-9.5)/9.5*100=-2.11% → (-5,0) 在范围内
        # wma50=(9.3-9.0)/9.0*100=+3.33% → [0,5) 被跳过
        # ma60=NaN 跳过, wma30=None 跳过
        # 唯一候选: wma20
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "wma20"

    def test_handles_zero_or_negative_anchor(self):
        """锚点值 ≤0 → 跳过"""
        anchors = {"ma60": 0.0, "wma20": -1.0, "wma30": 10.0, "wma50": 9.0}
        close = 9.3
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None

    def test_positive_deviation_in_upper_ranges(self):
        """偏离率在正区间内 → ma60 实际回溯最短"""
        anchors = {"ma60": 10.0, "wma20": 9.0, "wma30": 8.5}
        close = 10.5  # ma60=+5%([5,10)), wma20=+16.7%(>=15), wma30=+23.5%(>=15)
        # ma60 实际回溯 60 天 < wma20≈100 天
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "ma60"
        assert result[2] > 0

    def test_boundary_below_negative_10(self):
        """≤ -10% 区间：ma60 和 wma20 都在范围内，ma60 回溯更短"""
        anchors = {"ma60": 10.0, "wma20": 9.0}
        close = 8.9  # ma60=-11%(<=-10), wma20=-1.11%(-5,0)
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "ma60"

    def test_boundary_exactly_zero_excluded(self):
        """偏离率 +11.11% 在 [10,15) 区间内，ma60 回溯最短"""
        anchors = {"ma60": 10.0, "wma20": 9.0}
        close = 10.0  # ma60=0%(不在区间), wma20=+11.11%([10,15)在区间!)
        # wma20 是唯一在区间内的锚点
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert result[0] == "wma20"
        assert 10.0 <= result[2] < 15.0

    def test_zero_deviation_across_all_anchors_excluded(self):
        """所有锚点偏离率都在 [0,5) 跳过区间 → None"""
        anchors = {"ma60": 10.0, "wma20": 9.95}
        close = 10.01  # ma60=+0.1%, wma20=+0.6% → 都在[0,5)跳过区间
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is None

    def test_all_anchors_invalid_returns_none(self):
        """所有锚点都无效 → None"""
        result = EmailNotifier._pick_best_anchor(9.3, {})
        assert result is None

    def test_deviation_with_decimal_precision(self):
        """偏离率保留两位小数"""
        anchors = {"ma60": 10.0}
        close = 9.423
        result = EmailNotifier._pick_best_anchor(close, anchors)
        assert result is not None
        assert len(str(result[2]).split(".")[-1]) <= 2  # ≤2 位小数


# ════════════════════════════════════════════════════════
# send_brief_report 测试
# ════════════════════════════════════════════════════════

class TestSendBriefReport:
    """简报邮件测试"""

    @pytest.fixture
    def mock_session(self):
        """构建模拟 Session"""
        session = Mock()
        session.get_all_dataframe.return_value = pd.DataFrame([
            {
                "stock_code": "601728",
                "stock_name": "中国电信",
                "date": datetime(2026, 4, 27),
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
                "date": datetime(2026, 4, 26),  # 昨天 → 美股休市
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
                "date": datetime(2026, 4, 27),
                "open": 18.50,
                "close": 18.62,
                "ma60": 18.00,
                "wma20": 18.40,
                "wma30": 17.80,
                "wma50": 17.50,
            },
        ])
        return session

    def test_trading_day_filter_excludes_old_data(self, mock_session):
        """超过3天的旧数据被过滤"""
        # 把 VOO 日期改成 10 天前
        df = mock_session.get_all_dataframe.return_value
        df.at[1, "date"] = datetime(2026, 4, 17)

        notifier = EmailNotifier({"email": {}})
        with patch.object(notifier, "_send_email") as mock_send:
            notifier.send_brief_report(
                mock_session,
                {"id": "test", "label": "测试简报"},
            )
            body = mock_send.call_args[0][1]
            assert "601728" in body
            assert "00883" in body
            assert "VOO" not in body  # 10天前数据被过滤

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

    def test_anchor_displayed_when_in_range(self, mock_session):
        """锚点在警报范围内时正确显示"""
        notifier = EmailNotifier({"email": {}})
        with patch.object(notifier, "_send_email") as mock_send:
            notifier.send_brief_report(
                mock_session,
                {"id": "test", "label": "测试简报"},
            )
            body = mock_send.call_args[0][1]
            # 00883: close=18.62, wma20=18.40 → dev=+1.20% (不在区间)
            # ma60=18.0 → dev=+3.44% (也不在区间, [0,5)被跳过)
            # wma30=17.80 → dev=+4.61% (也不在区间)
            # wma50=17.50 → dev=+6.4% [5,10) ← 在区间! 窗口50
            # 601728: close=5.76, wma20=5.78 → -0.35% (-5,0)←在区间! 窗口20
            # 所以两行都应有锚点数据
            assert "601728" in body
            assert "wma20" in body or "ma60" in body  # 至少有一个锚点

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
        session.get_all_dataframe.return_value = pd.DataFrame()
        notifier = EmailNotifier({"email": {}})
        with patch.object(notifier, "_send_email") as mock_send:
            notifier.send_brief_report(
                session, {"id": "test", "label": "空测试"}
            )
            assert mock_send.called


# ════════════════════════════════════════════════════════
# 集成: run_brief_report 测试
# ════════════════════════════════════════════════════════

class TestRunBriefReportIntegration:
    """run_brief_report 集成测试"""

    def test_weekend_skip(self):
        """周末跳过整个简报"""
        from main import run_brief_report
        with patch("main.load_config") as mock_config:
            mock_config.return_value = {
                "scheduler": {
                    "brief_reports": [
                        {"id": "morning_snapshot", "label": "早盘简报"}
                    ]
                },
                "stocks": ["601728"],
            }
            with patch("main.datetime") as mock_dt:
                # 周六
                mock_dt.now.return_value = datetime(2026, 4, 25, 9, 50)
                mock_dt.date.return_value = datetime(2026, 4, 25).date()
                with patch("main.StockDataFetcher") as mock_fetcher:
                    run_brief_report("morning_snapshot")
                    # 周末跳过 → 不应该尝试 fetch
                    mock_fetcher.assert_not_called()
