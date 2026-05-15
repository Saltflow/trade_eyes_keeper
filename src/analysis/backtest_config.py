"""
回测约束配置

定义回测时间线（观察期 / 交易期 / 持仓期）、资金注入计划、月度限额等。
供 PortfolioEvaluator 和 StrategyOptimizer 共用。

时间线示意（默认 24 个月）:
    月0 ── 6 ────────── 18 ── 24
      observe  trade    hold
"""

from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field


class BacktestConfig(BaseModel):
    """回测约束配置

    Attributes:
        observe_end_month: 观察期结束月（0 到此月之前仅观察，不交易）
        trade_end_month: 交易期结束月（此月之后进入持仓期，不交易）
        capital_injections: 资金注入计划 {month → amount}
        initial_capital: 初始资金池
        monthly_buy_limit: 月买入限额 (float('inf') 表示不限制)
        monthly_sell_limit: 月卖出限额
        commission_rate: 手续费率
        lot_size_override: 单只股票的手数覆盖 {stock_code → lot_size}
    """

    observe_end_month: int = 6
    trade_end_month: int = 18
    rf_rate: float = 2.0  # 无风险利率 (%) — A股默认2%, 境外设4.5%
    capital_injections: dict[int, float] = Field(default_factory=dict)
    initial_capital: float = 100000.0
    monthly_buy_limit: float = float("inf")
    monthly_sell_limit: float = float("inf")
    commission_rate: float = 0.002
    lot_size_override: Optional[dict[str, int]] = None

    def get_phase(self, elapsed_months: float) -> str:
        """判断当前处于哪个阶段

        Returns:
            "observe", "trade", 或 "hold"
        """
        if elapsed_months < self.observe_end_month:
            return "observe"
        if elapsed_months >= self.trade_end_month:
            return "hold"
        return "trade"

    def can_trade(self, elapsed_months: float) -> bool:
        """当前是否允许交易"""
        return self.get_phase(elapsed_months) == "trade"

    def get_injection(self, month: int) -> float:
        """获取指定月的资金注入额（未配置返回 0）"""
        return self.capital_injections.get(month, 0.0)

    def get_lot_size(self, stock_code: str, default: int) -> int:
        """获取手数（支持覆盖）"""
        if self.lot_size_override and stock_code in self.lot_size_override:
            return self.lot_size_override[stock_code]
        return default


def elapsed_months(date_str: str, ref_date_str: str) -> float:
    """计算自参考日期起的经过月数（小数）

    Args:
        date_str: 当前日期 'YYYY-MM-DD'
        ref_date_str: 参考起始日期

    Returns:
        经过的月数（含小数部分）
    """
    d = pd.Timestamp(date_str)
    ref = pd.Timestamp(ref_date_str)
    months = (d.year - ref.year) * 12 + (d.month - ref.month)
    months += (d.day - ref.day) / 30.0
    return round(months, 2)


def make_default_optimizer_config() -> BacktestConfig:
    """生成策略搜索用的默认回测配置（训练: 0-12 月）"""
    from collections import OrderedDict

    injections = OrderedDict()
    for m in range(6, 13):  # months 6-12 inclusive
        injections[m] = 20000.0

    return BacktestConfig(
        observe_end_month=6,
        trade_end_month=18,  # full 24-month timeline
        capital_injections=injections,
        initial_capital=100000.0,
        monthly_buy_limit=float("inf"),
        monthly_sell_limit=float("inf"),
        commission_rate=0.002,
    )


def make_training_config() -> BacktestConfig:
    """生成训练用的回测配置（仅 0-12 月，用于贝叶斯优化）"""
    from collections import OrderedDict

    injections = OrderedDict()
    for m in range(6, 13):
        injections[m] = 20000.0

    return BacktestConfig(
        observe_end_month=6,
        trade_end_month=12,  # truncated to 12 months for training
        capital_injections=injections,
        initial_capital=100000.0,
        monthly_buy_limit=float("inf"),
        monthly_sell_limit=float("inf"),
        commission_rate=0.002,
    )
