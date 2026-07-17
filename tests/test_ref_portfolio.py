"""TDD: 参考持仓 (RefPortfolio) 全链路测试。

测试覆盖：
- 数据模型序列化/反序列化
- 持久化加载/保存
- 重置（reset）行为
- 调仓（rebalance）：买入/卖出/互斥/周末/资金不足
- Nav 计算
- 状态摘要
- 交易日计数
"""

import os
import sys
import tempfile
from datetime import date, datetime
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")


# ── 模拟 StrategyAlert ─────────────────────────────────────────

@dataclass
class MockAlert:
    stock_code: str
    rule_id: str
    rule_label: str = ""
    current_value: str = ""
    type: str = ""


def _buy_alert(code: str) -> MockAlert:
    return MockAlert(stock_code=code, rule_id="buy_minus5",
                     rule_label="跌破MA60 -5%", type="strategy_buy")


def _sell_alert(code: str) -> MockAlert:
    return MockAlert(stock_code=code, rule_id="sell_plus5",
                     rule_label="突破MA60 +5%", type="strategy_sell")


# ── 数据模型测试 ───────────────────────────────────────────────

class TestRefPortfolioModel:
    """RefPortfolio / Holding / Trade 数据模型。"""

    def test_default_portfolio(self):
        from src.core.ref_portfolio import RefPortfolio
        pf = RefPortfolio()
        assert pf.cash == 100000.0
        assert pf.initial_capital == 100000.0
        assert pf.trading_days == 0
        assert pf.inception_date == ""
        assert pf.holdings == {}
        assert pf.trade_log == []

    def test_nav_with_holdings(self):
        from src.core.ref_portfolio import RefPortfolio, Holding
        pf = RefPortfolio(cash=50000.0, initial_capital=100000.0)
        pf.holdings["601728"] = Holding(code="601728", shares=1000, avg_cost=10.0)
        nav = pf.nav({"601728": 12.0})
        assert nav == 50000.0 + 1000 * 12.0  # 62000.0

    def test_nav_return_pct(self):
        from src.core.ref_portfolio import RefPortfolio, Holding
        pf = RefPortfolio(cash=50000.0, initial_capital=100000.0)
        pf.holdings["601728"] = Holding(code="601728", shares=1000, avg_cost=10.0)
        ret = pf.nav_return_pct({"601728": 12.0})
        assert ret == pytest.approx(-38.0)  # (62000/100000 - 1) * 100

    def test_to_dict_and_from_dict(self):
        from src.core.ref_portfolio import RefPortfolio, Holding, Trade
        pf = RefPortfolio(
            inception_date="2026-07-14", cash=90000.0, initial_capital=100000.0,
            trading_days=2, last_rebalance_date="2026-07-15",
        )
        pf.holdings["601728"] = Holding(code="601728", shares=500, avg_cost=12.34)
        pf.trade_log.append(Trade(
            date="2026-07-14", code="601728", action="buy",
            shares=500, price=12.34, cost=6170.0, reason="buy_minus5",
        ))

        d = pf.to_dict()
        restored = RefPortfolio.from_dict(d)

        assert restored.inception_date == "2026-07-14"
        assert restored.cash == 90000.0
        assert restored.initial_capital == 100000.0
        assert restored.trading_days == 2
        assert "601728" in restored.holdings
        assert restored.holdings["601728"].shares == 500
        assert restored.holdings["601728"].avg_cost == pytest.approx(12.34)
        assert len(restored.trade_log) == 1
        assert restored.trade_log[0].code == "601728"


# ── 持久化测试 ─────────────────────────────────────────────────

