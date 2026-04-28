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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from .rule_engine import Rule
from .backtest_config import (
    BacktestConfig,
    make_training_config,
    make_default_optimizer_config,
)
from .portfolio_strategy import PortfolioEvaluator
from .indicator_library import compute_all

logger = logging.getLogger(__name__)


@dataclass
class StrategyTrial:
    """单次策略试验记录"""

    params: dict[str, float]  # 参数名→值
    rules: list[Rule]  # 生成的规则列表
    train_return: float  # 训练期（0-12月）总收益率(%)
    train_drawdown: float  # 训练期最大回撤(%)
    test_return: float  # 测试期（12-24月）总收益率(%)
    test_drawdown: float  # 测试期最大回撤(%)
    sharpe: float  # 全期夏普比
    trade_count: int  # 总交易次数
    sub_periods: dict | None = None  # PortfolioResult.sub_periods 原始数据

    @property
    def fitness(self) -> float:
        """训练期适应度（用于 Bayessian 优化目标）"""
        return self.train_return


@dataclass
class OptimizationReport:
    """优化报告"""

    report_id: str
    group: str
    timestamp: str
    iterations: int
    n_random_starts: int = 20  # 贝叶斯优化随机初始点数
    top_strategies: list[StrategyTrial] = field(default_factory=list)
    convergence: list[float] = field(default_factory=list)
    all_train_returns: list[float] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    best_params: dict[str, float] = field(default_factory=dict)


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

    # ── 模板渲染 ──

    def _collect_params(self) -> list[dict]:
        """收集所有可搜索参数（有序）"""
        params = []
        seen = set()
        for rule_list_key in ("buy_rules", "sell_rules"):
            for rule_tpl in self.template.get(rule_list_key, []):
                for p in rule_tpl.get("search_params", []):
                    name = p["name"]
                    if name not in seen:
                        seen.add(name)
                        params.append(p)
        return params

    def _build_dimensions(self) -> list:
        """构建 skopt 搜索空间"""
        from skopt.space import Real, Integer

        dims = []
        for p in self._collect_params():
            rng = p["range"]
            if p["type"] == "real":
                dims.append(Real(rng[0], rng[1], name=p["name"]))
            elif p["type"] == "integer":
                dims.append(Integer(int(rng[0]), int(rng[1]), name=p["name"]))
            else:
                logger.warning("未知参数类型: %s，跳过", p["type"])
        return dims

    def _params_to_rules(self, param_vec: list[float]) -> list[Rule]:
        """将参数向量转换为 Rule 列表"""
        param_dict = {}
        for i, p in enumerate(self._collect_params()):
            val = param_vec[i]
            if p["type"] == "integer":
                param_dict[p["name"]] = int(val)
            else:
                param_dict[p["name"]] = round(float(val), 4)  # 截断浮点精度

        rules = []
        for rule_tpl in self.template.get("buy_rules", []) + self.template.get(
            "sell_rules", []
        ):
            try:
                condition = rule_tpl["condition_template"].format(**param_dict)
                action = rule_tpl.get("action_template", "")
                if action:
                    action = action.format(**param_dict)
            except KeyError as e:
                logger.warning("模板渲染缺少参数 %s，跳过规则 %s", e, rule_tpl.get("id"))
                continue

            rule = Rule(
                id=rule_tpl["id"],
                label=rule_tpl.get("label", rule_tpl["id"]),
                type=rule_tpl["type"],
                priority=rule_tpl.get("priority", 1),
                condition=condition,
                budget_pool=rule_tpl.get("budget_pool", rule_tpl["type"]),
                action_amount=action if rule_tpl["type"] == "buy" else None,
                action_fraction=rule_tpl.get("action_fraction"),
                action_min=rule_tpl.get("action_min"),
                action_max=rule_tpl.get("action_max"),
                reset_when=rule_tpl.get("reset_when"),
            )
            rules.append(rule)

        return rules

    # ── 优化主流程 ──

    def run(
        self,
        stock_codes: list[str],
        max_drawdown_pct: float | None = None,
        iterations: int | None = None,
        random_starts: int | None = None,
        report_id: str = "",
    ) -> OptimizationReport:
        """
        执行两阶段策略搜索。

        Args:
            stock_codes: 参与优化的股票代码列表
            max_drawdown_pct: 最大允许回撤（负数），超出则惩罚
            iterations: 贝叶斯优化迭代次数
            random_starts: 随机初始采样点数
            report_id: 报告标识（用于保存文件）

        Returns:
            OptimizationReport
        """
        from skopt import gp_minimize

        # 参数默认值
        dd_limit = max_drawdown_pct or self.constraints.get("max_drawdown_pct", -25)
        n_calls = iterations or self.constraints.get("iterations", 150)
        n_random = random_starts or self.constraints.get("random_starts", 20)
        penalty = self.constraints.get("drawdown_penalty_weight", 2.0)

        # 1. 预计算指标
        if not self.indicators:
            logger.info("[IndicatorLibrary] 计算 6 类指标...")
            t0 = time.time()
            self.indicators = compute_all(self.stocks_data)
            logger.info(
                "[IndicatorLibrary] 完成，%d 只股票，耗时 %.1fs",
                len(self.indicators),
                time.time() - t0,
            )

        # 2. 构建搜索空间
        dimensions = self._build_dimensions()
        param_spec = self._collect_params()
        param_names = [p["name"] for p in param_spec]
        logger.info(
            "[StrategyOptimizer] 搜索空间: %d 维参数 | 迭代 %d 轮",
            len(dimensions),
            n_calls,
        )

        # 3. 评估器
        evaluator = PortfolioEvaluator(self.stocks_data, self.group)
        train_config = make_training_config()

        # 4. 记录所有试验
        all_trials: list[tuple[list[float], list[Rule], float, float]] = []
        best_fitness = -float("inf")

        def objective(param_vec: list[float]) -> float:
            """目标函数: 最大化训练期收益，超出回撤限度时惩罚"""
            nonlocal best_fitness

            rules = self._params_to_rules(param_vec)
            # 注入自定义规则到 evaluator
            evaluator.rules = rules
            evaluator.stocks_data = self.stocks_data

            result = evaluator.evaluate(
                stock_codes,
                backtest_config=train_config,
                indicators_data=self.indicators,
            )

            # 适应度 = 收益率 + 回撤惩罚
            fitness = result.total_return
            if result.max_drawdown < dd_limit:
                fitness += (result.max_drawdown - dd_limit) * penalty

            all_trials.append((list(param_vec), rules, result.total_return,
                               result.max_drawdown))

            if fitness > best_fitness:
                best_fitness = fitness
                logger.info(
                    "[BayesOpt] 新最优 | 训练收益 %.1f%% | 回撤 %.1f%% | 适应度 %.1f",
                    result.total_return,
                    result.max_drawdown,
                    fitness,
                )

            return -fitness  # gp_minimize 最小化

        # 5. 贝叶斯优化
        t0 = time.time()
        opt_result = gp_minimize(
            objective,
            dimensions,
            n_calls=n_calls,
            n_random_starts=n_random,
            verbose=False,
        )
        elapsed = time.time() - t0
        logger.info(
            "[BayesOpt] 完成。%d 次评估，耗时 %.0fs",
            n_calls,
            elapsed,
        )

        # 6. 阶段 B: 最终评估（0-24 月）
        test_config = make_default_optimizer_config()
        evaluator_test = PortfolioEvaluator(self.stocks_data, self.group)
        trials: list[StrategyTrial] = []

        logger.info("[FinalEval] 在全时间线上重跑 %d 个候选策略...", len(all_trials))
        for param_vec, rules, train_ret, train_dd in all_trials:
            evaluator_test.rules = rules
            evaluator_test.stocks_data = self.stocks_data
            result = evaluator_test.evaluate(
                stock_codes,
                backtest_config=test_config,
                indicators_data=self.indicators,
            )

            test_ret = 0.0
            test_dd = 0.0
            if "test" in result.sub_periods:
                sp = result.sub_periods["test"]
                test_ret = sp.total_return
                test_dd = sp.max_drawdown

            params_dict = {name: val for name, val in zip(param_names, param_vec)}
            trials.append(
                StrategyTrial(
                    params=params_dict,
                    rules=rules,
                    train_return=train_ret,
                    train_drawdown=train_dd,
                    test_return=test_ret,
                    test_drawdown=test_dd,
                    sharpe=result.sharpe_ratio,
                    trade_count=result.trade_count,
                    sub_periods=result.sub_periods,
                )
            )

        # 按测试期收益排序
        trials.sort(key=lambda t: t.test_return, reverse=True)
        top_n = self.output_cfg.get("top_n", 10)
        top_trials = trials[:top_n]

        # 收敛曲线
        convergence = [-opt_result.func_vals[:i].min()
                       for i in range(1, len(opt_result.func_vals) + 1)]
        all_train_returns = [t[2] for t in all_trials]

        report = OptimizationReport(
            report_id=report_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
            group=self.group,
            timestamp=datetime.now().isoformat(),
            iterations=n_calls,
            n_random_starts=n_random,
            top_strategies=top_trials,
            convergence=convergence,
            all_train_returns=all_train_returns,
            elapsed_seconds=elapsed,
            best_params=top_trials[0].params if top_trials else {},
        )

        # 生成收敛图
        save_dir = Path(self.output_cfg.get("save_dir", "data/optimizer"))
        save_dir.mkdir(parents=True, exist_ok=True)
        plot_path = save_dir / f"{report.report_id}_{report.group}_convergence.png"
        self._plot_convergence(report, plot_path)

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

            ax1.set_ylabel("训练收益 (%)", fontsize=11)
            ax1.set_xlabel("迭代轮次", fontsize=11)
            ax1.legend(loc="lower right", fontsize=9)
            ax1.grid(True, alpha=0.3)
            ax1.set_title("图 1: 贝叶斯收敛曲线 (训练期 0-12 月)", fontsize=12)

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
                returns.append(m.total_return if m else 0)
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
            ax2.set_ylabel("区间收益率 (%)", fontsize=11)
            ax2.set_title("图 2: 最优策略 3 阶段分拆 (排名 #1)", fontsize=12)

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
        lines.append(
            f"  {'排名':<4} {'训练(0-12)':>10} {'测试(12-24)':>10} "
            f"{'回撤':>8} {'夏普':>7} {'交易':>5}"
        )
        lines.append("  " + "-" * 50)

        for i, t in enumerate(report.top_strategies, 1):
            lines.append(
                f"  {i:<4} {t.train_return:>+9.1f}% {t.test_return:>+9.1f}% "
                f"{t.test_drawdown:>+7.1f}% {t.sharpe:>7.4f} {t.trade_count:>5d}"
            )

        lines.append("")
        if report.top_strategies:
            best = report.top_strategies[0]
            lines.append("  ★ 最优策略参数:")
            for k, v in sorted(best.params.items()):
                lines.append(f"      {k}: {v:.4f}" if isinstance(v, float)
                             else f"      {k}: {v}")
            lines.append("")
            lines.append("  ★ 最优策略规则:")
            for rule in best.rules:
                lines.append(f"      [{rule.id}] {rule.label}")
                lines.append(f"        条件: {rule.condition}")
                lines.append(f"        动作: {rule.action_amount or rule.action_fraction}")
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

        lines.append("=" * 70)
        return "\n".join(lines)

    def save_results(
        self,
        report: OptimizationReport,
        save_dir: str | Path = "data/optimizer",
    ) -> Path:
        """保存最优策略到 YAML 文件（可直接复制到 config.yaml）"""
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        fname = save_dir / f"{report.report_id}_{report.group}_strategies.yaml"

        output = {
            "report_id": report.report_id,
            "group": report.group,
            "timestamp": report.timestamp,
            "iterations": report.iterations,
            "elapsed_seconds": report.elapsed_seconds,
        }

        strategies = []
        for i, t in enumerate(report.top_strategies, 1):
            strat = {
                "rank": i,
                "train_return": t.train_return,
                "test_return": t.test_return,
                "test_drawdown": t.test_drawdown,
                "sharpe": t.sharpe,
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
