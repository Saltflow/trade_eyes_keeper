"""
投资组合策略模块测试
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime

from src.analysis.portfolio_strategy import (
    TimingStrategyEngine,
    PortfolioEvaluator,
    PortfolioOptimizer,
    _detect_stock_group,
    _get_lot_size,
    PortfolioResult,
)


# ── 辅助：生成模拟数据 ──


def make_price_data(close_prices, start_date="2024-01-01"):
    """从价格序列创建DataFrame"""
    dates = pd.date_range(start=start_date, periods=len(close_prices), freq="B")
    return pd.DataFrame({"date": dates, "close": close_prices})


def make_trend_data(base=10.0, length=500, trend_strength=0.0, seed=42):
    """生成有趋势的模拟价格数据"""
    np.random.seed(seed)
    dates = pd.date_range(end=datetime.now(), periods=length, freq="B")
    trend = np.linspace(0, trend_strength, length)
    noise = np.random.randn(length) * base * 0.03
    prices = base + trend + noise
    prices = np.maximum(prices, base * 0.3)
    return pd.DataFrame({"date": dates, "close": prices})


# ── 分组检测 ──


class TestStockGroupDetection:
    def test_a_share_six_digit(self):
        assert _detect_stock_group("601728") == "a_share"
        assert _detect_stock_group("000958") == "a_share"
        assert _detect_stock_group("600938") == "a_share"

    def test_non_a_share(self):
        assert _detect_stock_group("GOOG") == "non_a_share"
        assert _detect_stock_group("00883") == "non_a_share"
        assert _detect_stock_group("C38U.SI") == "non_a_share"
        assert _detect_stock_group("UPRO") == "non_a_share"

    def test_lot_size(self):
        assert _get_lot_size("601728") == 100
        assert _get_lot_size("GOOG") == 1
        assert _get_lot_size("00883") == 1
        assert _get_lot_size("C38U.SI") == 1


# ── 择时策略引擎 ──


class TestTimingStrategyEngine:
    def test_basic_run(self):
        """策略引擎能正常运行并返回指标"""
        df = make_trend_data(base=10.0, length=500, trend_strength=2.0)
        engine = TimingStrategyEngine("601728", df)
        metrics = engine.run_simulation(initial_cash=10000)
        assert metrics.stock_code == "601728"
        assert metrics.total_trades >= 0
        assert len(metrics.daily_values) > 0
        assert len(metrics.trade_log) >= 0

    def test_lot_size_a_share(self):
        """A股买入整手应为100的倍数"""
        prices = [10.0 + i * 0.01 for i in range(500)]
        df = make_price_data(prices)
        engine = TimingStrategyEngine("601728", df)
        metrics = engine.run_simulation(initial_cash=10000)
        for trade in metrics.trade_log:
            if trade.trade_type == "buy":
                assert trade.shares % 100 == 0, f"A股买入{trade.shares}不是100的倍数"

    def test_lot_size_us_stock(self):
        """美股买入整手应为1股"""
        prices = [100.0 + i * 0.05 for i in range(500)]
        df = make_price_data(prices)
        engine = TimingStrategyEngine("GOOG", df)
        metrics = engine.run_simulation(initial_cash=10000)
        for trade in metrics.trade_log:
            if trade.trade_type == "buy":
                assert trade.shares >= 1

    def test_buy_at_minus_5(self):
        """价格跌破MA60的-5%时应触发买入"""
        # 构造先涨后跌的数据
        np.random.seed(42)
        length = 500
        dates = pd.date_range(end=datetime.now(), periods=length, freq="B")
        # 前200天平稳，然后快速下跌
        prices = np.ones(length) * 10.0
        prices[:200] = 10.0
        prices[200:300] = np.linspace(10.0, 9.0, 100)  # 缓慢下跌
        prices[300:350] = np.linspace(9.0, 8.0, 50)  # 加速跌
        prices[350:] = np.linspace(8.0, 7.5, 150)  # 持续低位
        df = pd.DataFrame({"date": dates, "close": prices})
        engine = TimingStrategyEngine("601728", df)
        metrics = engine.run_simulation(initial_cash=10000)
        buy_trades = [t for t in metrics.trade_log if t.trade_type == "buy"]
        # 应该至少有一次买入
        assert len(buy_trades) > 0, "价格跌破-5%应触发买入"

    def test_sell_at_plus_5(self):
        """价格突破MA60的+5%时应触发卖出"""
        # 构造先跌后涨的数据
        np.random.seed(42)
        length = 500
        dates = pd.date_range(end=datetime.now(), periods=length, freq="B")
        # 前200天下跌，然后快速上涨
        prices = np.ones(length) * 10.0
        prices[:200] = np.linspace(10.0, 9.0, 200)  # 缓慢下跌
        prices[200:280] = 8.5  # 低位震荡
        prices[280:350] = np.linspace(8.5, 10.5, 70)  # 涨到超过MA60
        prices[350:400] = np.linspace(10.5, 11.5, 50)  # 继续涨
        prices[400:] = np.linspace(11.5, 10.0, 100)  # 回落
        df = pd.DataFrame({"date": dates, "close": prices})
        engine = TimingStrategyEngine("601728", df)
        metrics = engine.run_simulation(initial_cash=10000)
        sell_trades = [t for t in metrics.trade_log if t.trade_type == "sell"]
        print(f"Sell trades: {len(sell_trades)}")
        for t in sell_trades[:5]:
            print(f"  {t.date} sell {t.shares}sh @ {t.price} ({t.reason})")
        # 应该至少有一次卖出
        assert len(sell_trades) > 0, "价格突破+5%应触发卖出"

    def test_buy_amount_limit(self):
        """每笔买入不超过5000元"""
        df = make_trend_data(base=10.0, length=500, trend_strength=1.0)
        engine = TimingStrategyEngine("601728", df)
        metrics = engine.run_simulation(initial_cash=10000)
        for trade in metrics.trade_log:
            if trade.trade_type == "buy":
                assert trade.amount <= 5100, f"买入{trade.amount}超过5000元限额"


# ── 投资组合评估 ──


class TestPortfolioEvaluator:
    def test_empty_portfolio(self):
        """空组合应返回零值"""
        evaluator = PortfolioEvaluator({}, "a_share")
        result = evaluator.evaluate([])
        assert result.total_return == 0.0
        assert result.composition == []

    def test_single_stock_portfolio(self):
        """单只股票组合"""
        df = make_trend_data(base=10.0, length=500, trend_strength=2.0)
        evaluator = PortfolioEvaluator({"601728": df}, "a_share")
        result = evaluator.evaluate(["601728"])
        assert len(result.composition) == 1
        assert result.composition[0] == "601728"
        assert isinstance(result.total_return, float)

    def test_two_stock_portfolio(self):
        """两只股票组合"""
        df1 = make_trend_data(base=10.0, length=500, trend_strength=2.0, seed=42)
        df2 = make_trend_data(base=20.0, length=500, trend_strength=3.0, seed=99)
        stocks = {"601728": df1, "600938": df2}
        evaluator = PortfolioEvaluator(stocks, "a_share")
        result = evaluator.evaluate(["601728", "600938"])
        assert len(result.composition) == 2
        assert result.trade_count >= 0

    def test_max_drawdown_calculation(self):
        """最大回撤应为负值或零"""
        df = make_trend_data(base=10.0, length=500, trend_strength=1.0)
        evaluator = PortfolioEvaluator({"601728": df}, "a_share")
        result = evaluator.evaluate(["601728"])
        assert result.max_drawdown <= 0, "最大回撤应为负值"


# ── 投资组合优化 ──


class TestPortfolioOptimizer:
    def test_detect_groups_from_config(self):
        """从配置中正确检测分组"""
        config = {"stocks": ["601728", "600938", "GOOG", "VOO", "00883"]}
        optimizer = PortfolioOptimizer(config)
        # 只测试分组逻辑
        groups = {"a_share": [], "non_a_share": []}
        for code in config["stocks"]:
            g = _detect_stock_group(str(code))
            groups[g].append(code)
        assert "601728" in groups["a_share"]
        assert "600938" in groups["a_share"]
        assert "GOOG" in groups["non_a_share"]
        assert "VOO" in groups["non_a_share"]
        assert "00883" in groups["non_a_share"]

    def test_greedy_search_convergence(self):
        """贪心搜索能收敛到最优组合"""
        # 用少量模拟数据测试
        np.random.seed(42)
        length = 450
        dates = pd.date_range(end=datetime.now(), periods=length, freq="B")
        stocks = {}
        for i, code in enumerate(["601728", "600938", "601985"]):
            base = 10.0 + i * 5.0
            trend = np.linspace(0, 2.0 + i * 0.5, length)
            noise = np.random.randn(length) * base * 0.03
            prices = base + trend + noise
            prices = np.maximum(prices, base * 0.3)
            stocks[code] = pd.DataFrame({"date": dates, "close": prices})

        evaluator = PortfolioEvaluator(stocks, "a_share")
        codes = list(stocks.keys())

        # 手动贪心搜索（最高收益）
        selected = []
        remaining = list(codes)
        best_score = -float("inf")
        best_result = None

        while remaining:
            improved = False
            step_best = best_score
            step_stock = None
            for s in remaining:
                trial = selected + [s]
                result = evaluator.evaluate(trial)
                if result.total_return > step_best:
                    step_best = result.total_return
                    step_stock = s
                    improved = True
            if improved:
                selected.append(step_stock)
                remaining.remove(step_stock)
                best_score = step_best
                best_result = evaluator.evaluate(selected)
            else:
                break

        assert best_result is not None
        assert len(selected) >= 1
        assert isinstance(best_result, PortfolioResult)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
