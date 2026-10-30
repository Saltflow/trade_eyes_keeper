"""TDD: 参考持仓 (RefPortfolio) 全链路测试。

测试覆盖：
- 数据模型序列化/反序列化
- 持久化加载/保存
- 重置（reset）行为
- 调仓合同见 test_reference_trade_plan.py
- Nav 计算
- 状态摘要
- 交易日计数
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")


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
            inception_date="2026-07-14",
            cash=90000.0,
            initial_capital=100000.0,
            trading_days=2,
            last_rebalance_date="2026-07-15",
        )
        pf.holdings["601728"] = Holding(code="601728", shares=500, avg_cost=12.34)
        pf.trade_log.append(
            Trade(
                date="2026-07-14",
                code="601728",
                action="buy",
                shares=500,
                price=12.34,
                cost=6170.0,
                reason="buy_minus5",
            )
        )

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
            RefPortfolioManager,
            RefPortfolio,
            Holding,
            Trade,
        )

        mgr = RefPortfolioManager(self._path)
        pf = RefPortfolio(
            inception_date="2026-07-14",
            cash=80000.0,
            initial_capital=100000.0,
            trading_days=5,
        )
        pf.holdings["601728"] = Holding(code="601728", shares=200, avg_cost=10.5)
        pf.trade_log.append(
            Trade(
                date="2026-07-14",
                code="601728",
                action="buy",
                shares=200,
                price=10.5,
                cost=2100.0,
                reason="buy_minus5",
            )
        )
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
            RefPortfolioManager,
            RefPortfolio,
            Holding,
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


class TestGetStatus:
    """get_status() 返回字段完整性。"""

    def test_status_has_required_fields(self):
        from src.core.ref_portfolio import (
            RefPortfolioManager,
            RefPortfolio,
            Holding,
        )

        mgr = RefPortfolioManager()
        pf = RefPortfolio(
            inception_date="2026-07-14",
            cash=80000.0,
            initial_capital=100000.0,
            trading_days=3,
            last_rebalance_date="2026-07-16",
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
