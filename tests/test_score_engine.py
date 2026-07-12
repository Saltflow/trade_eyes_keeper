"""评分引擎决策仿真语义测试 (_score_sim_core / simulate_portfolio)。

锁定主链路量化执行语义（历史需求：commit b01233f 3日确认+均价执行，
以及分位引擎接入后的同日互斥/月额度/回补规则）：

1. 买入执行价 = 近3日收盘均价（含当日；不足3日用现有天数）
2. 卖出执行价 = 单日收盘价（触发日）
3. 同日既触发买又触发卖 → 双向跳过（同日互斥）
4. 月度买入额度默认不限制 (inf)
5. 卖出后允许回补（无 shares==0 永久壁垒）
6. 手数取整 / 手续费 / 现金约束
7. 分位归一评分 → 阈值决策
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")

from analysis.signal_functions import simulate_portfolio  # noqa: E402

INF = float("inf")


def _sim(buy, sell, price, *, cash=100000.0, buy_th=0.5, sell_th=0.5,
         frac=1.0, lot=1, monthly=INF, comm=0.0):
    """便捷封装：单/多标的评分矩阵 → PortfolioTrace。"""
    buy = np.asarray(buy, dtype=np.float64)
    sell = np.asarray(sell, dtype=np.float64)
    price = np.asarray(price, dtype=np.float64)
    if buy.ndim == 1:
        buy = buy.reshape(-1, 1)
        sell = sell.reshape(-1, 1)
        price = price.reshape(-1, 1)
    T, N = buy.shape
    dates = [f"d{i}" for i in range(T)]
    codes = [f"S{i}" for i in range(N)]
    return simulate_portfolio(
        buy, sell, price, cash, buy_th, sell_th, frac, lot, monthly, comm,
        dates, codes,
    )


class TestBuyExecutionPrice3DayAverage:
    """需求1：买入执行价 = 近3日收盘均价。"""

    def test_buy_uses_3day_close_average(self):
        # close = [10, 20, 30], 第3天(t=2)触发买入
        # 均价 = (10+20+30)/3 = 20，全仓 100000/20 = 5000 股
        tr = _sim([0, 0, 1], [0, 0, 0], [10.0, 20.0, 30.0])
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 5000  # 100000/20，不是 /30(=3333)

    def test_buy_window_shorter_when_insufficient_history(self):
        # 第1天(t=0)触发，只有1天历史 → 均价=10 → 10000 股
        tr = _sim([1, 0, 0], [0, 0, 0], [10.0, 20.0, 30.0], cash=100000.0)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 10000  # 100000/10

    def test_buy_2day_window(self):
        # 第2天(t=1)触发，2天历史 → 均价=(10+20)/2=15 → 100000/15=6666.67→6666股
        tr = _sim([0, 1, 0], [0, 0, 0], [10.0, 20.0, 30.0], lot=1)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == int(100000 / 15)


class TestSellExecutionPriceSingleDay:
    """需求2：卖出执行价 = 单日收盘价（触发日），不平滑。"""

    def test_sell_uses_trigger_day_close(self):
        # t=0 买入(价10), t=4 卖出(价50). 卖出按当日50, 不是3日均价
        buy = [1, 0, 0, 0, 0]
        sell = [0, 0, 0, 0, 1]
        price = [10.0, 20.0, 30.0, 40.0, 50.0]
        tr = _sim(buy, sell, price, frac=1.0, lot=1)
        # 买入 10000 股 @10; t=4 卖出 frac=1.0 → 全卖 @50 → cash≈500000
        assert tr.total_trades == 2
        assert tr.final_shares[0] == 0
        assert tr.final_cash > 490000  # 卖在50而非3日均价40


class TestSameDayMutualExclusion:
    """需求3：同日既触发买又触发卖 → 双向跳过。"""

    def test_same_day_buy_and_sell_skipped(self):
        # t=1 买卖信号同时触发 → 0 交易
        tr = _sim([0, 1, 0], [0, 1, 0], [10.0, 10.0, 10.0])
        assert tr.total_trades == 0
        assert tr.final_shares[0] == 0

    def test_buy_only_day_still_trades(self):
        # 对照：只买不卖 → 有交易
        tr = _sim([0, 1, 0], [0, 0, 0], [10.0, 10.0, 10.0])
        assert tr.total_trades == 1

    def test_sell_signal_after_holding_executes(self):
        # t=0 只买, t=2 只卖 → 两笔都成交
        tr = _sim([1, 0, 0], [0, 0, 1], [10.0, 10.0, 10.0], frac=1.0)
        assert tr.total_trades == 2
        assert tr.final_shares[0] == 0


class TestMonthlyLimitUnlimited:
    """需求4：月度买入额度默认 inf（不人为限制）。"""

    def test_large_single_buy_not_blocked(self):
        # frac=1.0, cash=100000 → 单笔买 100000 远超旧 15000 限额
        # inf 下应正常成交
        tr = _sim([1, 0], [0, 0], [10.0, 10.0], frac=1.0, monthly=INF)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 10000  # 全仓

    def test_finite_monthly_limit_caps_buy(self):
        # 显式有限额度 15000 → 单笔买入截断到 15000 → 1500 股
        tr = _sim([1, 0], [0, 0], [10.0, 10.0], frac=1.0, monthly=15000.0)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 1500  # 15000/10


class TestReentryAllowed:
    """需求5：卖出后允许回补（无 shares==0 永久壁垒）。"""

    def test_rebuy_after_sell(self):
        # t=0 买, t=2 全卖, t=4 再买 → 3 笔交易，最终持仓 > 0
        buy = [1, 0, 0, 0, 1]
        sell = [0, 0, 1, 0, 0]
        price = [10.0, 10.0, 10.0, 10.0, 10.0]
        tr = _sim(buy, sell, price, frac=1.0)
        assert tr.total_trades == 3  # 买+卖+再买
        assert tr.final_shares[0] > 0  # 回补成功


class TestLotAndCommission:
    """需求6：手数取整 + 手续费 + 现金约束。"""

    def test_lot_rounding_100(self):
        # A股 lot=100. cash=100000 price=333 → 100000/333=300.3 → 300股(3手)
        tr = _sim([1, 0], [0, 0], [333.0, 333.0], frac=1.0, lot=100)
        assert tr.final_shares[0] % 100 == 0
        assert tr.final_shares[0] == 300

    def test_commission_reduces_shares(self):
        # 有手续费时买入股数应 ≤ 无手续费
        tr_no = _sim([1, 0], [0, 0], [10.0, 10.0], frac=1.0, lot=1, comm=0.0)
        tr_fee = _sim([1, 0], [0, 0], [10.0, 10.0], frac=1.0, lot=1, comm=0.01)
        assert tr_fee.final_shares[0] <= tr_no.final_shares[0]

    def test_no_buy_when_cash_insufficient(self):
        # 现金买不起1手 → 0交易
        tr = _sim([1, 0], [0, 0], [10000.0, 10000.0], cash=5000.0,
                  frac=1.0, lot=100)
        assert tr.total_trades == 0


class TestThresholdSemantics:
    """需求7：评分需严格 > 阈值才触发。"""

    def test_score_equal_threshold_no_trade(self):
        # buy_score == threshold(0.5) → 不触发（要求严格 >）
        tr = _sim([0.5, 0.5], [0, 0], [10.0, 10.0], buy_th=0.5)
        assert tr.total_trades == 0

    def test_score_above_threshold_trades(self):
        tr = _sim([0.51, 0], [0, 0], [10.0, 10.0], buy_th=0.5)
        assert tr.total_trades == 1


class TestPortfolioTraceOutputs:
    """回归：PortfolioTrace 关键字段合理性。"""

    def test_flat_cash_when_no_signals(self):
        tr = _sim([0, 0, 0], [0, 0, 0], [10.0, 11.0, 12.0])
        assert tr.total_trades == 0
        assert tr.avg_position_pct == 0.0
        # 全程空仓，净值恒等于初始现金
        assert abs(tr.total_return_pct) < 1e-6

    def test_drawdown_not_nan_on_flat(self):
        tr = _sim([0, 0, 0], [0, 0, 0], [10.0, 10.0, 10.0])
        assert not np.isnan(tr.max_drawdown_pct)
        assert tr.max_drawdown_pct <= 0.0

    def test_multistock_independent_positions(self):
        # 2 只标的，只有标的0触发买入
        buy = np.array([[1.0, 0.0], [0.0, 0.0]])
        sell = np.zeros((2, 2))
        price = np.array([[10.0, 20.0], [10.0, 20.0]])
        tr = _sim(buy, sell, price, frac=0.5, lot=1)
        assert tr.final_shares[0] > 0
        assert tr.final_shares[1] == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
