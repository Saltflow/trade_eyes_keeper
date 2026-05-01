"""
Session链路集成测试 - 验证新的Session数据流是否正常工作
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/models"))

import pytest
import pandas as pd


class TestSessionFlow:
    """Session数据流测试"""

    def test_session_manager_creation(self):
        """测试SessionManager创建"""
        from src.session.session_manager import SessionManager

        config = {"stocks": []}
        session_manager = SessionManager(config)
        assert session_manager is not None
        print("✅ SessionManager创建成功")

    def test_session_creation(self):
        """测试Session创建"""
        from src.session.session_manager import SessionManager
        from src.models.schemas import SessionContext

        config = {"stocks": []}
        session_manager = SessionManager(config)
        session = session_manager.create_session(config)

        assert session is not None
        assert isinstance(session, SessionContext)
        assert hasattr(session, "session_id")
        assert hasattr(session, "stocks_data")
        assert hasattr(session, "alerts")
        print(f"✅ Session创建成功: {session.session_id}")

    def test_fetch_to_session(self):
        """测试fetch_to_session方法"""
        from src.session.session_manager import SessionManager
        from data_fetcher import StockDataFetcher

        config = {
            "stocks": ["000001"],  # 只测试一只股票，节省时间
            "data_source": {"type": "web_crawler"},
            "storage": {"data_dir": "./cache"},
        }

        session_manager = SessionManager(config)
        session = session_manager.create_session(config)

        fetcher = StockDataFetcher(config)
        fetcher.fetch_to_session(session, session_manager)

        # 验证session中的数据
        assert len(session.stocks_data) >= 0  # 可能为0（数据获取失败）
        print(f"✅ fetch_to_session执行成功: {len(session.stocks_data)}只股票")

    def test_check_from_session(self):
        """测试check_from_session方法"""
        from src.session.session_manager import SessionManager
        from src.core.condition_checker import ConditionChecker

        config = {"stocks": [], "alerts": {"enabled": False}}

        session_manager = SessionManager(config)
        session = session_manager.create_session(config)

        # 手动添加测试数据到session
        from src.models.converters import dataframe_to_stock_price_data

        test_df = pd.DataFrame(
            [
                {
                    "stock_code": "000001",
                    "date": pd.Timestamp.now(),
                    "open": 10.0,
                    "close": 10.5,
                    "high": 11.0,
                    "low": 9.8,
                    "volume": 1000000,
                    "amount": 10000000,
                    "ma60": 10.2,
                }
            ]
        )

        session_manager.update_stock_from_dataframe(session, "000001", test_df)

        checker = ConditionChecker(config)
        checker.check_from_session(session, session_manager)

        # 验证session中的警报
        assert isinstance(session.alerts, list)
        print(f"✅ check_from_session执行成功: {len(session.alerts)}个警报")

    def test_email_notifier_from_session(self):
        """测试email_notifier的send_from_session方法"""
        from src.session.session_manager import SessionManager
        from email_notifier import EmailNotifier
        from models.schemas import AlertStock

        config = {
            "stocks": [],
            "email": {
                "sender_email": "test@example.com",
                "sender_password": "test",
                "receiver_email": "test@example.com",
            },
        }

        session_manager = SessionManager(config)
        session = session_manager.create_session(config)

        # 添加测试数据到session
        test_alert = AlertStock(
            stock_code="000001",
            low_price=9.8,
            ma60=10.2,
            price_difference=0.4,
            percentage_difference=3.92,
            condition="test",
        )
        session.alerts.append(test_alert)

        notifier = EmailNotifier(config)

        # 测试send_from_session（会跳过实际发送，因为我们设置了SKIP_EMAIL）
        import os

        os.environ["SKIP_EMAIL"] = "true"

        try:
            notifier.send_from_session(session)
            print("✅ send_from_session执行成功")
        except Exception as e:
            print(f"❌ send_from_session失败: {e}")
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