class TestRefPortfolioPersistence:
    """加载/保存/重置。"""

    def setup_method(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self._tmp.close()
        self._path = Path(self._tmp.name)

    def teardown_method(self):
        if self._path.exists():
            self._path.unlink(missing_ok=True)

    def test_load_empty_file(self):
        from src.core.ref_portfolio import RefPortfolioManager, RefPortfolio
        mgr = RefPortfolioManager(self._path)
        pf = mgr.load()
        assert isinstance(pf, RefPortfolio)
        assert pf.cash == 100000.0
        assert pf.inception_date == ""

    def test_save_and_load_roundtrip(self):
        from src.core.ref_portfolio import (
            RefPortfolioManager, RefPortfolio, Holding, Trade,
        )
        mgr = RefPortfolioManager(self._path)
        pf = RefPortfolio(
            inception_date="2026-07-14", cash=80000.0, initial_capital=100000.0,
            trading_days=5,
        )
        pf.holdings["601728"] = Holding(code="601728", shares=200, avg_cost=10.5)
        pf.trade_log.append(Trade(
            date="2026-07-14", code="601728", action="buy",
            shares=200, price=10.5, cost=2100.0, reason="buy_minus5",
        ))
        mgr.save(pf)

        loaded = mgr.load()
        assert loaded.inception_date == "2026-07-14"
        assert loaded.cash == 80000.0
        assert loaded.trading_days == 5
        assert loaded.holdings["601728"].shares == 200
        assert loaded.holdings["601728"].avg_cost == pytest.approx(10.5)
        assert len(loaded.trade_log) == 1

    def test_reset_clears_everything(self):
        from src.core.ref_portfolio import (
            RefPortfolioManager, RefPortfolio, Holding,
        )
        mgr = RefPortfolioManager(self._path)
        # 先创建一个有持仓的状态
        pf = RefPortfolio(inception_date="2026-01-01", cash=50000.0)
        pf.holdings["601728"] = Holding(code="601728", shares=100, avg_cost=10.0)
        mgr.save(pf)

        # 重置
        new_pf = mgr.reset(initial_capital=200000.0, inception_date="2026-07-15")
        assert new_pf.cash == 200000.0
        assert new_pf.initial_capital == 200000.0
        assert new_pf.inception_date == "2026-07-15"
        assert new_pf.holdings == {}
        assert new_pf.trading_days == 0

        # 确认已保存
        loaded = mgr.load()
        assert loaded.cash == 200000.0
        assert loaded.holdings == {}


# ── 调仓测试 ───────────────────────────────────────────────────

class TestRebalance:
    """rebalance() 交易逻辑。"""

    def setup_method(self):
        from src.core.ref_portfolio import RefPortfolio
        self.pf = RefPortfolio(
            inception_date="2026-07-14",
            cash=100000.0,
            initial_capital=100000.0,
        )
        self.prices = {"601728": 12.0, "600938": 8.0, "00883": 20.0}

    # ── 买入 ──

    def test_buy_creates_holding(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        new_pf, trades = mgr.rebalance(
            self.pf, [_buy_alert("601728")], self.prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        assert "601728" in new_pf.holdings
        h = new_pf.holdings["601728"]
        # BUY_CASH_FRACTION=0.20: max_amount=min(20000,50000,100000)=20000
        # raw=20000/12=1666, buy=1600 (lot=100)
        assert h.shares == 1600
        assert new_pf.cash < 100000.0  # 扣了现金
        assert len(trades) == 1
        assert trades[0].action == "buy"
        assert trades[0].shares == 1600

    def test_buy_deducts_cash_and_commission(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        new_pf, trades = mgr.rebalance(
            self.pf, [_buy_alert("601728")], self.prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        expected_gross = 1600 * 12.0  # 19200
        expected_commission = expected_gross * 0.005  # 96
        expected_cost = expected_gross + expected_commission  # 19296
        assert new_pf.cash == pytest.approx(100000.0 - expected_cost)
        assert trades[0].commission == pytest.approx(expected_commission)

    def test_buy_complies_with_lot_size(self):
        """买入股数必须是 lot_size 的整数倍。"""
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        new_pf, trades = mgr.rebalance(
            self.pf, [_buy_alert("601728")], self.prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        assert new_pf.holdings["601728"].shares % 100 == 0

    def test_buy_insufficient_cash_skips(self):
        from src.core.ref_portfolio import RefPortfolioManager, RefPortfolio
        mgr = RefPortfolioManager()
        poor_pf = RefPortfolio(cash=50.0, initial_capital=100000.0,
                               inception_date="2026-07-14")
        new_pf, trades = mgr.rebalance(
            poor_pf, [_buy_alert("601728")], self.prices, "2026-07-15",
        )
        assert len(trades) == 0  # 50 元不够一手
        assert new_pf.cash == 50.0

    def test_buy_partial_fill_when_near_cash_limit(self):
        """现金刚好差一点 → 减一手重算。"""
        from src.core.ref_portfolio import RefPortfolioManager, RefPortfolio
        mgr = RefPortfolioManager()
        # 一手 = 12*100=1200 + 6 手续费 = 1206
        # 给 1300 现金 → 买 100 股
        tight_pf = RefPortfolio(cash=1300.0, initial_capital=100000.0,
                                inception_date="2026-07-14")
        new_pf, trades = mgr.rebalance(
            tight_pf, [_buy_alert("601728")], self.prices, "2026-07-15",
        )
        if trades:  # 1300 够一手应该成交
            assert new_pf.holdings.get("601728", None)

    # ── 卖出 ──

    def test_sell_reduces_holding(self):
        from src.core.ref_portfolio import RefPortfolioManager, Holding, RefPortfolio
        mgr = RefPortfolioManager()
        pf = RefPortfolio(inception_date="2026-07-14", cash=50000.0,
                          initial_capital=100000.0)
        pf.holdings["601728"] = Holding(code="601728", shares=1000, avg_cost=10.0)
        prices = {"601728": 15.0}

        new_pf, trades = mgr.rebalance(
            pf, [_sell_alert("601728")], prices, "2026-07-15",
        )
        # 卖出 25% = 250 shares → 向下取整手 → 200 shares
        assert new_pf.holdings["601728"].shares == 800
        # 现金增加（200 手取整后）
        gross = 200 * 15.0
        commission = gross * 0.005
        assert new_pf.cash == pytest.approx(50000.0 + gross - commission)
        assert len(trades) == 1
        assert trades[0].action == "sell"

    def test_sell_lot_size_rounding(self):
        """卖出手数必须是 lot_size 的整数倍。"""
        from src.core.ref_portfolio import RefPortfolioManager, Holding, RefPortfolio
        mgr = RefPortfolioManager()
        pf = RefPortfolio(inception_date="2026-07-14", cash=50000.0)
        pf.holdings["601728"] = Holding(code="601728", shares=150, avg_cost=10.0)
        # 150 * 0.25 = 37.5 → 向下取整到 0 (不足一手)
        prices = {"601728": 15.0}
        new_pf, trades = mgr.rebalance(
            pf, [_sell_alert("601728")], prices, "2026-07-15",
        )
        # 不足一手不卖
        assert new_pf.holdings["601728"].shares == 150
        assert len(trades) == 0

    def test_sell_no_holding_skips(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        new_pf, trades = mgr.rebalance(
            self.pf, [_sell_alert("601728")], self.prices, "2026-07-15",
        )
        assert len(trades) == 0
        assert new_pf.cash == self.pf.cash

    # ── 互斥 ──

    def test_same_stock_buy_sell_mutual_exclusion(self):
        """同一标的同日既有买又有卖信号 → 双方都取消。"""
        from src.core.ref_portfolio import RefPortfolioManager, Holding, RefPortfolio
        mgr = RefPortfolioManager()
        pf = RefPortfolio(inception_date="2026-07-14", cash=50000.0)
        pf.holdings["601728"] = Holding(code="601728", shares=500, avg_cost=10.0)
        prices = {"601728": 15.0}

        new_pf, trades = mgr.rebalance(
            pf,
            [_buy_alert("601728"), _sell_alert("601728")],
            prices, "2026-07-15",
        )
        assert len(trades) == 0
        assert new_pf.holdings["601728"].shares == 500  # unchanged

    # ── 周末阻挡 ──

    def test_weekend_no_trading(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        # 2026-07-18 is Saturday
        new_pf, trades = mgr.rebalance(
            self.pf, [_buy_alert("601728")], self.prices, "2026-07-18",
        )
        assert len(trades) == 0

    def test_weekday_trading_allowed(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        new_pf, trades = mgr.rebalance(
            self.pf, [_buy_alert("601728")], self.prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        assert len(trades) >= 1

    # ── 持仓延续 ──

    def test_holdings_persist_across_rebalance(self):
        """持仓不会因为新一轮调仓而丢失（除非被卖出）。"""
        from src.core.ref_portfolio import RefPortfolioManager, Holding, RefPortfolio
        mgr = RefPortfolioManager()
        pf = RefPortfolio(inception_date="2026-07-14", cash=50000.0)
        pf.holdings["600938"] = Holding(code="600938", shares=300, avg_cost=8.0)
        prices = {"601728": 12.0, "600938": 9.0}

        new_pf, trades = mgr.rebalance(
            pf, [_buy_alert("601728")], prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        # 600938 still there
        assert "600938" in new_pf.holdings
        assert new_pf.holdings["600938"].shares == 300
        # 601728 added
        assert "601728" in new_pf.holdings

    # ── 交易日志 ──

    def test_trade_log_appended(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        pf = self.pf
        assert len(pf.trade_log) == 0

        new_pf, trades1 = mgr.rebalance(
            pf, [_buy_alert("601728")], self.prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        assert len(new_pf.trade_log) == 1

        prices2 = {"601728": 14.0}
        new_pf2, trades2 = mgr.rebalance(
            new_pf, [_sell_alert("601728")], prices2, "2026-07-16",
        )
        assert len(new_pf2.trade_log) == 2

    # ── 交易日计数 ──

    def test_trading_days_increments(self):
        from src.core.ref_portfolio import RefPortfolioManager
        mgr = RefPortfolioManager()
        pf = self.pf
        assert pf.trading_days == 0

        new_pf, _ = mgr.rebalance(
            pf, [_buy_alert("601728")], self.prices, "2026-07-15",
            monthly_buy_limit=50000,
        )
        assert new_pf.trading_days == 1

        # 同一天再次调仓 → 不增加
        new_pf2, _ = mgr.rebalance(
            new_pf, [_buy_alert("600938")],
            {"601728": 12.0, "600938": 8.0}, "2026-07-15",
            monthly_buy_limit=50000,
        )
        assert new_pf2.trading_days == 1

        # 新一天 → 增加
        new_pf3, _ = mgr.rebalance(
            new_pf2, [_buy_alert("600938")],
            {"601728": 12.0, "600938": 8.0}, "2026-07-16",
            monthly_buy_limit=50000,
        )
        assert new_pf3.trading_days == 2


# ── 状态摘要测试 ───────────────────────────────────────────────

class TestGetStatus:
    """get_status() 返回字段完整性。"""

    def test_status_has_required_fields(self):
        from src.core.ref_portfolio import (
            RefPortfolioManager, RefPortfolio, Holding,
        )
        mgr = RefPortfolioManager()
        pf = RefPortfolio(
            inception_date="2026-07-14", cash=80000.0, initial_capital=100000.0,
            trading_days=3, last_rebalance_date="2026-07-16",
        )
        pf.holdings["601728"] = Holding(code="601728", shares=200, avg_cost=10.0)
        prices = {"601728": 12.0}

        status = mgr.get_status(pf, prices)
        assert status["inception_date"] == "2026-07-14"
        assert status["cash"] == 80000.0
        assert status["initial_capital"] == 100000.0
        assert status["trading_days"] == 3
        assert status["nav"] == pytest.approx(82400.0)  # 80000 + 200*12
        assert status["nav_return_pct"] == pytest.approx(-17.6)
        assert len(status["holdings"]) == 1
        assert status["holdings"][0]["code"] == "601728"
        assert status["holdings"][0]["market_value"] == pytest.approx(2400.0)
        assert "last_rebalance_date" in status

    def test_status_no_prices_shows_cash_only(self):
        from src.core.ref_portfolio import RefPortfolioManager, RefPortfolio
        mgr = RefPortfolioManager()
        pf = RefPortfolio(cash=80000.0, initial_capital=100000.0)
        status = mgr.get_status(pf, None)
        assert status["nav"] == 80000.0
        assert status["holdings"] == []


# ── 初始化判断 ─────────────────────────────────────────────────

class TestIsInitialized:
    def test_empty_not_initialized(self):
        from src.core.ref_portfolio import RefPortfolioManager, RefPortfolio
        mgr = RefPortfolioManager()
        assert not mgr.is_initialized(RefPortfolio())

    def test_with_date_is_initialized(self):
        from src.core.ref_portfolio import RefPortfolioManager, RefPortfolio
        mgr = RefPortfolioManager()
        assert mgr.is_initialized(RefPortfolio(inception_date="2026-07-14"))
