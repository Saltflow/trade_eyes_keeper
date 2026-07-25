"""
投资组合策略评估模块

日报/IM 的唯一回测入口。
evaluate_all_groups() → EvaluationReport → 邮件/IM 渲染段直接消费，
无回退链、无类包装、纯函数。

策略接口通过 SearchStrategy ABC（search_interface.py）接入，
不关心具体实现（percentile/builder/simplified）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 常量 ──
MIN_EVAL_DAYS = 60  # 日报评估最低数据天数
MIN_TRADING_DAYS = 400  # 搜参最低天数
RISK_FREE_A = 0.02
RISK_FREE_NON_A = 0.045


# ═══════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════


@dataclass
class EvaluationReport:
    """单组策略评估完整报告 — 邮件/IM 渲染段的唯一数据源"""

    group: str  # "a_share" / "hk" / "us"
    engine_name: str  # "percentile" / "builder" / "simplified"
    strategy_label: str  # "分位评分"
    timestamp: str  # "2026-07-25T19:00:00"

    # 策略表现
    total_return: float  # 策略总收益(%)
    excess_return: float  # 超额收益(%)
    max_drawdown: float  # 最大回撤(%, 负值)
    sharpe_ratio: float  # 夏普
    trade_count: int  # 交易笔数
    avg_cash_pct: float  # 平均现金仓位(%)

    # 基准比较
    benchmark_returns: dict[str, float] = field(default_factory=dict)

    # 组合构成
    composition: list[str] = field(default_factory=list)

    # 可视化
    nav_series: list[float] = field(default_factory=list)
    nav_dates: list[str] = field(default_factory=list)
    quarterly_holdings: list[dict] = field(default_factory=list)

    def to_cache_dict(self) -> dict:
        """序列化为 session._yaml_eval_cache 兼容格式"""
        return {
            "total_return": self.total_return,
            "excess_return": self.excess_return,
            "dd": self.max_drawdown,
            "sharpe": self.sharpe_ratio,
            "benchmark_returns": dict(self.benchmark_returns),
            "trades": self.trade_count,
            "composition": list(self.composition),
        }


# ═══════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════


def _detect_stock_group(stock_code: str) -> str:
    """A股(6位数字) vs 非A股"""
    code = str(stock_code).strip()
    return "a_share" if (code.isdigit() and len(code) == 6) else "non_a_share"


def _detect_fine_group(stock_code: str) -> str:
    """细分：a_share(6位) / hk(5位) / us(含字母)"""
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return "a_share"
    if code.isdigit() and len(code) == 5:
        return "hk"
    return "us"


def get_skip_search(config: dict) -> set[str]:
    return {str(c).strip() for c in (config.get("skip_search") or [])}


def get_skip_signals(config: dict) -> set[str]:
    return {str(c).strip() for c in (config.get("skip_signals") or [])}


def _get_lot_size(stock_code: str) -> int:
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return 100
    if code.isdigit() and len(code) == 5:
        return 100
    return 1


def _get_month_key(date_str: str) -> str:
    return date_str[:7]


def _eval_lookback_days() -> int:
    """读 optimizer_constraints.yaml 计算回看天数"""
    try:
        import yaml

        with open("config/optimizer_constraints.yaml", "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        wf = raw.get("walk_forward", {}) or {}
        months = int(wf.get("test_months", 9))
        return int(months * 30.4375)
    except Exception:
        return 274


# ═══════════════════════════════════════════════════
# 核心：组合评估
# ═══════════════════════════════════════════════════


def _evaluate_signal_fn(
    stocks_data: dict[str, pd.DataFrame],
    active_codes: list[str],
    strategy,  # SearchStrategy
    params,  # Params
    initial_capital: float,
    monthly_limit: float,
    commission_rate: float,
    lot_size: int,
    fx_rate: float = 1.0,
) -> tuple["PortfolioTrace", list[str], list[str]]:
    """用信号评分流水线评估一组标的（无类、纯函数）。

    Returns:
        (trace, dates, stock_codes)  — trace 来自 backtester.simulate_portfolio
    """
    from .backtester import simulate_portfolio
    from .search_interface import PortfolioTrace
    from src.data.technical_indicators import compute_all

    # 补齐技术指标
    try:
        computed = compute_all({c: stocks_data[c] for c in active_codes})
    except Exception as e:
        logger.warning(f"指标计算失败，仅用兜底列: {e}")
        computed = {}

    # 统一日期轴
    date_set: set[str] = set()
    per_code_df: dict[str, pd.DataFrame] = {}
    for code in active_codes:
        df = computed.get(code)
        if df is None:
            df = stocks_data[code].copy()
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
        empty_trace = PortfolioTrace(
            daily_values=np.zeros(1),
            daily_dates=dates,
            total_trades=0,
            avg_position_pct=0.0,
            max_drawdown_pct=0.0,
            sharpe_ratio=0.0,
            total_return_pct=0.0,
            final_position_pct=0.0,
            quarterly_holdings=[],
            composition=list(active_codes),
        )
        return empty_trace, dates, list(active_codes)

    date_idx = {d: i for i, d in enumerate(dates)}

    # 评分矩阵 (T, N)
    buy_scores = np.zeros((T, N), dtype=np.float64)
    sell_scores = np.zeros((T, N), dtype=np.float64)
    price = np.full((T, N), np.nan, dtype=np.float64)

    for j, code in enumerate(active_codes):
        df = per_code_df[code]
        b, s = strategy.score_timeseries(params, df)
        closes = df["close"].astype(float).values
        for k, d in enumerate(df["date"].tolist()):
            ti = date_idx.get(d)
            if ti is None:
                continue
            price[ti, j] = closes[k] * fx_rate
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

    exec_p = strategy.execution_params(params)
    trace = simulate_portfolio(
        buy_scores,
        sell_scores,
        price,
        float(initial_capital),
        float(exec_p.get("buy_threshold", 0.0)),
        float(exec_p.get("sell_threshold", 0.0)),
        float(exec_p.get("position_frac", 0.15)),
        lot_size,
        float(monthly_limit),
        float(commission_rate),
        dates=dates,
        stock_codes=list(active_codes),
    )

    return trace, dates, list(active_codes)


def evaluate_all_groups(
    config: dict,
    signal_fn,  # SearchStrategy instance
    params,  # Params instance
    benchmark_data: dict[str, pd.DataFrame] | None = None,
    target_groups: list[str] | None = None,
) -> dict[str, EvaluationReport]:
    """日报/IM 的唯一评估入口。

    读 config.stocks → 按 fine_group 分组 → 拉数据 → 回测 → EvaluationReport。

    Args:
        config: 系统配置
        signal_fn: SearchStrategy 实例（如 PercentileSearchStrategy）
        params: 策略参数
        benchmark_data: {code: DataFrame} 基准价格数据（510300/510880/VOO等）
        target_groups: 评估分组子集。None = 全部三组

    Returns:
        {group_key: EvaluationReport}
    """
    from ..data.data_source import DataSource

    stocks = config.get("stocks", [])
    if not stocks:
        return {}

    target_groups = target_groups or ["a_share", "hk", "us"]
    ps_config = config.get("portfolio_strategy", {})
    lookback_days = ps_config.get("lookback_days") or _eval_lookback_days()

    # 读取执行配置
    from .config import get_execution_config

    exec_cfg = get_execution_config()
    fx_map = exec_cfg.fx_rates
    lot_map = exec_cfg.lot_sizes

    data_source = DataSource(config)

    # 标的按细分组分池
    group_data_map: dict[str, dict[str, pd.DataFrame]] = {
        g: {} for g in target_groups
    }
    for code in stocks:
        code_str = str(code)
        group = _detect_fine_group(code_str)
        if group not in target_groups:
            continue
        try:
            df = data_source.fetch_stock_data(code_str, lookback_days)
        except Exception as e:
            logger.warning(f"获取 {code_str} 数据失败: {e}")
            continue
        if df is None or df.empty or "close" not in df.columns:
            continue
        if len(df) < MIN_EVAL_DAYS:
            continue
        group_data_map[group][code_str] = df

    engine_name = getattr(params, "_engine", "") or signal_fn.name
    engine_names = {"percentile": "分位评分", "builder": "条件构建", "simplified": "固定限额"}
    from datetime import datetime

    timestamp = datetime.now().isoformat()

    results: dict[str, EvaluationReport] = {}
    for group_name in target_groups:
        group_data = group_data_map.get(group_name, {})
        active_codes = list(group_data.keys())
        if not active_codes:
            continue

        fx = fx_map.get(group_name, 1.0)
        lot = lot_map.get(group_name, 100)
        initial_capital = float(exec_cfg.initial_capital)
        monthly_limit = float(exec_cfg.monthly_buy_limit)
        commission = float(exec_cfg.commission_rate)

        # 选风险利率
        risk_free = RISK_FREE_A if group_name == "a_share" else RISK_FREE_NON_A

        # 基准曲线（加权平均建立市场基准净值）
        bench_nav = _build_benchmark_nav(
            benchmark_data, group_name, active_codes, group_data
        )

        # 回测
        trace, dates, codes = _evaluate_signal_fn(
            group_data,
            active_codes,
            signal_fn,
            params,
            initial_capital,
            monthly_limit,
            commission,
            lot,
            fx,
        )

        # 超额收益 = 策略收益 - 基准收益
        benchmark_returns = _compute_benchmark_returns(
            trace.nav_series,
            bench_nav,
            group_data,
            group_name,
            risk_free,
            len(dates),
        )
        # 现金基准年化收益率用于超额
        n_days = max(len(dates), 1)
        rfr_annual = risk_free
        rfr_daily = (1 + rfr_annual) ** (1 / 252) - 1

        cash_baseline = initial_capital
        cash_navs = [initial_capital]
        for _ in range(1, n_days):
            cash_baseline *= 1 + rfr_daily
            cash_navs.append(cash_baseline)

        strat_ret = trace.total_return_pct
        # 策略年化 vs 现金年化
        if len(cash_navs) >= 2 and n_days > 0:
            cash_ret = (cash_navs[-1] / cash_navs[0] - 1) * 100
            excess = strat_ret - cash_ret
        else:
            excess = 0.0

        # 平均现金仓位
        qh = trace.quarterly_holdings or []
        cash_pcts = [(100 - q.get("pos_pct", 0)) for q in qh if q.get("nav", 0) > 0]
        avg_cash = sum(cash_pcts) / len(cash_pcts) if cash_pcts else 0.0

        report = EvaluationReport(
            group=group_name,
            engine_name=engine_name,
            strategy_label=engine_names.get(engine_name, engine_name),
            timestamp=timestamp,
            total_return=round(strat_ret, 2),
            excess_return=round(excess, 2),
            max_drawdown=round(trace.max_drawdown_pct, 2),
            sharpe_ratio=round(trace.sharpe_ratio, 4),
            trade_count=trace.total_trades,
            avg_cash_pct=round(avg_cash, 0),
            benchmark_returns=benchmark_returns,
            composition=list(codes),
            nav_series=list(trace.nav_series),
            nav_dates=list(trace.nav_dates),
            quarterly_holdings=list(qh),
        )
        results[group_name] = report
        logger.info(
            f"{group_name} 评估完成: 收益{strat_ret:.1f}% "
            f"超额{excess:.1f}% 回撤{trace.max_drawdown_pct:.1f}% "
            f"夏普{trace.sharpe_ratio:.2f} {trace.total_trades}笔"
        )

    return results


def _build_benchmark_nav(
    benchmark_data: dict[str, pd.DataFrame] | None,
    group_name: str,
    active_codes: list[str],
    group_data: dict[str, pd.DataFrame],
) -> np.ndarray | None:
    """构建基准净值曲线（等权组合）。"""
    if not benchmark_data:
        return None

    benchmark_map = {
        "a_share": ["510300", "510880"],
        "hk": ["VOO", "BRK.B"],
        "us": ["VOO", "BRK.B"],
    }
    bench_codes = benchmark_map.get(group_name, [])
    valid_bench: dict[str, pd.DataFrame] = {}
    for bc in bench_codes:
        bdf = benchmark_data.get(bc)
        if bdf is not None and len(bdf) >= 20:
            valid_bench[bc] = bdf

    if not valid_bench:
        return None

    # 取主标的首个日期作为基准起始点
    first_code = active_codes[0]
    first_df = group_data.get(first_code)
    if first_df is None:
        return None
    start_date = pd.to_datetime(first_df["date"].iloc[0])
    end_date = pd.to_datetime(first_df["date"].iloc[-1])

    # 等权合成基准净值：每只 ETF 归一化到 start=1，取平均
    all_navs = []
    for bdf in valid_bench.values():
        bdf = bdf.copy()
        bdf["date"] = pd.to_datetime(bdf["date"])
        bdf = bdf[(bdf["date"] >= start_date) & (bdf["date"] <= end_date)]
        if len(bdf) < 2:
            continue
        b_close = bdf["close"].values
        b_nav = b_close / b_close[0]
        all_navs.append(b_nav)

    if not all_navs:
        return None

    min_len = min(len(n) for n in all_navs)
    trimmed = [n[:min_len] for n in all_navs]
    return np.mean(trimmed, axis=0)


def _compute_benchmark_returns(
    strategy_navs: list[float],
    bench_nav: np.ndarray | None,
    group_data: dict[str, pd.DataFrame],
    group_name: str,
    risk_free: float,
    n_days: int,
) -> dict[str, float]:
    """计算各基准收益率（用于胜率展示）"""
    results: dict[str, float] = {}

    # 无风险收益
    rfr_daily = (1 + risk_free) ** (1 / 252) - 1
    if n_days > 0:
        results["risk_free"] = round(((1 + rfr_daily) ** n_days - 1) * 100, 2)

    # 基准 ETF 收益
    if bench_nav is not None and len(bench_nav) >= 2:
        results["基准(等权)"] = round(
            (bench_nav[-1] / bench_nav[0] - 1) * 100, 2
        )

    return results
