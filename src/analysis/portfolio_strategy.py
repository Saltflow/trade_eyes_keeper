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
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field

from .rule_engine import RuleEngine, get_default_rules, Rule
from .backtest_config import BacktestConfig, elapsed_months

logger = logging.getLogger(__name__)

# ── 策略参数 ──
BUY_THRESHOLDS = [-0.05, -0.10]  # -5%, -10%
SELL_THRESHOLDS = [0.05, 0.10, 0.15]  # +5%, +10%, +15%
MAX_BUY_PER_TRADE = 5000.0  # 每笔买入上限(元)
MAX_SELL_PER_TRADE = 10000.0  # 每笔卖出上限(元)
MIN_SELL_PER_TRADE = 2500.0  # 每笔卖出下限(元)
COMMISSION_RATE = 0.005  # 手续费率 (0.5%, 含滑点)
MONTHLY_BUY_LIMIT = 15000.0  # 组合月买入限额
MONTHLY_SELL_LIMIT = 15000.0  # 组合月卖出限额
INITIAL_CASH_PER_STOCK = 10000.0  # 每只股票初始资金（兼容旧调用）
TOTAL_CAPITAL = 100000.0  # 每组总资金池（A股10万 / 非A股10万）
RISK_FREE_A = 0.02  # A股无风险利率
RISK_FREE_NON_A = 0.045  # 非A股无风险利率
MIN_TRADING_DAYS = 400  # 最少交易日数（≈2年）
MIN_EVAL_DAYS = 60      # 日报/验证期评估最低门槛（够算MA60+指标+告警）


# ── 数据模型 ──


class TradeRecord(BaseModel):
    """单笔交易记录"""

    date: str
    stock_code: str
    trade_type: str  # "buy" or "sell"
    price: float
    shares: int
    amount: float  # 交易金额(元)
    fee: float  # 手续费
    reason: str  # 触发原因


class StockMetrics(BaseModel):
    """个股择时策略运行指标"""

    stock_code: str
    total_return: float  # 总收益率(%)
    annual_return: float  # 年化收益率(%)
    max_drawdown: float  # 最大回撤(% ,负值)
    sharpe_ratio: float  # 夏普比率
    total_trades: int  # 总交易次数
    final_position_value: float  # 期末持仓市值
    avg_position: float  # 平均持仓市值
    daily_values: list[float] = Field(default_factory=list)  # 每日总资产
    trade_log: list[TradeRecord] = Field(default_factory=list)


class SubPeriodMetrics(BaseModel):
    """子区间回测指标"""

    label: str  # "observe", "deploy", "test"
    start_month: float  # 起始月序号
    end_month: float  # 结束月序号
    total_return: float  # 区间总收益率(%)
    max_drawdown: float  # 区间最大回撤(%, 负值)
    sharpe_ratio: float  # 区间夏普比率
    trade_count: int = 0  # 区间交易次数
    excess_return: float = 0.0  # 超额收益 = 真实收益 − 现金基准收益


class PortfolioResult(BaseModel):
    """投资组合优化结果"""
    name: str                # "max_return", "min_drawdown", "max_sharpe"
    group: str               # "a_share", "non_a_share"
    total_return: float      # 组合总收益率(%)
    max_drawdown: float      # 最大回撤(% ,负值)
    sharpe_ratio: float      # 夏普比率
    expected_position: float # 期末持仓市值(元)
    composition: list[str]   # 成分股列表
    trade_count: int         # 总交易次数
    stock_details: list[dict] = Field(default_factory=list)  # 各股详情
    nav_series: list[float] = Field(default_factory=list)    # 组合净值序列
    nav_dates: list[str] = Field(default_factory=list)       # 净值对应日期
    sub_periods: dict[str, SubPeriodMetrics] = Field(default_factory=dict)  # 子区间指标
    quarterly_holdings: list[dict] = Field(default_factory=list)  # 季末持仓快照


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


def _detect_fine_group(stock_code: str) -> str:
    """细分组：a_share / hk / us（用于日报独立资金池，不影响回测/优化器二分）。

    - 6位纯数字 → a_share (601728, 000958)
    - 5位纯数字 → hk (00883, 01816)
    - 含字母 → us (VOO, BRK.B, GOOG)
    """
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return "a_share"
    if code.isdigit() and len(code) == 5:
        return "hk"
    return "us"


def get_skip_search(config: dict) -> set[str]:
    """config.skip_search 标的集合（不参与搜参）。"""
    return {str(c).strip() for c in (config.get("skip_search") or [])}


def get_skip_signals(config: dict) -> set[str]:
    """config.skip_signals 标的集合（不显示策略信号）。"""
    return {str(c).strip() for c in (config.get("skip_signals") or [])}


