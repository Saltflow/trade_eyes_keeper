"""评分引擎决策仿真语义测试 (_score_sim_core / simulate_portfolio)。

锁定主链路量化执行语义（历史需求：commit b01233f 3日确认+均价执行，
以及分位引擎接入后的同日互斥/月额度/回补规则）：

1. 买入执行价 = max(high[t-1], high[t], high[t+1])，窗口末买单待执行
2. 卖出执行价 = low[t]，NAV 仍按 close[t] 估值
3. 同日既触发买又触发卖 → 双向跳过（同日互斥）
4. 月度买入额度默认不限制 (inf)
5. 卖出后允许回补（无 shares==0 永久壁垒）
6. 手数取整 / 手续费 / 现金约束
7. 分位归一评分 → 阈值决策
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")

from analysis.backtester import simulate_portfolio  # noqa: E402
from analysis.search_interface import StrategyMarketData, TradePlan  # noqa: E402

INF = float("inf")


def _sim(
    buy,
    sell,
    price,
    *,
    cash=100000.0,
    buy_th=0.5,
    sell_th=0.5,
    frac=1.0,
    lot=1,
    monthly=INF,
    comm=0.0,
    highs=None,
    lows=None,
):
    """便捷封装：单/多标的评分矩阵 → PortfolioTrace。"""
    buy = np.asarray(buy, dtype=np.float64)
    sell = np.asarray(sell, dtype=np.float64)
    price = np.asarray(price, dtype=np.float64)
    highs = np.asarray(price if highs is None else highs, dtype=np.float64)
    lows = np.asarray(price if lows is None else lows, dtype=np.float64)
    if buy.ndim == 1:
        buy = buy.reshape(-1, 1)
        sell = sell.reshape(-1, 1)
        price = price.reshape(-1, 1)
        highs = highs.reshape(-1, 1)
        lows = lows.reshape(-1, 1)
    T, N = buy.shape
    dates = pd.bdate_range("2025-01-02", periods=T).strftime(
        "%Y-%m-%d"
    ).tolist()
    codes = [f"S{i}" for i in range(N)]
    buy_signals = buy > buy_th
    sell_signals = sell > sell_th
    buy_signals[sell_signals] = False
    plan = TradePlan(
        buy_signals=buy_signals,
        sell_signals=sell_signals,
        buy_priority=np.where(buy_signals, buy, -np.inf),
        sell_priority=np.where(sell_signals, sell, -np.inf),
        buy_cash_limit=float(cash) * float(frac),
        sell_cash_limit=float(cash) * float(frac),
        warmup_rows=0,
        dates=dates,
        symbols=codes,
    )
    market_data = StrategyMarketData(
        indicator_matrix=np.empty((*price.shape, 0), dtype=np.float32),
        dates=dates,
        symbols=codes,
        prices=price,
        highs=highs,
        lows=lows,
        tradable=np.isfinite(price) & (price > 0),
    )
    return simulate_portfolio(
        plan,
        market_data,
        cash,
        lot,
        comm,
    )


class TestBuyExecutionPrice3DayMax:
    """买入执行价使用触发日前一日、当日、后一日最高价的最大值。"""

    def test_final_row_buy_is_pending_without_next_session_high(self):
        # t=2 缺少 t+1 high，因此窗口末买单必须保持待执行。
        tr = _sim([0, 0, 1], [0, 0, 0], [10.0, 20.0, 30.0])
        assert tr.total_trades == 0
        assert tr.pending_order_count == 1
        assert tr.final_shares[0] == 0

    def test_buy_window_shorter_when_insufficient_history(self):
        # t=0 没有 t-1，买价取 max(high[t], high[t+1]) = 20。
        tr = _sim([1, 0, 0], [0, 0, 0], [10.0, 20.0, 30.0], cash=100000.0)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 5000  # max(high[t], high[t+1]) = 20

    def test_buy_2day_window(self):
        # t=1 买价取 max(high[t-1], high[t], high[t+1]) = 30。
        tr = _sim([0, 1, 0], [0, 0, 0], [10.0, 20.0, 30.0], lot=1)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 3333  # max(high[t-1:t+1]) = 30


class TestSellExecutionPriceSingleDay:
    """卖出执行价使用触发日最低价，不与 NAV 收盘估值混用。"""

    def test_sell_uses_trigger_day_low(self):
        # t=0 按 t/t+1 最高价20买入5000股；t=4 按最低价40部分卖出。
        buy = [1, 0, 0, 0, 0]
        sell = [0, 0, 0, 0, 1]
        price = [10.0, 20.0, 30.0, 40.0, 50.0]
        tr = _sim(
            buy,
            sell,
            price,
            frac=1.0,
            lot=1,
            lows=[10.0, 20.0, 30.0, 40.0, 40.0],
        )
        # t=4 按 100000 元单笔上限卖出 2500 股，NAV 仍按收盘价50估值。
        assert tr.total_trades == 2
        assert tr.final_shares[0] == 2500
        assert tr.final_cash == 100000.0


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

    def test_finite_monthly_limit_is_ignored_by_cash_tier_execution(self):
        # 月度额度已废除；实际金额只能由单笔现金档位决定。
        tr = _sim([1, 0], [0, 0], [10.0, 10.0], frac=1.0, monthly=15000.0)
        assert tr.total_trades == 1
        assert tr.final_shares[0] == 10000


class TestReentryAllowed:
    """需求5：卖出后允许回补（无 shares==0 永久壁垒）。"""

    def test_rebuy_after_sell(self):
        # t=0 买, t=2 全卖, t=4 再买 → 3 笔交易，最终持仓 > 0
        buy = [1, 0, 0, 0, 1, 0]
        sell = [0, 0, 1, 0, 0, 0]
        price = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
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
        tr = _sim([1, 0], [0, 0], [10000.0, 10000.0], cash=5000.0, frac=1.0, lot=100)
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


class TestQuarterlyCostBasisSnapshot:
    """季度持仓成本快照：必须用【时点】成本/价格，不能用最终值（防 +16171% 假pnl）。"""

    def test_quarterly_cost_matches_actual_buy_price(self):
        # 单标的：t=0 以 ~10 买入并持有到底；季度成本应≈10，pnl 合理
        T = 200
        buy = np.zeros((T, 1))
        sell = np.zeros((T, 1))
        buy[0, 0] = 1.0
        # 价格 10 起，缓慢上涨到 12
        price = np.linspace(10.0, 12.0, T).reshape(T, 1)
        tr = _sim(buy, sell, price, frac=1.0, lot=1, cash=100000.0)
        assert len(tr.quarterly_holdings) > 0
        for q in tr.quarterly_holdings:
            for pos in q["positions"]:
                # 成本必须接近真实买入价 ~10（含手续费略高），绝不能是 0 或离谱值
                assert 9.5 <= pos["cost"] <= 10.6, f"季度成本异常: {pos}"
                # pnl% 必须在合理范围（价格 10→12，最多 +20%）
                assert -5 <= pos["pnl_pct"] <= 25, f"季度pnl%异常: {pos}"

    def test_quarterly_cost_never_zero_when_holding(self):
        # 持仓时成本单价不得为 0（旧 bug：最终cb/时点shares 错配导致 0.00）
        T = 150
        buy = np.zeros((T, 1))
        sell = np.zeros((T, 1))
        buy[0, 0] = 1.0
        price = np.full((T, 1), 5.0)
        tr = _sim(buy, sell, price, frac=1.0, lot=1)
        for q in tr.quarterly_holdings:
            for pos in q["positions"]:
                if pos["shares"] > 0:
                    assert pos["cost"] > 0, f"持仓成本为0: {pos}"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
