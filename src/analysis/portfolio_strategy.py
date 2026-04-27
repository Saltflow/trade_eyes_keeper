"""
投资组合策略分析模块

基于MA60锚节点的择时策略 + 投资组合优化。
A股和非A股分开计算两组，每组产出3个优化组合（最高收益、最小最大回撤、最高夏普比）。

核心逻辑：
  1. 个股择时引擎：MA60偏离度触发买卖
  2. 投资组合评估：月度限额约束，组合级指标计算
  3. 贪心前向选择：自动搜索最优组合
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

import pandas as pd
import numpy as np

from .rule_engine import RuleEngine, get_default_rules, Rule

logger = logging.getLogger(__name__)

# ── 策略参数 ──
BUY_THRESHOLDS = [-0.05, -0.10]  # -5%, -10%
SELL_THRESHOLDS = [0.05, 0.10, 0.15]  # +5%, +10%, +15%
MAX_BUY_PER_TRADE = 5000.0  # 每笔买入上限(元)
MAX_SELL_PER_TRADE = 10000.0  # 每笔卖出上限(元)
MIN_SELL_PER_TRADE = 2500.0  # 每笔卖出下限(元)
COMMISSION_RATE = 0.002  # 手续费率
MONTHLY_BUY_LIMIT = 15000.0  # 组合月买入限额
MONTHLY_SELL_LIMIT = 15000.0  # 组合月卖出限额
INITIAL_CASH_PER_STOCK = 10000.0  # 每只股票初始资金（兼容旧调用）
TOTAL_CAPITAL = 100000.0  # 每组总资金池（A股10万 / 非A股10万）
RISK_FREE_A = 0.02  # A股无风险利率
RISK_FREE_NON_A = 0.045  # 非A股无风险利率
MIN_TRADING_DAYS = 400  # 最少交易日数（≈2年）


# ── 数据模型 ──


@dataclass
class TradeRecord:
    """单笔交易记录"""

    date: str
    stock_code: str
    trade_type: str  # "buy" or "sell"
    price: float
    shares: int
    amount: float  # 交易金额(元)
    fee: float  # 手续费
    reason: str  # 触发原因


@dataclass
class StockMetrics:
    """个股择时策略运行指标"""

    stock_code: str
    total_return: float  # 总收益率(%)
    annual_return: float  # 年化收益率(%)
    max_drawdown: float  # 最大回撤(% ,负值)
    sharpe_ratio: float  # 夏普比率
    total_trades: int  # 总交易次数
    final_position_value: float  # 期末持仓市值
    avg_position: float  # 平均持仓市值
    daily_values: list[float] = field(default_factory=list)  # 每日总资产
    trade_log: list[TradeRecord] = field(default_factory=list)


@dataclass
class PortfolioResult:
    """投资组合优化结果"""
    name: str                # "max_return", "min_drawdown", "max_sharpe"
    group: str               # "a_share", "non_a_share"
    total_return: float      # 组合总收益率(%)
    max_drawdown: float      # 最大回撤(% ,负值)
    sharpe_ratio: float      # 夏普比率
    expected_position: float # 期末持仓市值(元)
    composition: list[str]   # 成分股列表
    trade_count: int         # 总交易次数
    stock_details: list[dict] = field(default_factory=list)  # 各股详情
    nav_series: list[float] = field(default_factory=list)    # 组合净值序列
    nav_dates: list[str] = field(default_factory=list)       # 净值对应日期


# ── 辅助函数 ──


def _detect_stock_group(stock_code: str) -> str:
    """
    判断股票分组：A股 vs 非A股
    A股: 6位纯数字代码
    非A股: 其他（港股、美股、新加坡等）
    """
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return "a_share"
    return "non_a_share"


def _get_lot_size(stock_code: str) -> int:
    """获取整手股数：A股100股，其余1股"""
    if _detect_stock_group(stock_code) == "a_share":
        return 100
    return 1


def _get_month_key(date_str: str) -> str:
    """将日期转换为月份键 'YYYY-MM'"""
    return date_str[:7]


# ── 个股择时引擎 ──


class TimingStrategyEngine:
    """
    个股MA60锚点择时策略引擎

    策略逻辑：
      - 计算每日MA60
      - deviation = (close - MA60) / MA60
      - 买入：deviation 向下突破 -5% → 买入5000；向下突破 -10% → 再买入5000
      - 卖出：deviation 向上突破 +5%/10%/15% → 各卖出1/4持仓
      - 阈值触发标记在价格回归MA60以上时重置
    """

    def __init__(self, stock_code: str, price_data: pd.DataFrame):
        """
        Args:
            stock_code: 股票代码
            price_data: DataFrame 必须含 'date' 和 'close' 列
        """
        self.stock_code = stock_code
        self.lot_size = _get_lot_size(stock_code)

        # 深拷贝数据
        self.data = price_data[["date", "close"]].copy()
        self.data["date"] = pd.to_datetime(self.data["date"])
        self.data = self.data.sort_values("date").reset_index(drop=True)

        # 计算 MA60 和 偏离度
        self.data["ma60"] = self.data["close"].rolling(window=60, min_periods=1).mean()
        self.data["deviation"] = (self.data["close"] - self.data["ma60"]) / self.data[
            "ma60"
        ]

    def run_simulation(
        self,
        monthly_buy_limit: float = MONTHLY_BUY_LIMIT,
        monthly_sell_limit: float = MONTHLY_SELL_LIMIT,
        initial_cash: float = INITIAL_CASH_PER_STOCK,
        rules: list[Rule] | None = None,
    ) -> StockMetrics:
        """
        运行择时策略模拟

        Args:
            monthly_buy_limit: 该股月度买入限额
            monthly_sell_limit: 该股月度卖出限额
            initial_cash: 初始资金
            rules: 自定义规则列表，默认使用 MA60 锚点择时规则

        Returns:
            StockMetrics 包含完整交易记录和日净值序列
        """
        if rules is None:
            rules = get_default_rules()
        engine = RuleEngine(rules)

        # ── 状态变量 ──
        cash = initial_cash
        shares = 0
        trade_log: list[TradeRecord] = []
        daily_values: list[float] = []
        prev_deviation = None

        # 月度交易统计
        monthly_buys: dict[str, float] = {}
        monthly_sells: dict[str, float] = {}

        for idx, row in self.data.iterrows():
            date_str = str(row["date"].date())
            close = float(row["close"])
            ma60 = float(row["ma60"])
            deviation = float(row["deviation"])

            # 前 60 天 MA60 不稳定，仅记录净值
            if idx < 60:
                total_value = cash + shares * close
                daily_values.append(total_value)
                prev_deviation = deviation
                continue

            month_key = _get_month_key(date_str)

            # 无效数据跳过
            if pd.isna(deviation) or pd.isna(ma60) or ma60 <= 0:
                total_value = cash + shares * close
                daily_values.append(total_value)
                prev_deviation = deviation
                continue

            # ── 构建上下文 ──
            ctx = {
                "close": close,
                "ma60": ma60,
                "deviation": deviation,
                "prev_deviation": prev_deviation,
                "cash": cash,
                "shares": shares,
                "position_value": shares * close,
                "monthly_buy_used": monthly_buys.get(month_key, 0.0),
                "monthly_sell_used": monthly_sells.get(month_key, 0.0),
                "monthly_buy_limit": monthly_buy_limit,
                "monthly_sell_limit": monthly_sell_limit,
                "lot_size": self.lot_size,
                "commission_rate": COMMISSION_RATE,
            }

            # ── 卖出执行（嵌套函数，通过 nonlocal 修改外层状态）──
            def _exec_sell(
                reason_label: str,
                fraction: float = 0.25,
                sell_min: float = 2500.0,
                sell_max: float = 10000.0,
            ):
                nonlocal shares, cash

                # 本次卖出股数：fraction * 持仓
                raw_sell = int(shares * fraction)
                sell_shares = max(
                    self.lot_size,
                    int(raw_sell / self.lot_size) * self.lot_size,
                )
                sell_amount = sell_shares * close

                # 约束1：不低于 sell_min
                if sell_amount < sell_min:
                    current_position_value = shares * close
                    if current_position_value < sell_min:
                        sell_shares = shares  # 清仓
                    else:
                        target_shares = (
                            int(sell_min / close / self.lot_size)
                            * self.lot_size
                        )
                        target_shares = max(
                            self.lot_size, min(target_shares, shares)
                        )
                        sell_shares = target_shares
                    sell_amount = sell_shares * close

                # 约束2：不超过 sell_max
                if sell_amount > sell_max:
                    capped = (
                        int(sell_max / close / self.lot_size)
                        * self.lot_size
                    )
                    sell_shares = max(
                        self.lot_size, min(capped, shares)
                    )
                    sell_amount = sell_shares * close

                # 月度限额检查
                current_month_sells = monthly_sells.get(month_key, 0.0)
                remaining_sell = max(
                    0, monthly_sell_limit - current_month_sells
                )
                if remaining_sell <= 0:
                    return

                sell_value = min(sell_amount, remaining_sell)
                if sell_value < sell_amount:
                    ratio = sell_value / sell_amount
                    sell_shares = (
                        int(int(shares * ratio) / self.lot_size)
                        * self.lot_size
                    )
                    sell_shares = max(self.lot_size, sell_shares)
                    sell_value = sell_shares * close

                if sell_shares <= 0 or sell_shares > shares:
                    return

                fee = sell_value * COMMISSION_RATE
                cash += sell_value - fee
                shares -= sell_shares
                monthly_sells[month_key] = (
                    monthly_sells.get(month_key, 0.0) + sell_value + fee
                )
                trade_log.append(
                    TradeRecord(
                        date=date_str,
                        stock_code=self.stock_code,
                        trade_type="sell",
                        price=close,
                        shares=sell_shares,
                        amount=sell_value - fee,
                        fee=fee,
                        reason=(
                            f"{reason_label} "
                            f"(偏离={deviation * 100:.1f}%)"
                        ),
                    )
                )

            # ── 评估规则引擎 ──
            for rule, action_amount in engine.evaluate_day(ctx):
                if rule.type == "buy":
                    buy_amount = min(float(action_amount), cash)
                    # 月度限额检查
                    current_month_buys = monthly_buys.get(month_key, 0.0)
                    remaining_buy = max(
                        0, monthly_buy_limit - current_month_buys
                    )
                    buy_amount = min(buy_amount, remaining_buy)

                    if buy_amount >= close * self.lot_size:
                        available = buy_amount * (1 - COMMISSION_RATE)
                        shares_to_buy = (
                            int(available / close / self.lot_size)
                            * self.lot_size
                        )
                        if shares_to_buy > 0:
                            cost = shares_to_buy * close
                            fee = cost * COMMISSION_RATE
                            if cost + fee <= buy_amount:
                                shares += shares_to_buy
                                cash -= cost + fee
                                monthly_buys[month_key] = (
                                    monthly_buys.get(month_key, 0.0)
                                    + cost + fee
                                )
                                trade_log.append(
                                    TradeRecord(
                                        date=date_str,
                                        stock_code=self.stock_code,
                                        trade_type="buy",
                                        price=close,
                                        shares=shares_to_buy,
                                        amount=cost + fee,
                                        fee=fee,
                                        reason=(
                                            f"{rule.label} "
                                            f"(偏离={deviation * 100:.1f}%)"
                                        ),
                                    )
                                )

                elif rule.type == "sell" and shares > 0:
                    _exec_sell(
                        reason_label=rule.label,
                        fraction=rule.action_fraction or 0.25,
                        sell_min=rule.action_min or 2500.0,
                        sell_max=rule.action_max or 10000.0,
                    )

            # ── 记录每日总资产 ──
            total_value = cash + shares * close
            daily_values.append(total_value)

            prev_deviation = deviation

        # ── 计算指标 ──
        if len(daily_values) < 2:
            return StockMetrics(
                stock_code=self.stock_code,
                total_return=0.0,
                annual_return=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                total_trades=0,
                final_position_value=0.0,
                avg_position=0.0,
                daily_values=daily_values,
                trade_log=trade_log,
            )

        initial_value = daily_values[0]
        final_value = daily_values[-1]
        total_return = (
            ((final_value - initial_value) / initial_value * 100)
            if initial_value > 0
            else 0.0
        )

        # 年化收益率
        num_days = len(daily_values)
        years = num_days / 252.0
        if years > 0 and initial_value > 0:
            annual_return = ((final_value / initial_value) ** (1.0 / years) - 1) * 100
        else:
            annual_return = 0.0

        # 最大回撤
        peak = daily_values[0]
        max_drawdown = 0.0
        for v in daily_values:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100
            if dd < max_drawdown:
                max_drawdown = dd

        # 夏普比率
        daily_returns = []
        for i in range(1, len(daily_values)):
            if daily_values[i - 1] > 0:
                dr = (daily_values[i] - daily_values[i - 1]) / daily_values[i - 1]
                daily_returns.append(dr)

        risk_free = (
            RISK_FREE_A
            if _detect_stock_group(self.stock_code) == "a_share"
            else RISK_FREE_NON_A
        )
        if len(daily_returns) > 5:
            excess_returns = [r - risk_free / 252 for r in daily_returns]
            mean_excess = np.mean(excess_returns)
            std_excess = np.std(excess_returns, ddof=1)
            sharpe_ratio = (
                (mean_excess / std_excess * np.sqrt(252)) if std_excess > 1e-10 else 0.0
            )
        else:
            sharpe_ratio = 0.0

        # 期末持仓市值
        final_position_value = (
            shares * float(self.data.iloc[-1]["close"]) if shares > 0 else 0.0
        )

        # 平均持仓市值
        avg_position = (
            np.mean([v - cash for v in daily_values]) if daily_values else 0.0
        )

        return StockMetrics(
            stock_code=self.stock_code,
            total_return=round(total_return, 2),
            annual_return=round(annual_return, 2),
            max_drawdown=round(max_drawdown, 2),
            sharpe_ratio=round(sharpe_ratio, 4),
            total_trades=len(trade_log),
            final_position_value=round(final_position_value, 2),
            avg_position=round(avg_position, 2),
            daily_values=daily_values,
            trade_log=trade_log,
        )


# ── 投资组合评估器 ──


class PortfolioEvaluator:
    """
    投资组合评估器

    对一组股票同时运行择时策略，应用月度限额约束，计算组合级指标。
    """

    def __init__(self, stocks_data: dict[str, pd.DataFrame], group: str,
                 rules: list[Rule] | None = None):
        """
        Args:
            stocks_data: {stock_code: DataFrame with date, close}
            group: "a_share" 或 "non_a_share"
            rules: 自定义规则，None 使用默认规则
        """
        self.stocks_data = stocks_data
        self.group = group
        self.rules = rules

    def evaluate(self, stock_codes: list[str]) -> PortfolioResult:
        """
        评估指定股票组合（共享资金池模拟）。

        所有标的共享 10 万元资金池 + 月度 1.5 万买卖限额，
        标的间竞争资金——这才是贪心搜索的意义。
        """
        if not stock_codes:
            return PortfolioResult(
                name="", group=self.group,
                total_return=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
                expected_position=0.0, composition=[], trade_count=0,
            )

        # ── 过滤有数据的标的 ──
        active_codes = [c for c in stock_codes if c in self.stocks_data]
        if not active_codes:
            return PortfolioResult(
                name="", group=self.group,
                total_return=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
                expected_position=0.0, composition=stock_codes, trade_count=0,
            )
        n = len(active_codes)

        # ── 共享状态 ──
        cash = TOTAL_CAPITAL
        positions: dict[str, int] = {c: 0 for c in active_codes}
        last_prices: dict[str, float] = {c: 0.0 for c in active_codes}
        prev_deviations: dict[str, float | None] = {c: None for c in active_codes}
        trade_count = 0
        daily_navs: list[float] = []
        nav_dates: list[str] = []
        monthly_buys: dict[str, float] = {}
        monthly_sells: dict[str, float] = {}

        # Rule engines per stock
        if self.rules is None:
            rules = get_default_rules()
        else:
            rules = self.rules
        engines: dict[str, RuleEngine] = {}
        for c in active_codes:
            engines[c] = RuleEngine(rules)

        # ── 构建统一日期轴（同时计算 MA60 和 deviation）──
        date_map: dict[str, dict[str, dict]] = {}
        for code in active_codes:
            df = self.stocks_data[code].copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()
            df["deviation"] = (df["close"] - df["ma60"]) / df["ma60"]
            dates = df["date"].dt.date.astype(str)
            for i in range(len(df)):
                d = dates.iloc[i]
                if d not in date_map:
                    date_map[d] = {}
                row = df.iloc[i]
                date_map[d][code] = {
                    "close": float(row["close"]),
                    "ma60": float(row.get("ma60", 0) or 0),
                    "deviation": float(row.get("deviation", 0) or 0),
                }

        # ── 逐日模拟 ──
        for date_str, day_data in sorted(date_map.items()):
            month_key = _get_month_key(date_str)
            month_buy_used = monthly_buys.get(month_key, 0.0)
            month_sell_used = monthly_sells.get(month_key, 0.0)

            # 当天所有标的价格（用于算 NAV）
            day_prices: dict[str, float] = {}

            for code, info in day_data.items():
                close = info["close"]
                ma60 = info["ma60"]
                deviation = info["deviation"]
                day_prices[code] = close
                last_prices[code] = close  # 追踪最后已知价格
                lot = _get_lot_size(code)

                # 跳过无锚点的早期数据
                if ma60 <= 0:
                    prev_deviations[code] = deviation
                    continue

                # 上下文
                ctx = {
                    "close": close,
                    "ma60": ma60,
                    "deviation": deviation,
                    "prev_deviation": prev_deviations.get(code),
                    "cash": cash,
                    "shares": positions[code],
                    "position_value": positions[code] * close,
                    "monthly_buy_used": month_buy_used,
                    "monthly_sell_used": month_sell_used,
                    "monthly_buy_limit": MONTHLY_BUY_LIMIT,
                    "monthly_sell_limit": MONTHLY_SELL_LIMIT,
                    "lot_size": lot,
                    "commission_rate": COMMISSION_RATE,
                }

                engine = engines[code]
                for rule, amount in engine.evaluate_day(ctx):
                    if rule.type == "buy":
                        buy_amount = min(float(amount), cash)
                        remaining = max(0, MONTHLY_BUY_LIMIT - month_buy_used)
                        buy_amount = min(buy_amount, remaining)
                        if buy_amount >= close * lot:
                            available = buy_amount * (1 - COMMISSION_RATE)
                            qty = int(available / close / lot) * lot
                            if qty > 0:
                                cost = qty * close
                                fee = cost * COMMISSION_RATE
                                if cost + fee <= buy_amount:
                                    positions[code] += qty
                                    cash -= cost + fee
                                    month_buy_used += cost + fee
                                    trade_count += 1

                    elif rule.type == "sell" and positions[code] > 0:
                        fraction = rule.action_fraction or 0.25
                        mn = rule.action_min or 2500.0
                        mx = rule.action_max or 10000.0
                        raw = int(positions[code] * fraction)
                        sell_qty = max(lot, int(raw / lot) * lot)
                        sell_amount = sell_qty * close

                        # 最低卖出额
                        if sell_amount < mn:
                            if positions[code] * close < mn:
                                sell_qty = positions[code]
                            else:
                                tgt = int(mn / close / lot) * lot
                                sell_qty = max(lot, min(tgt, positions[code]))
                            sell_amount = sell_qty * close

                        if sell_amount > mx:
                            capped = int(mx / close / lot) * lot
                            sell_qty = max(lot, min(capped, positions[code]))
                            sell_amount = sell_qty * close

                        remaining = max(0, MONTHLY_SELL_LIMIT - month_sell_used)
                        if remaining <= 0:
                            continue
                        sell_value = min(sell_amount, remaining)
                        if sell_value < sell_amount:
                            ratio = sell_value / sell_amount
                            sell_qty = int(int(positions[code] * ratio) / lot) * lot
                            sell_qty = max(lot, sell_qty)
                            sell_value = sell_qty * close

                        if sell_qty <= 0 or sell_qty > positions[code]:
                            continue

                        fee = sell_value * COMMISSION_RATE
                        cash += sell_value - fee
                        positions[code] -= sell_qty
                        month_sell_used += sell_value + fee
                        trade_count += 1

                prev_deviations[code] = deviation

            # ── 当日 NAV（缺数据标的用最后已知价，避免毛刺）──
            position_value = sum(
                positions[c] * (
                    day_prices.get(c) if day_prices.get(c) is not None
                    else last_prices.get(c, 0)
                )
                for c in active_codes
            )
            daily_navs.append(cash + position_value)
            nav_dates.append(date_str)
            monthly_buys[month_key] = month_buy_used
            monthly_sells[month_key] = month_sell_used

        # ── 指标计算 ──
        if len(daily_navs) < 2:
            return PortfolioResult(
                name="", group=self.group,
                total_return=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
                expected_position=0.0, composition=stock_codes, trade_count=0,
                nav_series=daily_navs, nav_dates=nav_dates,
            )

        initial_nav = daily_navs[0]
        final_nav = daily_navs[-1]
        total_return = (
            (final_nav - initial_nav) / initial_nav * 100
            if initial_nav > 0 else 0.0
        )
        num_days = len(daily_navs)
        years = num_days / 252.0
        annual_return = (
            (final_nav / initial_nav) ** (1.0 / years) - 1
        ) * 100 if years > 0 and initial_nav > 0 else 0.0
        peak = daily_navs[0]
        max_drawdown = 0.0
        for v in daily_navs:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100
            if dd < max_drawdown:
                max_drawdown = dd

        daily_returns = []
        for i in range(1, len(daily_navs)):
            if daily_navs[i - 1] > 0:
                daily_returns.append(
                    (daily_navs[i] - daily_navs[i - 1]) / daily_navs[i - 1]
                )
        risk_free = RISK_FREE_A if self.group == "a_share" else RISK_FREE_NON_A
        sharpe = 0.0
        if len(daily_returns) > 5:
            excess = [r - risk_free / 252 for r in daily_returns]
            mean_ex = np.mean(excess)
            std_ex = np.std(excess, ddof=1)
            sharpe = (mean_ex / std_ex * np.sqrt(252)) if std_ex > 1e-10 else 0.0

        position_value = sum(
            positions[c] * float(
                self.stocks_data[c].iloc[-1]["close"]
            ) for c in active_codes if positions[c] > 0
        )

        return PortfolioResult(
            name="", group=self.group,
            total_return=round(total_return, 2),
            max_drawdown=round(max_drawdown, 2),
            sharpe_ratio=round(sharpe, 4),
            expected_position=round(position_value, 2),
            composition=list(active_codes),
            trade_count=trade_count,
            nav_series=[round(v, 2) for v in daily_navs],
            nav_dates=nav_dates,
            stock_details=[],
        )


# ── 投资组合优化器 ──


class PortfolioOptimizer:
    """
    投资组合优化器

    贪心前向选择（Greedy Forward Selection）搜索最优组合。
    对 A股 / 非A股 两组分别搜索 3 个目标：
      1. 最高收益 (max_return)
      2. 最小最大回撤 (min_drawdown)
      3. 最佳夏普比 (max_sharpe)
    """

    def __init__(self, config: dict):
        self.config = config
        self._data_source = None

    @property
    def data_source(self):
        if self._data_source is None:
            from ..data.data_source import DataSource

            self._data_source = DataSource(self.config)
        return self._data_source

    def fetch_stock_data(self, stock_code: str, days: int = 730) -> pd.DataFrame:
        """通过 DataSource 获取历史数据"""
        try:
            data = self.data_source.fetch_stock_data(stock_code, days)
            if data is not None and not data.empty and "close" in data.columns:
                return data
        except Exception as e:
            logger.warning(f"获取 {stock_code} 数据失败: {e}")
        return pd.DataFrame()

    def run(self) -> dict:
        """
        运行投资组合优化

        Returns:
            dict: {
                "a_share": {
                    "max_return": PortfolioResult,
                    "min_drawdown": PortfolioResult,
                    "max_sharpe": PortfolioResult
                },
                "non_a_share": { ... }
            }
        """
        # 获取所有股票代码
        stocks = self.config.get("stocks", [])
        if not stocks:
            logger.warning("配置中无股票")
            return {}

        # 从配置加载策略参数
        ps_config = self.config.get("portfolio_strategy", {})
        lookback_days = ps_config.get("lookback_days", 730)

        # 加载自定义规则（可选）
        config_rules = ps_config.get("rules", None)
        custom_rules = None
        if config_rules:
            custom_rules = [Rule.from_dict(r) for r in config_rules]
            logger.info(f"投资组合使用自定义规则: {len(custom_rules)} 条")

        # 获取数据并分组
        a_share_data: dict[str, pd.DataFrame] = {}
        non_a_share_data: dict[str, pd.DataFrame] = {}

        for code in stocks:
            code_str = str(code)
            group = _detect_stock_group(code_str)

            data = self.fetch_stock_data(code_str, lookback_days)
            if data.empty or "close" not in data.columns:
                logger.warning(f"{code_str} 数据不足，跳过")
                continue
            if len(data) < MIN_TRADING_DAYS:
                logger.info(
                    f"{code_str} 仅有 {len(data)} 天数据(<{MIN_TRADING_DAYS})，跳过"
                )
                continue

            if group == "a_share":
                a_share_data[code_str] = data
            else:
                non_a_share_data[code_str] = data

        logger.info(
            f"投资组合优化: A股{len(a_share_data)}只, 非A股{len(non_a_share_data)}只"
        )

        results = {}
        for group_name, group_data in [
            ("a_share", a_share_data),
            ("non_a_share", non_a_share_data),
        ]:
            if not group_data:
                logger.info(f"{group_name} 无足够数据，跳过")
                results[group_name] = {}
                continue

            evaluator = PortfolioEvaluator(
                group_data, group_name, rules=custom_rules
            )
            codes = list(group_data.keys())

            results[group_name] = {
                "max_return": self._greedy_search(codes, evaluator, "total_return", maximize=True),
                # max_drawdown 是负值(-X%)，越大(越接近0)代表回撤越小
                "min_drawdown": self._greedy_search(codes, evaluator, "max_drawdown", maximize=True),
                "max_sharpe": self._greedy_search(codes, evaluator, "sharpe_ratio", maximize=True),
            }

        return results

    def _greedy_search(
        self,
        candidates: list[str],
        evaluator: PortfolioEvaluator,
        metric_key: str,
        maximize: bool = True,
    ) -> PortfolioResult:
        """
        贪心前向选择

        Args:
            candidates: 候选股票列表
            evaluator: PortfolioEvaluator 实例
            metric_key: 优化目标字段名
            maximize: True=最大化, False=最小化

        Returns:
            PortfolioResult
        """
        if not candidates:
            return PortfolioResult(
                name=metric_key,
                group=evaluator.group,
                total_return=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                expected_position=0.0,
                composition=[],
                trade_count=0,
            )

        selected: list[str] = []
        remaining = list(candidates)

        best_score = -float("inf") if maximize else float("inf")
        best_result: Optional[PortfolioResult] = None

        while remaining:
            improved = False
            step_best_score = best_score
            step_best_stock = None
            step_best_result = None

            for stock in remaining:
                trial = selected + [stock]
                result = evaluator.evaluate(trial)
                score = getattr(result, metric_key, 0.0)

                if maximize and score > step_best_score:
                    step_best_score = score
                    step_best_stock = stock
                    step_best_result = result
                    improved = True
                elif not maximize and score < step_best_score:
                    step_best_score = score
                    step_best_stock = stock
                    step_best_result = result
                    improved = True

            if improved and step_best_result is not None:
                selected.append(step_best_stock)
                remaining.remove(step_best_stock)
                best_score = step_best_score
                best_result = step_best_result
                logger.info(
                    f"  [{evaluator.group}] {metric_key}: 添加 {step_best_stock}, "
                    f"score={step_best_score:.4f}, 组合大小={len(selected)}"
                )
            else:
                break

        if best_result is None:
            best_result = (
                evaluator.evaluate(selected)
                if selected
                else PortfolioResult(
                    name=metric_key,
                    group=evaluator.group,
                    total_return=0.0,
                    max_drawdown=0.0,
                    sharpe_ratio=0.0,
                    expected_position=0.0,
                    composition=[],
                    trade_count=0,
                )
            )

        best_result.name = metric_key
        return best_result


# ── 投资组合图表生成 ──



def generate_portfolio_chart(
    portfolio_results: dict,
    bollinger_window: int = 90,
) -> dict[str, bytes] | None:
    """
    生成投资组合NAV走势图（每组一张独立PNG）

    - A股：3条投资组合净值曲线 + 布林带
    - 非A股：3条曲线 + 布林带
    - 每条曲线上叠加布林带（SMA ± 2×标准差，半透明填充）

    Args:
        portfolio_results: PortfolioOptimizer.run() 返回的字典
        bollinger_window: 布林带窗口天数，默认 90
    """

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    from io import BytesIO

    # ── 设置中文字体 ──
    from ..utils.font_setup import setup_cjk_font
    setup_cjk_font()

    # ── 图表配置 ──
    metric_labels = {
        "max_return": "最高收益",
        "min_drawdown": "最小回撤",
        "max_sharpe": "最优夏普",
    }
    metric_colors = {
        "max_return": "#2e7d32",
        "min_drawdown": "#1565c0",
        "max_sharpe": "#e65100",
    }
    group_labels = {"a_share": "A股投资组合", "non_a_share": "非A股投资组合"}
    output: dict[str, bytes] = {}

    for group_key in ("a_share", "non_a_share"):
        gd = portfolio_results.get(group_key, {})
        if not gd:
            continue

        # 收集该组所有指标数据
        subplots_data = {}
        for metric_key in ("max_return", "min_drawdown", "max_sharpe"):
            result = gd.get(metric_key)
            if result and result.nav_series and len(result.nav_series) >= 20:
                subplots_data[metric_key] = (result.nav_series, result.nav_dates)

        if not subplots_data:
            continue

        # ── 为该组创建独立画布 ──
        fig, ax = plt.subplots(figsize=(14, 5))
        title = f"{group_labels.get(group_key, group_key)} — 净值走势与布林带（近2年）"
        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_ylabel("组合净值 (基准100)", fontsize=11)
        ax.grid(True, alpha=0.25, linestyle="--")

        for metric_key in ("max_return", "min_drawdown", "max_sharpe"):
            if metric_key not in subplots_data:
                continue
            navs, dates = subplots_data[metric_key]
            color = metric_colors[metric_key]
            label = metric_labels[metric_key]

            nav_arr = np.array(navs)
            # 归一化到起点100
            base = nav_arr[0] if nav_arr[0] > 0 else 1.0
            nav_arr = nav_arr / base * 100

            # 判断曲线波动幅度
            data_range = nav_arr.max() - nav_arr.min()
            if data_range < 2.0:
                # 几乎平的线 → 虚线 + 图例注明"(近似水平)"
                linestyle = "--"
                label_with_note = f"{label} (≈水平, 波动<2%)"
                alpha = 0.55
            else:
                linestyle = "-"
                label_with_note = label
                alpha = 0.85
            try:
                dt_arr = pd.to_datetime(dates)
            except Exception:
                dt_arr = pd.date_range(end=pd.Timestamp.now(), periods=len(navs), freq="B")

            # 主曲线（zorder=5 确保画在最上层）
            ax.plot(dt_arr, nav_arr, color=color, linewidth=2.0,
                    alpha=alpha, linestyle=linestyle, label=label_with_note,
                    zorder=5)

            # 布林带 — 填充 + 上下界（平线不画带）
            if len(nav_arr) >= bollinger_window and data_range >= 2.0:
                window = bollinger_window
                sma = pd.Series(nav_arr).rolling(window=window, min_periods=1).mean()
                std = pd.Series(nav_arr).rolling(window=window, min_periods=1).std()
                upper = sma + 2 * std
                lower = sma - 2 * std
                upper = upper.bfill().values
                lower = lower.bfill().values

                # 填充区 zorder=1（最底层）
                ax.fill_between(dt_arr, lower, upper,
                                alpha=0.10, color=color, linewidth=0,
                                zorder=1)
                # 上下边界线 zorder=2
                ax.plot(dt_arr, upper, color=color,
                        linewidth=0.6, alpha=0.25, linestyle="--",
                        zorder=2)
                ax.plot(dt_arr, lower, color=color,
                        linewidth=0.6, alpha=0.25, linestyle="--",
                        zorder=2)

        # X轴
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.tick_params(axis="x", labelsize=9, rotation=30)
        ax.tick_params(axis="y", labelsize=9)

        # 强制 Y 轴贴合数据范围（避免平线被超大 margin 吃没）
        ax.relim()
        ax.autoscale_view(scalex=False, scaley=True)

        # Legend
        ax.legend(loc="upper left", fontsize=10, framealpha=0.85, edgecolor="#ccc", ncol=3)

        fig.tight_layout(rect=[0, 0, 1, 0.93])

        try:
            buf = BytesIO()
            fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
            buf.seek(0)
            output[group_key] = buf.read()
            plt.close(fig)
            logger.info(f"投资组合图表 {group_key} 生成成功")
        except Exception as e:
            logger.error(f"投资组合图表 {group_key} 生成失败: {e}")
            plt.close(fig)

    if not output:
        return None
    return output