def _get_lot_size(stock_code: str) -> int:
    """获取整手股数：A股100股，港股100股（单价分界在日回报测处判断），其余1股"""
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return 100  # A股
    if code.isdigit() and len(code) == 5:
        return 100  # 港股默认
    return 1  # 美股/其他


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
                 rules: list[Rule] | None = None, signal_fn=None):
        """
        Args:
            stocks_data: {stock_code: DataFrame with date, close}
            group: "a_share" 或 "non_a_share"
            rules: 自定义规则，None 使用默认规则
            signal_fn: SignalFn 引擎（非空且 rules 为 __signal_fn__ 时用评分流水线）
        """
        self.stocks_data = stocks_data
        self.group = group
        self.rules = rules
        self.signal_fn = signal_fn

    def _uses_signal_fn(self) -> bool:
        """规则是否为 SignalFn 评分标记（condition=__signal_fn__）。"""
        if self.signal_fn is None or not self.rules:
            return False
        return any(getattr(r, "condition", "") == "__signal_fn__" for r in self.rules)

    def _params_from_rules(self):
        """从当前 group 最新 YAML 读回引擎参数（Params）。"""
        # rules 不含参数，需从 YAML params 还原；由 run_fixed 注入 self._engine_params
        return getattr(self, "_engine_params", None)

    def _evaluate_signal_fn(
        self, active_codes, backtest_config, indicators_data,
        initial_capital, commission,
    ) -> "PortfolioResult":
        """分位/评分引擎日报回测：与优化器同一评分流水线。

        对每只标的算整段历史每日买/卖评分，对齐统一日期轴 → 评分矩阵
        → simulate_portfolio（与搜参一致的决策仿真）。
        """
        import numpy as np
        from .signal_functions import simulate_portfolio, compute_metrics

        params = self._params_from_rules()
        if params is None:
            logger.warning("signal_fn 评估缺少 engine_params，回退空结果")
            return PortfolioResult(
                name="", group=self.group, total_return=0.0, max_drawdown=0.0,
                sharpe_ratio=0.0, expected_position=0.0,
                composition=active_codes, trade_count=0,
            )

        # 统一日期轴（并集，升序）
        date_set: set[str] = set()
        per_code_df: dict[str, pd.DataFrame] = {}

        # 补齐分位源指标 (rsi/adx/vol_ratio)，deviation/ma200_dev 由引擎兜底
        try:
            from .indicator_library import compute_all
            computed = compute_all({c: self.stocks_data[c] for c in active_codes})
        except Exception as e:
            logger.warning(f"signal_fn 指标计算失败，仅用兜底列: {e}")
            computed = {}

        for code in active_codes:
            df = computed.get(code)
            if df is None:
                df = self.stocks_data[code].copy()
            else:
                df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").reset_index(drop=True)
            per_code_df[code] = df
            date_set.update(df["date"].tolist())

        dates = sorted(date_set)
        T = len(dates)
        N = len(active_codes)
        if T == 0 or N == 0:
            return PortfolioResult(
                name="", group=self.group, total_return=0.0, max_drawdown=0.0,
                sharpe_ratio=0.0, expected_position=0.0,
                composition=active_codes, trade_count=0,
            )
        date_idx = {d: i for i, d in enumerate(dates)}

        buy_scores = np.zeros((T, N), dtype=np.float64)
        sell_scores = np.zeros((T, N), dtype=np.float64)
        price = np.full((T, N), np.nan, dtype=np.float64)

        for j, code in enumerate(active_codes):
            df = per_code_df[code]
            b, s = self.signal_fn.score_timeseries(params, df)
            closes = df["close"].astype(float).values
            for k, d in enumerate(df["date"].tolist()):
                ti = date_idx.get(d)
                if ti is None:
                    continue
                price[ti, j] = closes[k]
                if k < len(b):
                    buy_scores[ti, j] = b[k]
                    sell_scores[ti, j] = s[k]

        # 前向填充价格（停牌/日期缺失）
        for j in range(N):
            last = np.nan
            for ti in range(T):
                if np.isnan(price[ti, j]):
                    price[ti, j] = last
                else:
                    last = price[ti, j]
        price = np.nan_to_num(price, nan=0.0)

        exec_p = self.signal_fn.execution_params(params)
        monthly = MONTHLY_BUY_LIMIT
        # 手数 + 汇率：按标的细分组决定
        from .portfolio_strategy import _detect_fine_group
        fine_groups = {c: _detect_fine_group(str(c)) for c in active_codes}
        # 汇率乘数（固定：1 USD=7 CNY, 1 HKD=0.9 CNY, A股=1）
        fx_map = {"a_share": 1.0, "hk": 0.9, "us": 7.0}
        # 取主力组别的汇率（各组标的池独立，同一 group 内汇率一致）
        main_fg = max(set(fine_groups.values()), key=lambda g: sum(1 for v in fine_groups.values() if v == g))
        fx = fx_map.get(main_fg, 1.0)
        # 手数：A股100；港股按均价分界（<100→1000股，≥100→100股）；美股1
        if main_fg == "a_share":
            lot = 100
        elif main_fg == "hk":
            avg_close = float(np.mean(price[-1][price[-1] > 0])) if np.any(price[-1] > 0) else 100.0
            lot = 1000 if avg_close < 100 else 100
        else:
            lot = 1
        price = price * fx  # 汇率折算为 CNY 等价

        trace = simulate_portfolio(
            buy_scores, sell_scores, price,
            float(initial_capital),
            float(exec_p.get("buy_threshold", 0.0)),
            float(exec_p.get("sell_threshold", 0.0)),
            float(exec_p.get("position_frac", 0.15)),
            lot, float(monthly), float(commission),
            dates=dates, stock_codes=list(active_codes),
        )

        # 基准：本组价格基准（risk_free 兜底）用于超额收益
        metrics = compute_metrics(trace, benchmark_series=None)

        return PortfolioResult(
            name="", group=self.group,
            total_return=trace.total_return_pct,
            max_drawdown=trace.max_drawdown_pct,
            sharpe_ratio=trace.sharpe_ratio,
            expected_position=trace.avg_position_pct,
            composition=list(active_codes),
            trade_count=trace.total_trades,
            nav_series=trace.nav_series,
            nav_dates=trace.nav_dates,
            quarterly_holdings=trace.quarterly_holdings,
        )

    def evaluate(
        self,
        stock_codes: list[str],
        backtest_config: BacktestConfig | None = None,
        indicators_data: dict[str, "pd.DataFrame"] | None = None,
    ) -> PortfolioResult:
        """
        评估指定股票组合（共享资金池模拟）。

        Args:
            stock_codes: 待评估的股票代码列表
            backtest_config: 回测约束（None=全时段自由交易，使用旧默认参数）
            indicators_data: 预计算的指标数据 {code: DataFrame}，列会合并到上下文

        当 backtest_config 提供时：
          - 观察期（0~observe_end_month）：只更新偏离，不交易
          - 交易期（observe~trade_end_month）：正常买卖，按月注入资金
          - 持仓期（trade_end_month+）：不交易，仅跟踪净值
        """
        if not stock_codes:
            return PortfolioResult(
                name="", group=self.group,
                total_return=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
                expected_position=0.0, composition=[], trade_count=0,
            )

        # ── 参数确定 ──
        cfg = backtest_config
        effective_initial_capital = cfg.initial_capital if cfg else TOTAL_CAPITAL
        effective_buy_limit = cfg.monthly_buy_limit if cfg else MONTHLY_BUY_LIMIT
        effective_sell_limit = cfg.monthly_sell_limit if cfg else MONTHLY_SELL_LIMIT
        effective_commission = cfg.commission_rate if cfg else COMMISSION_RATE

        # ── 过滤有数据的标的 ──
        active_codes = [c for c in stock_codes if c in self.stocks_data]
        if not active_codes:
            return PortfolioResult(
                name="", group=self.group,
                total_return=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
                expected_position=0.0, composition=stock_codes, trade_count=0,
            )
        n = len(active_codes)

        # ── 分位/评分引擎路径：规则为 __signal_fn__ 时走评分流水线 ──
        if self._uses_signal_fn():
            return self._evaluate_signal_fn(
                active_codes, backtest_config, indicators_data,
                effective_initial_capital, effective_commission,
            )

        # ── 共享状态 ──
        cash = effective_initial_capital
        positions: dict[str, int] = {c: 0 for c in active_codes}
        cost_basis: dict[str, float] = {c: 0.0 for c in active_codes}
        last_prices: dict[str, float] = {c: 0.0 for c in active_codes}
        prev_deviations: dict[str, float | None] = {c: None for c in active_codes}
        trade_count = 0
        daily_navs: list[float] = []
        nav_dates: list[str] = []
        monthly_buys: dict[str, float] = {}
        monthly_sells: dict[str, float] = {}
        # 资金注入追踪（仅 backtest_config 模式）
        injected_months: set[int] = set()
        cumulative_injected: float = 0.0  # 累计注入金额（用于计算现金基准）
        # 现金基准线（含无风险复利）: initial_capital 每天 ×(1+r_f/252)，注入时直接加
        risk_free = RISK_FREE_A if self.group == "a_share" else RISK_FREE_NON_A
        daily_rf = risk_free / 252.0
        cash_benchmark = effective_initial_capital
        daily_benchmark_navs: list[float] = []
        # 按 3 阶段分桶的净值序列 + 交易数（用于子区间指标和收敛图）
        phase_navs: dict[str, list[float]] = {
            "observe": [], "deploy": [], "test": [],
        }
        phase_dates: dict[str, list[str]] = {
            "observe": [], "deploy": [], "test": [],
        }
        phase_trade_count: dict[str, int] = {
            "observe": 0, "deploy": 0, "test": 0,
        }
        quarterly_holdings: list[dict] = []

        # Rule engines per stock
        if self.rules is None:
            rules = get_default_rules()
        else:
            rules = self.rules
        engines: dict[str, RuleEngine] = {}
        for c in active_codes:
            engines[c] = RuleEngine(rules)

        # ── 构建统一日期轴（同时计算 MA60、deviation，合并指标列）──
        date_map: dict[str, dict[str, dict]] = {}
        ref_date_str: str | None = None  # 参考起始日期（用于月序号计算）
        for code in active_codes:
            df = self.stocks_data[code].copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()
            df["deviation"] = (df["close"] - df["ma60"]) / df["ma60"]

            # 先合并预计算指标（来自 compute_all / optimizer），再兜底
            if indicators_data and code in indicators_data:
                ind_df = indicators_data[code]
                if "date" in ind_df.columns:
                    ind_df = ind_df.copy()
                    ind_df["date"] = pd.to_datetime(ind_df["date"])
                    # 只合并指标列（排除 date/close/open 等已有列）
                    indicator_cols = [
                        c for c in ind_df.columns
                        if c not in ("date", "open", "close", "high", "low",
                                     "volume", "amount", "amplitude", "change_pct",
                                     "change", "turnover", "stock_code", "stock_name")
                    ]
                    if indicator_cols:
                        df = df.merge(
                            ind_df[["date"] + indicator_cols],
                            on="date", how="left",
                        )

            # 兜底：merge 后关键指标仍缺失则内联计算
            if "rsi" not in df.columns:
                delta = df["close"].diff()
                gain = delta.clip(lower=0)
                loss = (-delta).clip(lower=0)
                avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
                avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
                rs = avg_gain / avg_loss.replace(0, float("nan"))
                df["rsi"] = 100.0 - 100.0 / (1.0 + rs)
            if "boll_pct_b" not in df.columns:
                roll = df["close"].rolling(20, min_periods=1)
                upper = roll.mean() + 2 * roll.std()
                lower = roll.mean() - 2 * roll.std()
                boll_range = upper - lower
                df["boll_pct_b"] = ((df["close"] - lower) / boll_range.replace(0, float("nan"))).clip(0, 1)

            # 兜底计算搜参策略需要的额外指标
            if "adx" not in df.columns:
                # high/low 缺失时用 close 代替（ADX 退化但不崩）
                high = df["high"] if "high" in df.columns else df["close"]
                low = df["low"] if "low" in df.columns else df["close"]
                tr = pd.concat([
                    high - low,
                    (high - df["close"].shift()).abs(),
                    (low - df["close"].shift()).abs(),
                ], axis=1).max(axis=1)
                atr = tr.ewm(alpha=1/14, adjust=False).mean()
                up = high.diff()
                dn = -low.diff()
                p_dm = up.where((up > 0) & (up > dn), 0.0)
                n_dm = dn.where((dn > 0) & (dn > up), 0.0)
                a_up = p_dm.ewm(alpha=1/14, adjust=False).mean()
                a_dn = n_dm.ewm(alpha=1/14, adjust=False).mean()
                di_p = (a_up / atr.replace(0, float("nan"))) * 100
                di_n = (a_dn / atr.replace(0, float("nan"))) * 100
                dx = ((di_p - di_n).abs() / (di_p + di_n).replace(0, float("nan"))) * 100
                df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

            if "macd_hist" not in df.columns:
                ema12 = df["close"].ewm(span=12, adjust=False).mean()
                ema26 = df["close"].ewm(span=26, adjust=False).mean()
                macd_line = ema12 - ema26
                df["macd_hist"] = macd_line - macd_line.ewm(span=9, adjust=False).mean()

            if "ma200_dev" not in df.columns:
                ma200 = df["close"].rolling(window=200, min_periods=1).mean()
                df["ma200_dev"] = (df["close"] - ma200) / ma200.replace(0, float("nan"))

            if "ma60_slope" not in df.columns:
                mv = df["ma60"] if "ma60" in df.columns else df["close"].rolling(60, min_periods=1).mean()
                df["ma60_slope"] = mv / mv.shift(20).replace(0, float("nan")) - 1.0

            if "pct_from_ath" not in df.columns:
                ath = df["close"].rolling(window=504, min_periods=1).max()
                df["pct_from_ath"] = df["close"] / ath.replace(0, float("nan")) - 1.0

            if "vol_ratio" not in df.columns and "volume" in df.columns:
                vol_ma5 = df["volume"].rolling(5, min_periods=1).mean()
                df["vol_ratio"] = df["volume"] / vol_ma5.replace(0, float("nan"))

            dates = df["date"].dt.date.astype(str)
            for i in range(len(df)):
                d = dates.iloc[i]
                if d not in date_map:
                    date_map[d] = {}
                    if ref_date_str is None:
                        ref_date_str = d
                row = df.iloc[i]
                info = {
                    "close": float(row["close"]),
                    "ma60": float(row.get("ma60", 0) or 0),
                    "deviation": float(row.get("deviation", 0) or 0),
                }
                # 携带指标列到上下文
                for col in df.columns:
                    if col not in ("date", "open", "close", "high", "low",
                                   "volume", "ma60", "deviation"):
                        val = row.get(col)
                        if val is not None and not (isinstance(val, float) and pd.isna(val)):
                            info[col] = float(val) if isinstance(val, (int, float, np.floating)) else val
                date_map[d][code] = info

        # ── 逐日模拟 ──
        for date_str, day_data in sorted(date_map.items()):
            month_key = _get_month_key(date_str)
            month_buy_used = monthly_buys.get(month_key, 0.0)
            month_sell_used = monthly_sells.get(month_key, 0.0)

            # ── 资金注入（backtest_config 模式）──
            day_phase = None
            if cfg and ref_date_str:
                em = elapsed_months(date_str, ref_date_str)
                curr_month = int(em)
                for m in range(curr_month + 1):
                    if m not in injected_months:
                        inj = cfg.get_injection(m)
                        if inj > 0:
                            cash += inj
                            cumulative_injected += inj
                            cash_benchmark += inj
                            injected_months.add(m)

                can_trade = cfg.can_trade(em)
                if em <= cfg.observe_end_month:
                    day_phase = "observe"
                elif em <= 12:
                    day_phase = "deploy"
                else:
                    day_phase = "test"
            else:
                can_trade = True

            # 当天所有标的价格（用于算 NAV）
            day_prices: dict[str, float] = {}

            for code, info in day_data.items():
                close = info["close"]
                ma60 = info["ma60"]
                deviation = info["deviation"]
                day_prices[code] = close
                last_prices[code] = close  # 追踪最后已知价格
                lot = _get_lot_size(code)

                # 覆盖手数
                if cfg:
                    lot = cfg.get_lot_size(code, lot)

                # 跳过无锚点的早期数据
                if ma60 <= 0:
                    prev_deviations[code] = deviation
                    continue

                if not can_trade:
                    # 观察期/持仓期：仅更新偏离追踪，不评估规则
                    prev_deviations[code] = deviation
                    continue

                # ── 上下文（含可选指标列）──
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
                    "monthly_buy_limit": effective_buy_limit,
                    "monthly_sell_limit": effective_sell_limit,
                    "lot_size": lot,
                    "commission_rate": effective_commission,
                }
                # 注入指标值到上下文（规则表达式可引用 rsi, macd_hist 等）
                for key, val in info.items():
                    if key not in ctx:
                        ctx[key] = val

                engine = engines[code]
                for rule, amount in engine.evaluate_day(ctx):
                    if rule.type == "buy":
                        buy_amount = min(float(amount), cash)
                        remaining = max(0, effective_buy_limit - month_buy_used)
                        buy_amount = min(buy_amount, remaining)
                        if buy_amount >= close * lot:
                            available = buy_amount * (1 - effective_commission)
                            qty = int(available / close / lot) * lot
                            if qty > 0:
                                cost = qty * close
                                fee = cost * effective_commission
                                if cost + fee <= buy_amount:
                                    positions[code] += qty
                                    cash -= cost + fee
                                    cost_basis[code] += cost + fee
                                    month_buy_used += cost + fee
                                    trade_count += 1
                                    if day_phase:
                                        phase_trade_count[day_phase] += 1

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

                        remaining = max(0, effective_sell_limit - month_sell_used)
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

                        fee = sell_value * effective_commission
                        cash += sell_value - fee
                        sold_fraction = sell_qty / positions[code]
                        cost_basis[code] -= cost_basis[code] * sold_fraction
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
            nav = cash + position_value
            daily_navs.append(nav)
            nav_dates.append(date_str)
            monthly_buys[month_key] = month_buy_used
            monthly_sells[month_key] = month_sell_used

            # ── 季度持仓快照（每 63 交易日 ≈ 1 季度）──
            day_idx = len(daily_navs) - 1
            q_interval = 63
            if day_idx > 0 and day_idx % q_interval == 0:
                q_positions = []
                for c in active_codes:
                    if positions[c] > 0:
                        px = day_prices.get(c) or last_prices.get(c, 0)
                        avg_cost = (
                            cost_basis[c] / positions[c]
                            if positions[c] > 0 else 0.0
                        )
                        val = round(positions[c] * px, 2)
                        pnl = round(val - cost_basis[c], 2)
                        pnl_pct = round(
                            pnl / cost_basis[c] * 100
                            if cost_basis[c] > 0 else 0.0, 1
                        )
                        q_positions.append({
                            "code": c,
                            "shares": positions[c],
                            "cost": round(avg_cost, 2),
                            "price": round(px, 2),
                            "value": val,
                            "pnl": pnl,
                            "pnl_pct": pnl_pct,
                        })
                q_pos_pct = round(
                    (position_value / nav * 100) if nav > 0 else 0.0, 1
                )
                quarterly_holdings.append({
                    "quarter": len(quarterly_holdings) + 1,
                    "day": day_idx,
                    "cash": round(cash, 2),
                    "nav": round(nav, 2),
                    "pos_pct": q_pos_pct,
                    "positions": q_positions,
                })

            # ── 现金基准线（含无风险复利）──
            cash_benchmark *= (1.0 + daily_rf)
            daily_benchmark_navs.append(cash_benchmark)

            # ── 分阶段净值记录（3 阶段：观察/部署/验证）──
            if cfg and day_phase:
                phase_navs[day_phase].append(nav)
                phase_dates[day_phase].append(date_str)

        # ── 指标计算 ──
        def _calc_metrics(nav_list: list[float]) -> tuple[float, float, float]:
            """从净值序列计算 (total_return%, max_drawdown%, sharpe)。
            不足 2 个数据点返回 (None, None, None) — 调用方负责显示 "—"。
            """
            if len(nav_list) < 2:
                return None, None, None
            initial = nav_list[0]
            final = nav_list[-1]
            total_ret = (final - initial) / initial * 100 if initial > 0 else 0.0
            peak_val = nav_list[0]
            max_dd = 0.0
            for v in nav_list:
                if v > peak_val:
                    peak_val = v
                dd = (v - peak_val) / peak_val * 100
                if dd < max_dd:
                    max_dd = dd
            risk_free = RISK_FREE_A if self.group == "a_share" else RISK_FREE_NON_A
            daily_rets = []
            for i in range(1, len(nav_list)):
                if nav_list[i - 1] > 0:
                    daily_rets.append(
                        (nav_list[i] - nav_list[i - 1]) / nav_list[i - 1]
                    )
            sp = 0.0
            if len(daily_rets) > 5:
                excess = [r - risk_free / 252 for r in daily_rets]
                mean_ex = np.mean(excess)
                std_ex = np.std(excess, ddof=1)
                sp = (mean_ex / std_ex * np.sqrt(252)) if std_ex > 1e-10 else 0.0
            return round(total_ret, 2), round(max_dd, 2), round(sp, 4)

        if len(daily_navs) < 2:
            return PortfolioResult(
                name="", group=self.group,
                total_return=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
                expected_position=0.0, composition=stock_codes, trade_count=0,
                nav_series=daily_navs, nav_dates=nav_dates,
            )

        total_return, max_drawdown, sharpe = _calc_metrics(daily_navs)

        # ── 子区间指标（3 阶段：观察/部署/验证）──
        sub_periods: dict[str, SubPeriodMetrics] = {}
        phase_meta = {
            "observe": (0, cfg.observe_end_month if cfg else 6),
            "deploy": (cfg.observe_end_month if cfg else 6, 12),
            "test": (12, 24),
        }
        for phase_key, (start_m, end_m) in phase_meta.items():
            if cfg and phase_navs.get(phase_key) and daily_benchmark_navs:
                tr, td, ts = _calc_metrics(phase_navs[phase_key])
                n_phase = len(phase_navs[phase_key])
                # 取对应区间的 benchmark
                observe_len = len(phase_navs.get("observe", []))
                deploy_len = len(phase_navs.get("deploy", []))
                if phase_key == "observe":
                    offset = 0
                elif phase_key == "deploy":
                    offset = observe_len
                else:  # test
                    offset = observe_len + deploy_len
                bench_slice = daily_benchmark_navs[offset:offset + n_phase]
                if len(bench_slice) >= 2:
                    bench_initial = bench_slice[0]
                    bench_final = bench_slice[-1]
                    bench_ret = (
                        (bench_final - bench_initial) / bench_initial * 100
                        if bench_initial > 0 else 0.0
                    )
                else:
                    bench_ret = 0.0
                excess = tr - bench_ret
                sub_periods[phase_key] = SubPeriodMetrics(
                    label=phase_key,
                    start_month=start_m,
                    end_month=end_m,
                    total_return=tr,
                    max_drawdown=td,
                    sharpe_ratio=ts,
                    trade_count=phase_trade_count.get(phase_key, 0),
                    excess_return=excess,
                )

        position_value = sum(
            positions[c] * float(
                self.stocks_data[c].iloc[-1]["close"]
            ) for c in active_codes if positions[c] > 0
        )

        return PortfolioResult(
            name="", group=self.group,
            total_return=total_return,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe,
            expected_position=round(position_value, 2),
            composition=list(active_codes),
            trade_count=trade_count,
            nav_series=[round(v, 2) for v in daily_navs],
            nav_dates=nav_dates,
            stock_details=[],
            sub_periods=sub_periods,
            quarterly_holdings=quarterly_holdings,
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

    def __init__(self, config: dict, custom_rules: list | None = None,
                 signal_fn=None, engine_params=None):
        """Args:
        config: 系统配置
        custom_rules: 自定义规则列表（如来自优化器 YAML），优先于 config 中的规则
        signal_fn: SignalFn 引擎（规则为 __signal_fn__ 时用评分流水线回测）
        engine_params: SignalFn 参数（Params），从 YAML params 还原
        """
        self.config = config
        self.custom_rules = custom_rules
        self.signal_fn = signal_fn
        self.engine_params = engine_params
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

        # 加载自定义规则：构造参数优先 > config 配置
        custom_rules = self.custom_rules
        if custom_rules is None:
            config_rules = ps_config.get("rules", None)
            if config_rules:
                custom_rules = [Rule.from_dict(r) for r in config_rules]
                logger.info(f"投资组合使用配置规则: {len(custom_rules)} 条")

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

    def run_fixed(self, groups: list[str] | None = None) -> dict:
        """对 config 当前标的做确定性评估（不搜索，不选股）。

        日报/简报专用：标的池 = config.yaml 当前 stocks（按细分组分池），
        策略规则 = self.custom_rules（来自 YAML）。删/加标的立刻生效，
        不依赖 YAML 里的 _stocks 快照。表现数字以 YAML test_return 为准，
        本方法产出的 total_return 仅供 NAV 图展示。

        Args:
            groups: 要评估的细分组子集，如 ["a_share"] 或 ["hk","us"]；
                    None = 全部三组
        Returns:
            dict: {group: {"top1": PortfolioResult}}  group ∈ a_share/hk/us
        """
        stocks = self.config.get("stocks", [])
        if not stocks:
            logger.warning("配置中无股票")
            return {}

        target_groups = groups or ["a_share", "hk", "us"]
        ps_config = self.config.get("portfolio_strategy", {})
        lookback_days = ps_config.get("lookback_days", 730)

        custom_rules = self.custom_rules
        if custom_rules is None:
            config_rules = ps_config.get("rules", None)
            if config_rules:
                custom_rules = [Rule.from_dict(r) for r in config_rules]

        # 标的池 = config 当前 stocks，按细分组分池（只拉目标组）
        # 注意：不过滤 skip_search — 那只作用于搜参阶段；日报/验证期照常评估
        # （如上市不足搜参窗口的 ETF/REITs，验证期仍按策略规则交易）
        group_data_map: dict[str, dict[str, pd.DataFrame]] = {
            "a_share": {}, "hk": {}, "us": {},
        }
        for code in stocks:
            code_str = str(code)
            group = _detect_fine_group(code_str)
            if group not in target_groups:
                continue
            data = self.fetch_stock_data(code_str, lookback_days)
            if data.empty or "close" not in data.columns:
                continue
            # 日报评估门槛远低于搜参（60天即可算指标+告警），
            # 让新上市标的（如 REITs）也能参与验证期交易
            if len(data) < MIN_EVAL_DAYS:
                logger.info(f"{code_str} 数据不足 {len(data)}<{MIN_EVAL_DAYS}，跳过日报评估")
                continue
            group_data_map[group][code_str] = data

        # 评估器 group 参数（risk_free 二分）：a_share=2%, hk/us=非A 4.5%
        eval_group = {"a_share": "a_share", "hk": "non_a_share", "us": "non_a_share"}
        results: dict = {}
        for group_name in target_groups:
            group_data = group_data_map.get(group_name, {})
            selected = list(group_data.keys())
            if not selected:
                logger.info(f"{group_name} config 无可用标的，跳过")
                results[group_name] = {}
                continue

            evaluator = PortfolioEvaluator(
                group_data, eval_group[group_name], rules=custom_rules,
                signal_fn=self.signal_fn,
            )
            if self.engine_params is not None:
                evaluator._engine_params = self.engine_params
            result = evaluator.evaluate(selected)
            result.name = "top1"
            results[group_name] = {"top1": result}
            logger.info(
                f"{group_name} 固定评估: {len(selected)}只 "
                f"收益{result.total_return:.1f}% 回撤{result.max_drawdown:.1f}%"
            )

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
    benchmark_data: dict[str, pd.DataFrame] | None = None,
) -> dict[str, bytes] | None:
    """
    生成投资组合NAV走势图（每组一张独立PNG）

    画 Top1 搜参策略 (max_return) 的净值曲线 + 基准 ETF 曲线。

    Args:
        portfolio_results: PortfolioOptimizer.run() 返回的字典
        bollinger_window: 已弃用，保留签名向后兼容
        benchmark_data: {stock_code: DataFrame} 基准价格数据，需含 date/close 列
    """

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    from io import BytesIO

    # ── 设置中文字体 ──
    from ..utils.font_setup import setup_cjk_font
    setup_cjk_font()

    group_labels = {
        "a_share": "A股投资组合", "hk": "港股投资组合", "us": "美股投资组合",
        "non_a_share": "非A股投资组合",
    }
    output: dict[str, bytes] = {}

    for group_key in ("a_share", "hk", "us", "non_a_share"):
        gd = portfolio_results.get(group_key, {})
        result = gd.get("top1") or gd.get("max_return")
        if not result or not result.nav_series or len(result.nav_series) < 20:
            continue

        navs = result.nav_series
        dates = result.nav_dates

        # ── 创建画布 ──
        fig, ax = plt.subplots(figsize=(14, 5))
        title = f"{group_labels.get(group_key, group_key)} — Top1 搜参策略净值走势（近2年）"
        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_ylabel("组合净值 (基准100)", fontsize=11)
        ax.grid(True, alpha=0.25, linestyle="--")

        nav_arr = np.array(navs)
        # 归一化到起点100
        base = nav_arr[0] if nav_arr[0] > 0 else 1.0
        nav_arr = nav_arr / base * 100

        try:
            dt_arr = pd.to_datetime(dates)
        except Exception as e:
            logger.debug(f"图表日期转换失败: {e} (dates={len(dates)}条)")
            dt_arr = pd.date_range(end=pd.Timestamp.now(), periods=len(navs), freq="B")

        # 单条净值曲线
        ax.plot(dt_arr, nav_arr, color="#2e7d32", linewidth=2.5,
                alpha=0.9, label="Top1 策略", zorder=5)

        # 布林带（30日 SMA ± 2σ 半透明填充）
        if len(nav_arr) >= 30:
            window = 30
            sma = pd.Series(nav_arr).rolling(window=window, min_periods=1).mean()
            std = pd.Series(nav_arr).rolling(window=window, min_periods=1).std()
            upper = (sma + 2 * std).bfill().values
            lower = (sma - 2 * std).bfill().values
            ax.fill_between(dt_arr, lower, upper,
                            alpha=0.15, color="#2e7d32", linewidth=0, zorder=1)
            ax.plot(dt_arr, upper, color="#2e7d32",
                    linewidth=1.0, alpha=0.4, linestyle="--", zorder=2)
            ax.plot(dt_arr, lower, color="#2e7d32",
                    linewidth=1.0, alpha=0.4, linestyle="--", zorder=2)

        # 基准 ETF 曲线（港股/美股主基准都用 VOO，按需求不拆基准）
        benchmark_map = {
            "a_share": ["510300", "510880"],
            "hk": ["VOO", "BRK.B"],
            "us": ["VOO", "BRK.B"],
            "non_a_share": ["VOO", "BRK.B"],
        }
        bench_colors = ["#e74c3c", "#c0392b"]
        bench_styles = ["--", "-."]
        for bi, bcode in enumerate(benchmark_map.get(group_key, [])):
            if not benchmark_data or bcode not in benchmark_data:
                continue
            bdf = benchmark_data[bcode]
            if bdf is None or len(bdf) < 20:
                continue
            bdf = bdf.copy()
            bdf["date"] = pd.to_datetime(bdf["date"])
            bdf = bdf.sort_values("date").reset_index(drop=True)
            # 对齐到策略日期范围
            mask = (bdf["date"] >= dt_arr[0]) & (bdf["date"] <= dt_arr[-1])
            bdf = bdf[mask]
            if len(bdf) < 2:
                continue
            b_close = bdf["close"].to_numpy()
            b_base = b_close[0] if b_close[0] > 0 else 1.0
            b_norm = b_close / b_base * 100
            b_dates = bdf["date"].to_numpy()
            ax.plot(b_dates, b_norm, color=bench_colors[bi],
                    linewidth=1.5, alpha=0.7, linestyle=bench_styles[bi],
                    label=bcode, zorder=3)

        # X轴
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.tick_params(axis="x", labelsize=9, rotation=30)
        ax.tick_params(axis="y", labelsize=9)

        ax.relim()
        ax.autoscale_view(scalex=False, scaley=True)

        ax.legend(loc="upper left", fontsize=10, framealpha=0.85, edgecolor="#ccc")

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
