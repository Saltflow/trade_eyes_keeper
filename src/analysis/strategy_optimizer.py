"""
策略搜索器

使用贝叶斯优化自动搜索"满足回撤约束且最大化收益"的策略参数。
基于 YAML 模板生成规则，两阶段评估（训练 0-12 月 / 测试 12-24 月）。

两阶段设计:
  - 阶段 A (训练): 优化器仅见 0-12 月数据，最大化部署期收益
  - 阶段 B (测试): 所有候选策略在完整 0-24 月重跑，按外样本（12-24月）排名

用法:
    from src.analysis.strategy_optimizer import StrategyOptimizer
    opt = StrategyOptimizer(config, stocks, data_source)
    report = opt.run(max_drawdown_pct=-25, iterations=150)
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, Field

from .rule_engine import Rule
from .backtest_config import (
    BacktestConfig,
    make_training_config,
    make_default_optimizer_config,
)
from .portfolio_strategy import PortfolioEvaluator
from .indicator_library import compute_all
from .rule_engine import ExpressionEngine

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  条件构建器池
# 
#  每个构建器是一个 callable: (threshold: float, direction: str) → condition: str
#  threshold 为归一化值 [0,1]，构建器内部映射到真实阈值。
#  direction = "buy" | "sell"
# 
#  构建器同时提供 reset_when 辅助函数。
# ═══════════════════════════════════════════════════════════════════


def _clip(v, lo, hi):
    """将归一化值映射到 [lo, hi] 区间"""
    return lo + v * (hi - lo)


def _build_deviation_cross(t_norm: float, direction: str) -> str:
    """MA60 偏离穿越：价格从一侧穿越阈值"""
    if direction == "buy":
        t = _clip(t_norm, -0.30, -0.005)
        return (
            f"deviation <= {t:.4f} and prev_deviation is not None "
            f"and prev_deviation > {t:.4f}"
        )
    else:
        t = _clip(t_norm, 0.005, 0.30)
        return (
            f"deviation >= {t:.4f} and prev_deviation is not None "
            f"and prev_deviation < {t:.4f} and shares > 0"
        )


def _build_rsi_signal(t_norm: float, direction: str) -> str:
    """RSI 超买/超卖"""
    if direction == "buy":
        t = _clip(1.0 - t_norm, 10, 40)  # 低归一→低RSI
        return f"rsi < {t:.1f}"
    else:
        t = _clip(t_norm, 60, 90)
        return f"rsi > {t:.1f} and shares > 0"


def _build_bollinger_signal(t_norm: float, direction: str) -> str:
    """布林带 %B 极值"""
    if direction == "buy":
        t = _clip(1.0 - t_norm, 0.0, 0.35)
        return f"boll_pct_b < {t:.3f}"
    else:
        t = _clip(t_norm, 0.65, 1.0)
        return f"boll_pct_b > {t:.3f} and shares > 0"


def _build_volume_spike(t_norm: float, direction: str) -> str:
    """放量异动（仅买入）"""
    t = _clip(t_norm, 1.2, 4.0)
    if direction == "sell":
        return "shares > 0"  # 卖出不能用纯量条件，兜底
    return f"vol_ratio > {t:.2f}"


def _build_deviation_absolute(t_norm: float, direction: str) -> str:
    """MA60 绝对偏离（不要求穿越）"""
    if direction == "buy":
        t = _clip(t_norm, -0.40, 0.0)
        return f"deviation <= {t:.4f}"
    else:
        t = _clip(t_norm, 0.0, 0.50)
        return f"deviation >= {t:.4f} and shares > 0"


def _build_trend_follow(t_norm: float, direction: str) -> str:
    """趋势跟踪：ADX 确认 + 方向"""
    t = _clip(t_norm, 15, 40)  # ADX threshold
    if direction == "buy":
        return f"adx > {t:.1f} and macd_hist > 0"
    else:
        return f"adx > {t:.1f} and macd_hist < 0 and shares > 0"


CONDITION_BUILDERS: dict[str, dict] = {
    "deviation_cross": {
        "label": "MA偏离穿越",
        "description": "价格从一侧穿越MA60偏离阈值",
        "build": _build_deviation_cross,
        "reset": lambda t_norm, direction: (
            "deviation > 0 and prev_deviation is not None and prev_deviation <= 0"
            if direction == "buy"
            else "deviation < 0 and prev_deviation is not None and prev_deviation >= 0"
        ),
    },
    "rsi_signal": {
        "label": "RSI信号",
        "description": "RSI超卖/超买",
        "build": _build_rsi_signal,
        "reset": lambda t_norm, direction: (
            "rsi > 50" if direction == "buy" else "rsi < 50"
        ),
    },
    "bollinger_signal": {
        "label": "布林带极值",
        "description": "价格触碰布林带下轨/上轨",
        "build": _build_bollinger_signal,
        "reset": lambda t_norm, direction: (
            "boll_pct_b > 0.5" if direction == "buy" else "boll_pct_b < 0.5"
        ),
    },
    "volume_spike": {
        "label": "放量异动",
        "description": "成交量放大（优于均值）",
        "build": _build_volume_spike,
        "reset": lambda t_norm, direction: "vol_ratio < 1.0",
    },
    "deviation_absolute": {
        "label": "MA绝对偏离",
        "description": "MA60偏离达绝对阈值（不要求穿越）",
        "build": _build_deviation_absolute,
        "reset": lambda t_norm, direction: (
            "deviation > 0" if direction == "buy" else "deviation < 0"
        ),
    },
    "trend_follow": {
        "label": "趋势跟踪",
        "description": "ADX趋势强度 + MACD方向确认",
        "build": _build_trend_follow,
        "reset": lambda t_norm, direction: "adx < 15",
    },
    "none": {
        "label": "禁用",
        "description": "该规则不触发",
        "build": lambda t, d: "False",
        "reset": lambda t, d: "True",
    },
}

# 买入/卖出各自可用的构建器
BUY_BUILDERS = [
    "deviation_cross", "rsi_signal", "bollinger_signal",
    "volume_spike", "deviation_absolute", "trend_follow", "none",
]
SELL_BUILDERS = [
    "deviation_cross", "rsi_signal", "bollinger_signal",
    "deviation_absolute", "trend_follow", "none",
]


def build_condition(
    builder_name: str,
    t_norm: float,
    direction: str,
) -> tuple[str, str]:
    """返回 (condition_str, reset_when_str)"""
    b = CONDITION_BUILDERS.get(builder_name, CONDITION_BUILDERS["none"])
    cond = b["build"](t_norm, direction)
    reset = b["reset"](t_norm, direction)
    return cond, reset


class StrategyTrial(BaseModel):
    """单次策略试验记录"""

    params: dict[str, str | float]  # 参数摘要（构建器名、阈值等）
    rules: list[Rule]  # 生成的规则列表
    train_return: float  # 训练期（0-12月）总收益率(%)
    train_drawdown: float  # 训练期最大回撤(%)
    test_return: float  # 测试期（12-24月）总收益率(%, vs primary benchmark)
    test_drawdown: float  # 测试期最大回撤(%)
    sharpe: float  # 全期夏普比
    trade_count: int  # 总交易次数
    sub_periods: dict | None = None  # PortfolioResult.sub_periods 原始数据
    benchmark_returns: dict[str, float] = Field(default_factory=dict)  # 全部基准收益
    strategy_return: float = 0.0  # 策略绝对收益
    final_position_pct: float = 0.0  # 期末仓位率
    final_holdings: list[dict] = Field(default_factory=list)  # [{code, shares, price, value, pct, cost}]
    final_cash: float = 0.0  # 期末现金
    total_nav: float = 0.0  # 期末总资产
    quarterly_holdings: list[dict] = Field(default_factory=list)
    strategy_description: str = ""  # 人话策略描述  # [{quarter, day, cash, nav, positions: [{code,shares,cost,price,value,pnl}]}]

    @property
    def fitness(self) -> float:
        """训练期适应度（用于 Bayessian 优化目标）"""
        return self.train_return


class OptimizationReport(BaseModel):
    """优化报告"""

    report_id: str
    group: str
    timestamp: str
    iterations: int
    n_random_starts: int = 20  # 贝叶斯优化随机初始点数
    top_strategies: list[StrategyTrial] = Field(default_factory=list)
    convergence: list[float] = Field(default_factory=list)
    all_train_returns: list[float] = Field(default_factory=list)
    elapsed_seconds: float = 0.0
    best_params: dict = Field(default_factory=dict)
    benchmarks: dict[str, float] = Field(default_factory=dict)  # 基准名称 → 测试超额收益


class StrategyOptimizer:
    """
    策略搜索器

    读取 optimizer.yaml 中的策略模板，用贝叶斯优化搜索最优参数。
    """

    def __init__(
        self,
        stocks_data: dict[str, pd.DataFrame],
        group: str,
        template_path: str | Path = "config/optimizer.yaml",
    ):
        """
        Args:
            stocks_data: {stock_code: DataFrame} 含 2 年 OHLCV 数据
            group: "a_share" 或 "non_a_share"
            template_path: optimizer.yaml 路径
        """
        self.stocks_data = stocks_data
        self.group = group
        self.template_path = Path(template_path)

        # 加载模板
        with open(self.template_path, encoding="utf-8") as f:
            self.opt_config = yaml.safe_load(f)

        self.template = self.opt_config.get("strategy_template", {})
        self.constraints = self.opt_config.get("constraints", {})
        self.output_cfg = self.opt_config.get("output", {})

        # 预计算指标
        self.indicators: dict[str, pd.DataFrame] = {}
        # 基准 ETF 数据（可选）
        self.benchmark_data: dict[str, pd.DataFrame] = {}
        # 预筛选结果（run() 时填充）
        self._filtered_buy_builders: list[str] | None = None
        self._filtered_sell_builders: list[str] | None = None

    def set_benchmark_data(self, data: dict[str, "pd.DataFrame"]):
        """设置基准 ETF 数据（510300 沪深300 / 510880 红利）"""
        self.benchmark_data = data

    def _make_benchmark_rules(self) -> list[Rule]:
        """买就满仓、永不卖出"""
        return [
            Rule(
                id="bn_buy", label="满仓买入", type="buy", priority=1,
                condition="True", budget_pool="buy",
                action_amount="cash * 0.95",
                reset_when="False",
            ),
            Rule(
                id="bn_sell", label="禁止卖出", type="sell", priority=2,
                condition="False", budget_pool="sell",
                action_fraction=0.0, reset_when="True",
            ),
        ]

    def _compute_benchmarks(
        self, stock_codes: list[str], test_config: "BacktestConfig",
    ) -> dict[str, float]:
        """对各基准 ETF 运行满仓持有策略，返回 {名称: 测试超额收益%}"""
        if not self.benchmark_data:
            return {}
        bench_rules = self._make_benchmark_rules()
        results: dict[str, float] = {}
        for name, df in self.benchmark_data.items():
            code = list(df.columns)[0] if "date" not in df.columns else name
            single_data = {code: df}
            evalr = PortfolioEvaluator(single_data, self.group)
            evalr.rules = bench_rules
            # 合并指标（和主策略共用同一套）
            ind_for_bench = {code: self.indicators.get(code, df)} if self.indicators else None
            try:
                result = evalr.evaluate(
                    [code],
                    backtest_config=test_config,
                    indicators_data=ind_for_bench,
                )
                test_sp = result.sub_periods.get("test")
                results[name] = round(test_sp.excess_return if test_sp else 0.0, 2)
            except Exception as e:
                logger.warning(f"基准 {name} 计算失败: {e}")
                results[name] = 0.0
        logger.info("[Benchmark] %s", results)
        return results

    # ── 观测期预筛选 ──

    def _prefilter_builders(
        self,
        stock_codes: list[str],
        observe_days: int = 120,
        min_signals: int = 1,
    ) -> tuple[list[str], list[str]]:
        """
        用 0-6 月观测期数据预筛选构建器：淘汰在这批标的上从未触发过信号的。

        逻辑：遍历每只股票前 observe_days 天，对每个构建器生成条件并计数。
        信号总数 < min_signals 的构建器 + 'none' → 淘汰出搜索空间。

        Returns:
            (filtered_buy_builders, filtered_sell_builders)
        """
        active_codes = [c for c in stock_codes if c in self.stocks_data]
        if not active_codes or not self.indicators:
            return list(BUY_BUILDERS), list(SELL_BUILDERS)

        expr_engine = ExpressionEngine()

        # 对每个构建器计数
        def _count_signals(builder_name: str, direction: str) -> int:
            total = 0
            norm_test = 0.5  # 中等阈值测试
            cond_str, _ = build_condition(builder_name, norm_test, direction)
            if cond_str == "False":
                return 0

            for code in active_codes:
                ind_df = self.indicators.get(code)
                if ind_df is None or len(ind_df) < 2:
                    continue
                df = ind_df.iloc[:observe_days].reset_index(drop=True)

                # 计算 deviation/ma60（如果指标库里没有）
                if "deviation" not in df.columns and "close" in df.columns:
                    df["ma60"] = df["close"].rolling(60, min_periods=1).mean()
                    df["deviation"] = (df["close"] - df["ma60"]) / df["ma60"]

                prev_dev = None
                for i in range(1, len(df)):
                    row = df.iloc[i]
                    prev_row = df.iloc[i - 1]
                    ctx = {
                        "close": float(row.get("close", 0)),
                        "deviation": float(row.get("deviation", 0) or 0),
                        "prev_deviation": prev_dev,
                        "rsi": float(row.get("rsi", 50) if pd.notna(row.get("rsi")) else 50),
                        "vol_ratio": float(row.get("vol_ratio", 1.0) if pd.notna(row.get("vol_ratio")) else 1.0),
                        "boll_pct_b": float(row.get("boll_pct_b", 0.5) if pd.notna(row.get("boll_pct_b")) else 0.5),
                        "macd_hist": float(row.get("macd_hist", 0) if pd.notna(row.get("macd_hist")) else 0),
                        "adx": float(row.get("adx", 20) if pd.notna(row.get("adx")) else 20),
                        "shares": 0,
                        "ma60": float(row.get("ma60", 0) or 0),
                    }
                    try:
                        if expr_engine.evaluate(cond_str, ctx):
                            total += 1
                    except Exception as e:
                        logger.debug(f"条件求值异常 (非致命): {cond_str[:50]} | {e}")
                    prev_dev = ctx["deviation"]

            return total

        # 筛选买入构建器
        keep_buy = []
        for b in BUY_BUILDERS:
            if b == "none":
                keep_buy.append(b)  # 'none' 永远保留
                continue
            n = _count_signals(b, "buy")
            logger.debug("[PreFilter] 买 %s: %d 次信号", b, n)
            if n >= min_signals:
                keep_buy.append(b)
            else:
                logger.info("[PreFilter] 淘汰买入构建器 %s (0-6月无信号)", b)

        # 筛选卖出构建器
        keep_sell = []
        for b in SELL_BUILDERS:
            if b == "none":
                keep_sell.append(b)
                continue
            n = _count_signals(b, "sell")
            logger.debug("[PreFilter] 卖 %s: %d 次信号", b, n)
            if n >= min_signals:
                keep_sell.append(b)
            else:
                logger.info("[PreFilter] 淘汰卖出构建器 %s (0-6月无信号)", b)

        self._filtered_buy_builders = keep_buy
        self._filtered_sell_builders = keep_sell

        logger.info(
            "[PreFilter] 买入 %d→%d, 卖出 %d→%d",
            len(BUY_BUILDERS), len(keep_buy),
            len(SELL_BUILDERS), len(keep_sell),
        )
        return keep_buy, keep_sell

    # ── 构建器渲染 ──

    def _get_rule_specs(self) -> list[dict]:
        """返回有序的规则规格列表 [{id, type, priority, builders, budget_pool}]

        若预筛选已完成，用过滤后的构建器列表替代 YAML 配置。
        """
        rules_config = self.opt_config.get("strategy_template", {}).get("rules", {})
        specs = []
        for rule_id, cfg in sorted(rules_config.items(),
                                   key=lambda x: x[1].get("priority", 99)):
            builders = cfg.get("builders", [])
            # 预筛选覆盖
            if self._filtered_buy_builders and cfg["type"] == "buy":
                builders = self._filtered_buy_builders
            elif self._filtered_sell_builders and cfg["type"] == "sell":
                builders = self._filtered_sell_builders
            specs.append({
                "id": rule_id,
                "type": cfg["type"],
                "priority": cfg.get("priority", 1),
                "label": cfg.get("label", rule_id),
                "builders": builders,
                "budget_pool": cfg.get("budget_pool", cfg["type"]),
            })
        return specs

    def _build_dimensions(self, stock_codes: list[str] | None = None) -> list:
        """构建 skopt 搜索空间（含规则参数 + 股票开关）"""
        from skopt.space import Real, Integer

        sp = self.opt_config.get("search_params", {})
        t_range = sp.get("threshold_range", [0.0, 1.0])
        buy_f = sp.get("buy_frac_range", [0.02, 0.30])
        sell_f = sp.get("sell_frac_range", [0.10, 0.50])

        dims = []
        rule_specs = self._get_rule_specs()
        self._num_rule_dims = 0
        for spec in rule_specs:
            n_builders = len(spec["builders"])
            if n_builders == 0:
                continue
            # 1) 构建器选择（n=1 时退化为 [0,1] 避免 skopt 报错，实际只取 0）
            effective_n = max(n_builders, 2)
            dims.append(Integer(0, effective_n - 1,
                                name=f"{spec['id']}_builder"))
            # 2) 归一化阈值
            dims.append(Real(t_range[0], t_range[1],
                             name=f"{spec['id']}_threshold"))
            # 3) 仓位比例
            fr = buy_f if spec["type"] == "buy" else sell_f
            dims.append(Real(fr[0], fr[1],
                             name=f"{spec['id']}_frac"))
            self._num_rule_dims += 3

        # 股票开关
        if stock_codes:
            for code in stock_codes:
                dims.append(Integer(0, 1, name=f"include_{code}"))

        return dims

    def _params_to_rules(self, param_vec: list[float],
                         stock_codes: list[str] | None = None,
                         ) -> tuple[list[Rule], list[str]]:
        """将参数向量转换为 Rule 列表 + 纳入的股票代码

        Returns:
            (rules, included_stocks) — rules 按 priority 排序
        """
        rule_specs = self._get_rule_specs()
        n_rule_dims = self._num_rule_dims
        n_per_rule = 3  # builder, threshold, frac

        rules = []
        p_idx = 0
        for spec in rule_specs:
            if p_idx + n_per_rule > len(param_vec):
                break
            builders = spec["builders"]
            if not builders:
                p_idx += n_per_rule
                continue

            builder_idx = int(round(param_vec[p_idx]))
            t_norm = max(0.0, min(1.0, param_vec[p_idx + 1]))
            frac = round(param_vec[p_idx + 2], 4)
            p_idx += n_per_rule

            builder_idx = min(builder_idx, len(builders) - 1)
            builder_name = builders[builder_idx]
            direction = spec["type"]  # "buy" or "sell"

            condition, reset_when = build_condition(builder_name, t_norm, direction)

            rule = Rule(
                id=spec["id"],
                label=spec["label"],
                type=spec["type"],
                priority=spec["priority"],
                condition=condition,
                budget_pool=spec["budget_pool"],
                action_amount=(
                    f"cash * {frac}" if spec["type"] == "buy" else None
                ),
                action_fraction=frac if spec["type"] == "sell" else None,
                action_min=None,
                action_max=None,
                reset_when=reset_when,
            )
            rules.append(rule)

        # 股票开关
        included = list(stock_codes or [])
        if stock_codes and n_rule_dims < len(param_vec):
            stock_flags = param_vec[n_rule_dims:]
            included = [
                code for code, flag in zip(stock_codes, stock_flags)
                if flag >= 0.5
            ]
            if not included:
                included = stock_codes[:1]  # 至少保留一只

        return rules, included

    # ── 优化主流程 ──

    def run(
        self,
        stock_codes: list[str],
        max_drawdown_pct: float | None = None,
        iterations: int | None = None,
        random_starts: int | None = None,
        report_id: str = "",
    ) -> OptimizationReport:
        """执行两阶段策略搜索。"""
        from skopt import gp_minimize

        dd_limit = max_drawdown_pct or self.constraints.get("max_drawdown_pct", -25)
        n_calls = iterations or self.constraints.get("iterations", 150)
        n_random = random_starts or self.constraints.get("random_starts", 20)
        penalty = self.constraints.get("drawdown_penalty_weight", 2.0)

        # 1. 预计算指标
        if not self.indicators:
            logger.info("[IndicatorLibrary] 计算 6 类指标...")
            t0 = time.time()
            self.indicators = compute_all(self.stocks_data)
            logger.info("[IndicatorLibrary] 完成，%d 只股票，耗时 %.1fs",
                        len(self.indicators), time.time() - t0)

        # 1b. 观测期预筛选：淘汰无信号的构建器
        self._prefilter_builders(stock_codes, observe_days=120)

        # 2. 构建搜索空间
        dimensions = self._build_dimensions(stock_codes)
        rule_specs = self._get_rule_specs()
        logger.info("[StrategyOptimizer] 搜索空间: %d 维 (%d 规则 + %d 股票开关) | 迭代 %d 轮",
                    len(dimensions), self._num_rule_dims,
                    len(dimensions) - self._num_rule_dims, n_calls)

        # 3. 评估器
        evaluator = PortfolioEvaluator(self.stocks_data, self.group)
        train_config = make_training_config()

        # 4. 记录所有试验
        all_trials: list[tuple] = []  # (param_vec, rules, included, train_excess, dd)
        best_fitness = -float("inf")

        def objective(param_vec: list[float]) -> float:
            nonlocal best_fitness

            rules, included = self._params_to_rules(param_vec, stock_codes)
            evaluator.rules = rules
            evaluator.stocks_data = self.stocks_data

            result = evaluator.evaluate(
                included,
                backtest_config=train_config,
                indicators_data=self.indicators,
            )

            # 适应度 = 部署期超额收益
            deploy_sp = result.sub_periods.get("deploy")
            excess = deploy_sp.excess_return if deploy_sp else 0.0
            dd = deploy_sp.max_drawdown if deploy_sp else 0.0

            fitness = excess
            if dd < dd_limit:
                fitness += (dd - dd_limit) * penalty

            all_trials.append((list(param_vec), rules, included, excess, dd))

            if fitness > best_fitness:
                best_fitness = fitness
                logger.info(
                    "[BayesOpt] 新最优 | 部署超额 %.1f%% | 回撤 %.1f%% | 入选 %d 只",
                    excess, dd, len(included),
                )

            return -fitness  # gp_minimize 最小化

        # 5. 贝叶斯优化
        t0 = time.time()
        opt_result = gp_minimize(
            objective, dimensions, n_calls=n_calls,
            n_random_starts=n_random, verbose=False,
        )
        elapsed = time.time() - t0
        logger.info("[BayesOpt] 完成。%d 次评估，耗时 %.0fs", n_calls, elapsed)

        # 6. 阶段 B: 最终评估（0-24 月）
        test_config = make_default_optimizer_config()
        evaluator_test = PortfolioEvaluator(self.stocks_data, self.group)
        trials: list[StrategyTrial] = []

        logger.info("[FinalEval] 在全时间线上重跑 %d 个候选策略...", len(all_trials))
        for param_vec, rules, included, train_excess, train_dd in all_trials:
            evaluator_test.rules = rules
            evaluator_test.stocks_data = self.stocks_data
            result = evaluator_test.evaluate(
                included,
                backtest_config=test_config,
                indicators_data=self.indicators,
            )

            test_sp = result.sub_periods.get("test")
            test_ret = test_sp.excess_return if test_sp else 0.0
            test_dd = test_sp.max_drawdown if test_sp else 0.0

            # 收集参数摘要（构建器名 + 阈值 + 比例）
            params_short: dict[str, str] = {}
            p_idx = 0
            for spec in rule_specs:
                if p_idx + 3 > len(param_vec):
                    break
                b_idx = int(round(param_vec[p_idx]))
                builders = spec.get("builders", [])
                # 单 builder 时 skopt 维度 [0,1] 可能给 1，clamp 到 0
                b_idx = min(b_idx, len(builders) - 1) if builders else 0
                b_name = builders[b_idx] if builders else CONDITION_BUILDERS["none"]["label"]
                params_short[f"{spec['id']}_signal"] = b_name
                params_short[f"{spec['id']}_t"] = f"{param_vec[p_idx+1]:.3f}"
                params_short[f"{spec['id']}_frac"] = f"{param_vec[p_idx+2]:.3f}"
                p_idx += 3

            params_short["_stocks"] = ",".join(included[:5])
            if len(included) > 5:
                params_short["_stocks"] += f" +{len(included)-5}"

            trials.append(StrategyTrial(
                params=params_short,
                rules=rules,
                train_return=train_excess,
                train_drawdown=train_dd,
                test_return=test_ret,
                test_drawdown=test_dd,
                sharpe=result.sharpe_ratio,
                trade_count=result.trade_count,
                sub_periods=result.sub_periods,
            ))

        # 按测试期超额收益排序
        trials.sort(key=lambda t: t.test_return, reverse=True)
        top_n = self.output_cfg.get("top_n", 10)
        top_trials = trials[:top_n]

        # 收敛曲线
        convergence = [-opt_result.func_vals[:i].min()
                       for i in range(1, len(opt_result.func_vals) + 1)]
        all_excess = [t[3] for t in all_trials]  # deploy excess

        report = OptimizationReport(
            report_id=report_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
            group=self.group,
            timestamp=datetime.now().isoformat(),
            iterations=n_calls,
            n_random_starts=n_random,
            top_strategies=top_trials,
            convergence=convergence,
            all_train_returns=all_excess,
            elapsed_seconds=elapsed,
            best_params=top_trials[0].params if top_trials else {},
        )

        # 计算基准（沪深300、红利ETF 满仓持有）
        report.benchmarks = self._compute_benchmarks(stock_codes, test_config)

        # 生成收敛图
        save_dir = Path(self.output_cfg.get("save_dir", "data/optimizer"))
        save_dir.mkdir(parents=True, exist_ok=True)
        plot_path = save_dir / f"{report.report_id}_{report.group}_convergence.png"
        self._plot_convergence(report, plot_path)

        # 生成 HTML 报告
        self.generate_html_report(report, save_dir)

        return report

    def _plot_convergence(
        self,
        report: "OptimizationReport",
        output_path: Path,
    ) -> None:
        """
        生成 2×1 收敛诊断图，保存为 PNG。

        图 1 (上): 贝叶斯收敛曲线
          - 实线: 每轮最优适应度
          - 散点: 所有评估点的训练收益（透明）
          - 竖虚线: 随机采样 → 贝叶斯采样分界

        图 2 (下): 最优策略 3 阶段柱状图
          - 观察期 (0-6月): 收益率 / 回撤 / 交易次数
          - 部署期 (6-12月): 收益率 / 回撤 / 交易次数
          - 验证期 (12-24月): 收益率 / 回撤 / 交易次数
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker

        # ── CJK 字体设置 ──
        from ..utils.font_setup import setup_cjk_font

        setup_cjk_font()

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))
        fig.suptitle(
            f"策略搜索报告 — {report.group} — {report.report_id}",
            fontsize=14, fontweight="bold",
        )

        # ══════ 图 1: 贝叶斯收敛 ══════
        if report.all_train_returns:
            n = len(report.all_train_returns)
            x = list(range(1, n + 1))

            # 散点: 所有评估点
            ax1.scatter(
                x, report.all_train_returns,
                c="steelblue", alpha=0.3, s=18, edgecolors="none",
                label="每次评估",
            )

            # 最优适应度线
            if report.convergence and len(report.convergence) == n:
                conv = report.convergence
                ax1.plot(x, conv, color="darkred", linewidth=2,
                         label="最优适应度")

            # 随机/贝叶斯分界
            rs = report.n_random_starts
            if rs and 0 < rs < n:
                ax1.axvline(x=rs + 0.5, color="gray", linestyle="--",
                            alpha=0.7, linewidth=1)
                ax1.text(rs + 1, ax1.get_ylim()[1] * 0.95,
                         "← 随机采样", fontsize=8, color="gray")
                ax1.text(rs + 1, ax1.get_ylim()[1] * 0.88,
                         "贝叶斯优化 →", fontsize=8, color="gray")

            ax1.set_ylabel("部署期超额收益 (%)", fontsize=11)
            ax1.set_xlabel("迭代轮次", fontsize=11)
            ax1.legend(loc="lower right", fontsize=9)
            ax1.grid(True, alpha=0.3)
            ax1.set_title("图 1: 贝叶斯收敛曲线 (训练期 0-12 月, 超额收益)", fontsize=12)

        # ══════ 图 2: 最优策略 3 阶段分拆 ══════
        if report.top_strategies:
            best = report.top_strategies[0]
            sp = best.sub_periods or {}

            phase_labels = {"observe": "观察期\n0-6月", "deploy": "部署期\n6-12月",
                            "test": "验证期\n12-24月"}
            phase_colors = {"observe": "#bbbbbb", "deploy": "#4c72b0",
                            "test": "#55a868"}

            phases = ["observe", "deploy", "test"]
            returns = []
            drawdowns = []
            trades = []
            for p in phases:
                m = sp.get(p)
                returns.append(m.excess_return if m else 0)
                drawdowns.append(m.max_drawdown if m else 0)
                trades.append(m.trade_count if m else 0)

            x_pos = range(len(phases))
            bars = ax2.bar(x_pos, returns, color=[phase_colors[p] for p in phases],
                           edgecolor="#333333", linewidth=0.8, width=0.55)

            # 柱顶标注收益率 + 交易次数
            for i, (ret, dd, t) in enumerate(zip(returns, drawdowns, trades)):
                sign = "+" if ret >= 0 else ""
                label_text = f"{sign}{ret:.1f}%"
                if t > 0:
                    label_text += f"  ({t}笔交易)"
                va = "bottom" if ret >= 0 else "top"
                y_pos = ret + (1 if ret >= 0 else -1)
                ax2.text(i, y_pos, label_text, ha="center", va=va,
                         fontsize=9, fontweight="bold")
                # 标注最大回撤
                if dd < 0:
                    ax2.text(i, ret + (2 if ret >= 0 else -2),
                             f"回撤 {dd:.1f}%",
                             ha="center", va="top" if ret >= 0 else "bottom",
                             fontsize=8, color="#c44e52")

            ax2.set_xticks(list(x_pos))
            ax2.set_xticklabels([phase_labels[p] for p in phases], fontsize=10)
            ax2.set_ylabel("超额收益 (%)", fontsize=11)
            ax2.set_title("图 2: 最优策略 3 阶段超额收益 (排名 #1)", fontsize=12)

            # 零线
            ax2.axhline(y=0, color="black", linewidth=0.8, alpha=0.5)
            ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter("%+.0f%%"))
            ax2.grid(axis="y", alpha=0.3)

            # 保证 y 轴范围至少 ±5%（防止全零时坐标轴坍塌）
            y_vals = [r for r in returns] + [d for d in drawdowns]
            y_min, y_max = min(y_vals), max(y_vals)
            if y_max - y_min < 5:
                mid = (y_max + y_min) / 2
                y_min = min(y_min, mid - 2.5)
                y_max = max(y_max, mid + 2.5)
            ax2.set_ylim(y_min - 1, y_max + 1)

            # 基准线（水平虚线，标注在验证期柱子右侧）
            bench_colors = {"沪深300": "#e8a838", "红利ETF": "#c44e52"}
            for i, (name, val) in enumerate(report.benchmarks.items()):
                color = bench_colors.get(name, "#ffffff")
                ax2.axhline(y=val, color=color, linestyle="--", linewidth=1,
                            alpha=0.8)
                ax2.text(2.35, val, f" {name} {val:+.1f}%",
                         fontsize=8, color=color, va="center")

        fig.tight_layout(rect=[0, 0, 1, 0.95])
        fig.savefig(str(output_path), dpi=150, bbox_inches="tight",
                    facecolor="white")
        plt.close(fig)
        logger.info("[ConvergencePlot] 已保存到 %s", output_path)

    def print_report(self, report: OptimizationReport) -> str:
        """生成可打印的报告文本"""
        lines = []
        lines.append("=" * 70)
        lines.append(f"  策略搜索结果 — {report.group} — {report.report_id}")
        lines.append("=" * 70)
        lines.append(
            f"  迭代: {report.iterations}  |  "
            f"耗时: {report.elapsed_seconds:.0f}s  |  "
            f"候选: {len(report.top_strategies)} 个"
        )
        lines.append("")
        lines.append("  注: 训练/测试收益均为超额收益(已扣除现金注资贡献)")
        lines.append("")
        lines.append(
            f"  {'排名':<4} {'部署超额':>9} {'测试超额':>9} "
            f"{'回撤':>8} {'夏普':>7} {'交易':>5}  {'入选'} "
        )
        lines.append("  " + "-" * 70)

        for i, t in enumerate(report.top_strategies, 1):
            stocks_str = t.params.get("_stocks", "?")
            lines.append(
                f"  {i:<4} {t.train_return:>+8.1f}% {t.test_return:>+8.1f}% "
                f"{t.test_drawdown:>+7.1f}% {t.sharpe:>7.3f} {t.trade_count:>5d}  "
                f"{stocks_str}"
            )

        lines.append("")
        if report.top_strategies:
            best = report.top_strategies[0]
            lines.append("  ★ 最优策略信号 & 仓位:")
            for spec in self._get_rule_specs():
                sig_key = f"{spec['id']}_signal"
                t_key = f"{spec['id']}_t"
                f_key = f"{spec['id']}_frac"
                sig = best.params.get(sig_key, "?")
                t_val = best.params.get(t_key, "?")
                f_val = best.params.get(f_key, "?")
                lines.append(f"      [{spec['id']}] {sig}  |  t={t_val}  |  "
                             f"{'买入' if spec['type']=='buy' else '卖出'}{f_val}")
            lines.append("")
            lines.append("  ★ 最优策略规则详情:")
            for rule in best.rules:
                lines.append(f"      [{rule.id}] {rule.label}")
                lines.append(f"        {rule.condition}")
                act = rule.action_amount or f"fraction={rule.action_fraction}"
                lines.append(f"        动作: {act}")
                lines.append(f"        重置: {rule.reset_when}")
                lines.append("")

        # 外样本 vs 内样本 对比
        train_ret = [t.train_return for t in report.top_strategies]
        test_ret = [t.test_return for t in report.top_strategies]
        if train_ret and test_ret:
            lines.append("  ★ 过拟合检测:")
            if len(set(test_ret)) < 2:
                lines.append("      测试期收益率一致（可能无交易），跳过相关性计算")
            else:
                corr = np.corrcoef(train_ret, test_ret)[0, 1] if len(train_ret) > 2 else 0
                if np.isnan(corr) or np.isinf(corr):
                    corr = 0.0
                lines.append(f"      训练-测试收益相关性: {corr:+.3f}")
                if corr < 0.2:
                    lines.append("      ⚠ 训练与测试相关性极低，策略可能过拟合")
                elif corr > 0.7:
                    lines.append("      ✓ 训练与测试高度相关，策略泛化性好")
            lines.append("")

        # 基准对比
        if report.benchmarks:
            lines.append("  ★ 基准对比 (买入持有不动):")
            for name, val in report.benchmarks.items():
                marker = "✓ 击败" if (report.top_strategies and 
                    report.top_strategies[0].test_return > val) else "✗ 不及"
                lines.append(f"      {name}: 测试超额 {val:+.2f}%  {marker}")
            lines.append("")

        lines.append("=" * 70)
        return "\n".join(lines)

    def save_results(
        self,
        report: OptimizationReport,
        save_dir: str | Path = "data/optimizer",
    ) -> Path:
        """保存最优策略到 YAML 文件（可直接复制到 config.yaml）"""

        def _native(v):
            """转换 numpy 标量为 Python 原生类型"""
            if hasattr(v, "item"):
                return float(v.item()) if hasattr(v, "dtype") else v
            return round(float(v), 4) if isinstance(v, float) else v

        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        fname = save_dir / f"{report.report_id}_{report.group}_strategies.yaml"

        output = {
            "report_id": report.report_id,
            "group": report.group,
            "timestamp": report.timestamp,
            "iterations": report.iterations,
            "elapsed_seconds": report.elapsed_seconds,
            "benchmarks": dict(report.benchmarks),
        }

        strategies = []
        for i, t in enumerate(report.top_strategies, 1):
            strat = {
                "rank": i,
                "train_return": _native(t.train_return),
                "test_return": _native(t.test_return),
                "test_drawdown": _native(t.test_drawdown),
                "sharpe": _native(t.sharpe),
                "trade_count": t.trade_count,
                "params": t.params,
                "rules": [
                    {
                        "id": r.id,
                        "type": r.type,
                        "priority": r.priority,
                        "condition": r.condition,
                        "action_amount": r.action_amount,
                        "action_fraction": r.action_fraction,
                        "reset_when": r.reset_when,
                    }
                    for r in t.rules
                ],
            }
            strategies.append(strat)

        output["strategies"] = strategies

        with open(fname, "w", encoding="utf-8") as f:
            yaml.dump(output, f, allow_unicode=True, default_flow_style=False,
                       sort_keys=False)

        logger.info("[StrategyOptimizer] 结果已保存到 %s", fname)
        return fname

    def generate_html_report(
        self,
        report: OptimizationReport,
        save_dir: str | Path = "data/optimizer",
    ) -> Path:
        """生成静态 HTML 可视化报告"""
        import json

        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        template_path = (
            Path(__file__).parent.parent / "templates" / "optimizer_report.html"
        )
        template = template_path.read_text(encoding="utf-8")

        best = report.top_strategies[0] if report.top_strategies else None
        best_ret = best.test_return if best else 0
        best_dd = best.test_drawdown if best else 0
        best_sp = best.sharpe if best else 0
        best_trades = best.trade_count if best else 0
        best_sp_data = best.sub_periods if best else {}

        # 各组名映射
        group_names = {"a_share": "A 股", "non_a_share": "非 A 股 / 境外"}
        group_label = group_names.get(report.group, report.group)

        # phase 数据转 JSON-serializable
        phase_json: dict[str, dict] = {}
        for k, v in (best_sp_data or {}).items():
            if hasattr(v, "label"):
                d = {}
                for f_name in ("label", "total_return", "max_drawdown",
                               "sharpe_ratio", "trade_count", "excess_return"):
                    val = getattr(v, f_name, None)
                    if hasattr(val, "item"):
                        val = float(val.item())
                    elif isinstance(val, float):
                        val = round(val, 4)
                    d[f_name] = val
                phase_json[k] = d

        # 策略列表（可序列化）
        strats = []
        for s in (report.top_strategies or []):
            strats.append({
                "train_return": round(float(getattr(s, "train_return", 0)), 4),
                "test_return": round(float(getattr(s, "test_return", 0)), 4),
                "test_drawdown": round(float(getattr(s, "test_drawdown", 0)), 4),
                "sharpe": round(float(getattr(s, "sharpe", 0)), 4),
                "trade_count": getattr(s, "trade_count", 0),
                "params": getattr(s, "params", {}),
                "rules": [
                    {
                        "id": r.id, "type": r.type, "priority": r.priority,
                        "condition": r.condition, "action_amount": r.action_amount,
                        "action_fraction": r.action_fraction, "reset_when": r.reset_when,
                    }
                    for r in (getattr(s, "rules", None) or [])
                ],
            })

        pf_buy = len(self._filtered_buy_builders or BUY_BUILDERS)
        pf_sell = len(self._filtered_sell_builders or SELL_BUILDERS)
        data = {
            "all_returns": [round(float(x), 4) for x in (report.all_train_returns or [])],
            "convergence": [round(float(x), 4) for x in (report.convergence or [])],
            "random_line": (report.n_random_starts + 0.5) if report.n_random_starts else None,
            "best_phases": phase_json,
            "strategies": strats,
            "benchmarks": report.benchmarks or {},
        }
        json_data = json.dumps(data, ensure_ascii=False)

        # __TOKEN__ 风格替换（避免与 CSS/JS 花括号冲突）
        stock_count = len(best.params.get("_stocks", "").split(",")) if best else 0
        replacements = {
            "__GROUP_NAME__": group_label,
            "__REPORT_ID__": report.report_id,
            "__ITERATIONS__": str(report.iterations),
            "__ELAPSED__": f"{report.elapsed_seconds:.0f}s",
            "__TEST_RET__": f"{best_ret:+.1f}",
            "__TEST_COLOR__": ' red' if best_ret < 0 else '',
            "__DD__": f"{best_dd:.1f}",
            "__SHARPE__": f"{best_sp:.3f}",
            "__TRADES__": str(best_trades),
            "__STOCKS_COUNT__": str(stock_count) if stock_count else "—",
            "__PF_BUY__": str(pf_buy),
            "__PF_SELL__": str(pf_sell),
            "__JSON_DATA__": json_data,
        }

        html = template
        for old, new in replacements.items():
            html = html.replace(old, new)

        fname = save_dir / f"{report.report_id}_{report.group}_report.html"
        fname.write_text(html, encoding="utf-8")
        logger.info("[HTMLReport] 已保存到 %s", fname)
        return fname
