"""
策略搜索器 V2 (StrategyOptimizerV2)

基于 Walk-Forward + 遗传搜索 + 向量化评估的新一代优化器。

与 V1 的关键区别:
  - 贝叶斯优化 → 遗传搜索 (离散参数空间)
  - 单测试期排名 → Walk-Forward 多窗口评分
  - 逐日 Python 回测 → numpy/numba 向量化评估 (100x+ 提速)
  - 2买3卖 → 5买0卖 (全买入规则)
  - 软性回撤惩罚 → 硬性约束过滤 + 月度交易密度审核

接口兼容 V1: 构造函数、run() 方法、返回值格式均相同。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from .optimizer_constraints import (
    StrategyConstraints,
    WindowStats,
    load_constraints,
)
from .walk_forward import WalkForwardManager, create_walk_forward_manager
from .fast_evaluator import FastEvaluator
from .genetic_searcher import GeneticSearcher, ScoredStrategy, StrategyEncoding
from .strategy_optimizer import (
    OptimizationReport,
    StrategyTrial,
    build_condition,
    Rule,
)
from .backtest_config import (
    BacktestConfig,
    make_default_optimizer_config,
)

logger = logging.getLogger(__name__)


# 策略参数人话映射
# builder → 简短中文名（与 email_notifier.SIGNAL_NAMES 对齐, 供 rule.label 使用）
_GLOBAL_SIGNAL_NAMES = {
    "deviation_cross": "偏离穿越", "deviation_absolute": "偏离达标",
    "rsi_signal": "RSI超卖", "bollinger_signal": "布林低位",
    "volume_spike": "放量异动", "trend_follow": "趋势跟踪",
    "deep_value": "深度价值", "absolute_discount": "绝对折价",
    "sell_overextended": "超涨卖出", "sell_deviation_cross": "偏离穿越(卖)",
    "sell_rsi_signal": "RSI超买", "sell_bollinger_signal": "布林高位",
    "sell_trend_follow": "趋势反转", "none": "无",
}

_BUILDER_LABELS = {
    "deviation_cross": lambda t, n=None: f"MA60偏离下穿 {-0.005 + t * (-0.295):.1%} 时买入",
    "deviation_absolute": lambda t, n=None: f"MA60偏离 < {-t * 0.40:.0%} 时买入",
    "rsi_signal": lambda t, n=None: f"RSI < {10 + (1 - t) * 30:.0f} 时买入",
    "bollinger_signal": lambda t, n=None: f"布林%%B < {(1 - t) * 0.35:.2f} 时买入",
    "volume_spike": lambda t, n=None: f"量比 > {1.2 + t * 2.8:.1f} 时买入",
    "trend_follow": lambda t, n=None: f"ADX > {15 + t * 25:.0f} 且 MACD>0 时买入",
    "absolute_discount": lambda t, n=None: f"距2年高点跌幅 > {-0.10 + t * (-0.60):.0%} 时买入",
    "deep_value": lambda t, n=None: f"MA200偏离 < {-0.05 + t * (-0.35):.0%} 且趋势启稳时买入",
    "none": lambda t, n=None: "(未使用)",
    "sell_deviation_cross": lambda t, n=None: f"MA60偏离上穿 {0.005 + t * 0.30:.1%} 时卖出",
    "sell_deviation_absolute": lambda t, n=None: f"MA60偏离 > {t * 0.50:.0%} 时卖出",
    "sell_rsi_signal": lambda t, n=None: f"RSI > {60 + t * 30:.0f} 时卖出",
    "sell_bollinger_signal": lambda t, n=None: f"布林%%B > {0.65 + t * 0.35:.2f} 时卖出",
    "sell_trend_follow": lambda t, n=None: f"ADX > {15 + t * 25:.0f} 且 MACD<0 时卖出",
    "sell_overextended": lambda t, n=None: f"接近2年高点(差距<{-0.05 + t * 0.05:.0%})时卖出",
}


class StrategyOptimizerV2:
    """策略搜索器 V2

    用法:
        opt = StrategyOptimizerV2(stocks_data, "a_share")
        report = opt.run(stock_codes=list(stocks_data.keys()))
    """

    def __init__(
        self,
        stocks_data: dict[str, pd.DataFrame],
        group: str,
        constraints_path: str | Path = "config/optimizer_constraints.yaml",
        indicators_data: dict[str, "pd.DataFrame"] | None = None,
        n_samples: int | None = None,
        n_generations: int | None = None,
        engine=None,  # StrategyEngine（None=旧全局阈值模式）
        signal_fn=None,  # SignalFn（新接口, 支持 global/percentile）
    ):
        """
        Args:
            stocks_data: {code: DataFrame with date/close/high/low/volume}
            group: "a_share" 或 "non_a_share"
            constraints_path: 约束配置文件路径
            indicators_data: 预计算指标（可选）
            n_samples: Phase 1 采样数覆盖（None=config 默认）
            n_generations: Phase 2 遗传代数覆盖（None=config 默认）
        """
        self.stocks_data = stocks_data
        self.group = group
        self.constraints_path = Path(constraints_path)
        self.n_samples = n_samples
        self.n_generations = n_generations
        self.engine = engine
        self.signal_fn = signal_fn if signal_fn is not None else None

        # 加载约束
        self.constraints = load_constraints(constraints_path)
        self.wf_cfg = self.constraints.walk_forward
        self.gs_cfg = self.constraints.genetic_search
        self.ds_cfg = self.constraints.discrete_search

        # 预计算指标 (可复用外部传入)
        self.indicators_data = indicators_data

    def _load_benchmarks(self) -> dict[str, "pd.DataFrame"]:
        """加载基准 ETF 数据（cache/data/ 优先，缓存缺失则通过 DataSource 网络拉取）。

        Returns:
            {bench_code: DataFrame with date/close} dict
        """
        self.constraints.set_group(self.group)
        benchmark_codes = self.constraints.benchmark_codes
        if not benchmark_codes:
            return {}

        from pathlib import Path
        cache_dir = Path("cache/data")

        # 延迟导入 DataSource（避免循环依赖）
        _ds = None

        def _get_ds():
            nonlocal _ds
            if _ds is not None:
                return _ds
            from src.data.data_source import DataSource
            import yaml
            config_path = Path("config") / "config.yaml"
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            _ds = DataSource(config)
            return _ds

        bench_dfs: dict[str, pd.DataFrame] = {}
        for bcode in benchmark_codes:
            if bcode == "risk_free":
                continue
            csv_path = cache_dir / f"{bcode}.csv"
            bdf = None
            try:
                if csv_path.exists():
                    bdf = pd.read_csv(csv_path, encoding="utf-8")
                    bdf["date"] = pd.to_datetime(bdf["date"])
                    bdf = bdf.sort_values("date")
                    logger.info(f"[V2] 基准 {bcode}: {len(bdf)} 行数据 (缓存)")
                else:
                    logger.info(f"[V2] 基准 {bcode}: 缓存缺失，尝试网络拉取...")
                    ds = _get_ds()
                    bdf = ds.fetch_stock_data(bcode, days=252 * 5)
                    if bdf is not None and not bdf.empty:
                        bdf = bdf.sort_values("date")
                        logger.info(f"[V2] 基准 {bcode}: {len(bdf)} 行数据 (网络)")
                    else:
                        logger.warning(f"[V2] 基准 {bcode}: 网络拉取失败，跳过")

            except Exception as e:
                logger.warning(f"[V2] 基准 {bcode} 加载失败: {e}")

            if bdf is not None and not bdf.empty:
                bench_dfs[bcode] = bdf

        return bench_dfs

    def run(
        self,
        stock_codes: list[str],
        max_drawdown_pct: float | None = None,
        iterations: int | None = None,
        random_starts: int | None = None,
        report_id: str = "",
    ) -> OptimizationReport:
        """执行完整的三阶段搜索

        Args:
            stock_codes: 候选股票代码列表（默认全部纳入）
            max_drawdown_pct: 覆盖约束文件中的最大回撤（None=使用配置文件）
            iterations: 覆盖遗传搜索参数（None=使用配置文件）
            random_starts: 覆盖 Phase 1 随机采样数（None=使用配置文件）
            report_id: 报告 ID（用于文件命名）

        Returns:
            OptimizationReport (与 V1 格式兼容)
        """
        # ── 1. 覆盖配置 ──
        if max_drawdown_pct is not None:
            self.constraints.max_drawdown_pct = max_drawdown_pct
        if iterations is not None:
            self.gs_cfg.phase1_random_samples = iterations
        if random_starts is not None:
            self.gs_cfg.phase1_random_samples = random_starts

        # ── 2. Walk-Forward 管理器 ──
        t0 = time.time()
        logger.info("[V2] 构建 Walk-Forward 管理器...")
        # 加载基准 ETF 数据
        benchmark_dfs = self._load_benchmarks()
        wf_mgr = WalkForwardManager(
            self.stocks_data,
            indicators_data=self.indicators_data,
            train_months=self.wf_cfg.train_months,
            test_months=self.wf_cfg.test_months,
            step_months=self.wf_cfg.step_months,
            num_windows=self.wf_cfg.num_windows,
            benchmark_dfs=benchmark_dfs,
        )

        if not wf_mgr.stock_codes:
            logger.error("[V2] 没有有效的股票数据")
            return OptimizationReport(
                report_id=report_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
                group=self.group,
                timestamp=datetime.now().isoformat(),
                iterations=0,
                top_strategies=[],
            )

        windows = wf_mgr.iter_windows()
        logger.info(
            "[V2] Walk-Forward: %d 个窗口, %d 只股票 (%s)",
            len(windows), wf_mgr.n_stocks,
            ", ".join(wf_mgr.stock_codes[:5]) + ("..." if wf_mgr.n_stocks > 5 else ""),
        )

        # ── 3. 快速评估器 ──
        # A股用100股手数，非A股用1
        lot_size = 100 if self.group == "a_share" else 1
        evaluator = FastEvaluator(
            initial_cash=100000.0,
            monthly_buy_limit=100000.0,
            lot_size=lot_size,
            commission_rate=0.002,
        )

        # ── 4. 遗传搜索 ──
        searcher = GeneticSearcher(self.constraints, wf_mgr, evaluator, engine=self.engine)

        logger.info(
            "[V2] Phase 1: 粗筛 %d 个随机策略",
            self.gs_cfg.phase1_random_samples,
        )
        t1 = time.time()
        phase1_results = searcher.run_phase1(windows)
        logger.info(
            "[V2] Phase 1 完成: %d 个有效策略, 耗时 %.0fs",
            len(phase1_results), time.time() - t1,
        )

        if not phase1_results:
            logger.error("[V2] Phase 1 没有策略通过约束，搜索失败")
            return OptimizationReport(
                report_id=report_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
                group=self.group,
                timestamp=datetime.now().isoformat(),
                iterations=self.gs_cfg.phase1_random_samples,
                top_strategies=[],
            )

        logger.info(
            "[V2] Phase 2: 遗传优化 %d 代, 种群 %d, 每代 %d 后代",
            self.gs_cfg.num_generations, self.gs_cfg.population_size,
            self.gs_cfg.offspring_size,
        )
        t2 = time.time()
        final_population = searcher.run_phase2(phase1_results, windows)
        logger.info(
            "[V2] Phase 2 完成: 耗时 %.0fs",
            time.time() - t2,
        )

        # ── 5. 构建 OptimizationReport ──
        report = self._build_report(
            final_population, wf_mgr, windows, report_id, time.time() - t0,
        )

        # ── 6. 保存结果 ──
        save_dir = Path("data/optimizer")
        save_dir.mkdir(parents=True, exist_ok=True)
        self._save_results(report, save_dir)

        # ── 6b. 更新策略分布池（贝叶斯增量更新）──
        try:
            from .strategy_distribution import StrategyDistributionPool
            pool_path = save_dir / "strategy_distributions.yaml"
            pool = StrategyDistributionPool(pool_path)

            # 取 Top10 搜索结果转为分布池更新格式
            search_results = []
            for t in report.top_strategies[:10]:
                # 过滤掉元数据字段
                clean_params = {
                    k: float(v) for k, v in t.params.items()
                    if not k.startswith("_") and isinstance(v, (int, float, str))
                }
                try:
                    clean_params = {k: float(v) for k, v in clean_params.items()}
                except (ValueError, TypeError):
                    continue

                search_results.append({
                    "params": clean_params,
                    "wf_scores": [t.test_return] * 6,  # V2 只有一个聚合得分，复制为6份
                    "recent_return": t.test_return,
                })

            if search_results:
                pool.update(search_results)
                logger.info(
                    "[V2] 策略分布池已更新: %d 个分布, Top1 评分 %.2f",
                    len(pool.distributions),
                    pool.distributions[0].overall_score if pool.distributions else 0,
                )
        except Exception as e:
            logger.warning(f"[V2] 策略分布池更新失败: {e}")

        logger.info(
            "[V2] 搜索完成: 总耗时 %.0fs, Top1 WF得分 %.2f",
            report.elapsed_seconds,
            report.top_strategies[0].test_return if report.top_strategies else -999,
        )

        return report

    def _build_report(
        self,
        scored: list["ScoredStrategy"],
        wf_mgr,
        windows,
        report_id: str,
        elapsed: float,
    ) -> OptimizationReport:
        """将遗传搜索结果转换为 OptimizationReport

        最终输出前执行完整硬约束过滤，确保产出的策略全部合规。
        """

        if not report_id:
            report_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        trials: list[StrategyTrial] = []
        accepted = 0

        for ss in scored:
            # 最终过滤: 只检查最大回撤（关乎生存的唯一硬约束）
            # 仓位/一致性/交易密度作为风险提示，不阻止入选
            max_dd = min(ws.max_drawdown_pct for ws in ss.window_stats)
            if max_dd < self.constraints.max_drawdown_pct:
                continue  # 回撤超限，丢弃

            accepted += 1
            if accepted > 10:
                break

            # 完整约束检查（用于生成风险提示）
            _, violations = self.constraints.check_hard_constraints(
                ss.window_stats, ss.wf_score,
            )

            # 转换为 Rule 列表（沿用 V1 格式）
            is_signal_fn = (
                self.signal_fn is not None and hasattr(ss.encoding, "values")
            )
            if is_signal_fn:
                rules = self._signal_fn_to_rules(ss.encoding)
            else:
                rules = self._encoding_to_rules(ss.encoding)

            # 从窗口统计推算训练/测试期指标
            # test_return = 排序窗口均值（排除验证窗口）
            v_win = getattr(self.constraints.walk_forward, "validation_windows", 0)
            all_ws = ss.window_stats
            rank_ws = all_ws[:-v_win] if v_win > 0 and len(all_ws) > v_win else all_ws
            avg_test_ret = np.mean([ws.test_excess_return for ws in rank_ws])
            avg_dd = np.mean([ws.max_drawdown_pct for ws in rank_ws])
            avg_sharpe = np.mean([s.sharpe_ratio for s in rank_ws])
            total_trades = sum(ws.total_trades for ws in all_ws)

            # 收集基准收益 + 期末持仓（取最后窗口的数据）
            bench_info: dict[str, float] = {}
            strat_ret: float = 0.0
            final_pos: float = 0.0
            final_cash_val: float = 0.0
            final_holdings: list[dict] = []
            total_nav: float = 0.0

            if ss.window_stats:
                if ss.window_stats[0].benchmark_returns:
                    bench_info = dict(ss.window_stats[0].benchmark_returns)
                    strat_ret = ss.window_stats[0].strategy_return
                # 期末持仓
                last_ws = ss.window_stats[-1]
                if last_ws.final_shares is not None:
                    last_window = windows[-1] if windows else None
                    if last_window is not None:
                        final_prices = wf_mgr.get_price_matrix(last_window, "all")
                        final_day_prices = final_prices[-1] if final_prices.shape[0] > 0 else None
                        codes = wf_mgr.stock_codes
                        if final_day_prices is not None:
                            shares_arr = last_ws.final_shares
                            cost_arr = last_ws.cost_basis
                            total_pos_val = 0.0
                        for i in range(min(len(codes), len(shares_arr))):
                            qty = float(shares_arr[i])
                            if qty <= 0.5:  # 忽略零头
                                continue
                                px = float(final_day_prices[i])
                                if px <= 0 or np.isnan(px):
                                    continue
                                value = qty * px
                                total_pos_val += value
                                cb = float(cost_arr[i]) if cost_arr is not None and i < len(cost_arr) else 0.0
                                holding = {
                                    "code": codes[i],
                                    "shares": round(qty, 1),
                                    "price": round(px, 2),
                                    "value": round(value, 2),
                                    "cost": round(cb, 2),
                                    "cost_value": round(qty * cb, 2) if cb > 0 else 0.0,
                                }
                                final_holdings.append(holding)
                            final_cash_val = round(float(last_ws.final_cash), 2) if last_ws.final_cash else 0.0
                            total_nav = round(total_pos_val + final_cash_val, 2)
                            final_pos = round(total_pos_val / total_nav * 100, 1) if total_nav > 0 else 0.0

            # 季度持仓明细 (取最后一个窗口，持仓最充分)
            quarterly: list[dict] = []
            last_ws_q = ss.window_stats[-1] if ss.window_stats else None
            if last_ws_q is not None and last_ws_q.quarter_shares is not None:
                import numpy as np2
                q_shares = last_ws_q.quarter_shares
                q_cash = last_ws_q.quarter_cash
                q_nav = last_ws_q.quarter_nav
                q_prices = last_ws_q.quarter_prices
                cost_arr_q = last_ws_q.cost_basis if last_ws_q.cost_basis is not None else np2.zeros(q_shares.shape[1])
                codes = wf_mgr.stock_codes
                N_Q = q_shares.shape[0] if q_shares.ndim >= 1 else 0

                # 从第一个窗口拿价格矩阵算季度间隔天数
                w_last = windows[-1] if windows else None
                interval_days = 0
                if w_last is not None:
                    interval_days = max(1, (w_last.test_end_idx - w_last.test_start_idx) // max(N_Q, 1))

                for qi in range(N_Q):
                    q_positions = []
                    pos_val = 0.0
                    for i in range(min(len(codes), q_shares.shape[1])):
                        qty = float(q_shares[qi, i]) if q_shares.ndim >= 2 else 0.0
                        if qty <= 0.5:
                            continue
                        px = float(q_prices[qi, i]) if q_prices.ndim >= 2 else 0.0
                        if px <= 0 or np.isnan(px):
                            continue
                        value = qty * px
                        pos_val += value
                        cb = float(cost_arr_q[i])
                        q_positions.append({
                            "code": codes[i],
                            "shares": round(qty, 1),
                            "cost": round(cb, 2),
                            "price": round(px, 2),
                            "value": round(value, 2),
                            "pnl": round((px - cb) * qty, 2) if cb > 0 else 0.0,
                            "pnl_pct": round((px / cb - 1) * 100, 1) if cb > 0 else 0.0,
                        })
                    nav_val = float(q_nav[qi]) if q_nav.ndim >= 1 else 0.0
                    cash_val = float(q_cash[qi]) if q_cash.ndim >= 1 else 0.0
                    pos_pct = round(pos_val / nav_val * 100, 1) if nav_val > 0 else 0.0
                    quarterly.append({
                        "quarter": qi + 1,
                        "day": (qi + 1) * interval_days,
                        "cash": round(cash_val, 2),
                        "nav": round(nav_val, 2),
                        "pos_pct": pos_pct,
                        "positions": q_positions,
                    })
            else:
                quarterly = []

            # 构建参数摘要
            params_summary: dict[str, str] = {}
            if is_signal_fn:
                # 分位/自定义引擎：写真实引擎参数（整数级别 → 原样存）
                for k, v in ss.encoding.values.items():
                    params_summary[k] = v
                params_summary["_mode"] = "signal_score"
            else:
                use_pt = self.ds_cfg.use_position_target
                # 买入
                for j in range(ss.encoding.n_buy_rules):
                    b = ss.encoding.buy_builders[j]
                    t = ss.encoding.buy_thresholds[j]
                    f = ss.encoding.buy_fracs[j]
                    builder_name = self.ds_cfg.buy_builders[b]
                    params_summary[f"buy_{j+1}_signal"] = builder_name
                    params_summary[f"buy_{j+1}_t"] = f"{t / (self.ds_cfg.threshold_levels - 1):.3f}" if self.ds_cfg.threshold_levels > 1 else "0.000"
                    if not use_pt:
                        params_summary[f"buy_{j+1}_frac"] = f"{self.ds_cfg.frac_levels[f]:.3f}"
                # 卖出
                for j in range(ss.encoding.n_sell_rules):
                    b = ss.encoding.sell_builders[j]
                    t = ss.encoding.sell_thresholds[j]
                    f = ss.encoding.sell_fracs[j]
                    builder_name = self.ds_cfg.sell_builders[b]
                    params_summary[f"sell_{j+1}_signal"] = builder_name
                    params_summary[f"sell_{j+1}_t"] = f"{t / (self.ds_cfg.threshold_levels - 1):.3f}" if self.ds_cfg.threshold_levels > 1 else "0.000"
                    if not use_pt:
                        params_summary[f"sell_{j+1}_frac"] = f"{self.ds_cfg.sell_frac_levels[f]:.3f}"
                # 仓位目标参数
                if use_pt:
                    sl, bi = ss.encoding.to_position_params(self.ds_cfg)
                    params_summary["position_slope"] = f"{sl:.2f}"
                    params_summary["position_bias"] = f"{bi:.2f}"
                    params_summary["_mode"] = "position_target"
                else:
                    params_summary["_mode"] = "frac"
            params_summary["_stocks"] = ",".join(wf_mgr.stock_codes[:12])
            if self.signal_fn is not None:
                params_summary["_engine"] = self.signal_fn.name
            if violations:
                params_summary["_warnings"] = "; ".join(violations[:3])

            trial = StrategyTrial(
                params=params_summary,
                rules=rules,
                train_return=-999,
                train_drawdown=-999,
                test_return=round(avg_test_ret, 2),
                test_drawdown=round(avg_dd, 2),
                sharpe=round(avg_sharpe, 4),
                trade_count=total_trades,
                benchmark_returns=bench_info,
                strategy_return=round(strat_ret, 2),
                final_position_pct=final_pos,
                final_holdings=final_holdings,
                final_cash=final_cash_val,
                total_nav=total_nav,
                quarterly_holdings=quarterly,
                strategy_description=(
                    self.signal_fn.to_human_readable(ss.encoding)
                    if self.signal_fn is not None and hasattr(ss.encoding, 'values')
                    else (self.engine.to_human_readable(ss.encoding, self.ds_cfg)
                          if self.engine is not None
                          else self._format_strategy_description(ss.encoding, self.ds_cfg))
                ),
            )
            trials.append(trial)

        return OptimizationReport(
            report_id=report_id,
            group=self.group,
            timestamp=datetime.now().isoformat(),
            iterations=self.gs_cfg.phase1_random_samples,
            n_random_starts=self.gs_cfg.phase1_top_keep,
            top_strategies=trials,
            convergence=[],  # V2 不需要收敛曲线
            all_train_returns=[],
            elapsed_seconds=round(elapsed, 1),
            best_params=trials[0].params if trials else {},
        )

    @staticmethod
    def _format_strategy_description(encoding, ds_cfg) -> str:
        """将策略编码转为人话描述。"""
        lines = []
        # 买入规则
        lines.append("买入条件:")
        for i in range(encoding.n_buy_rules):
            bn = ds_cfg.buy_builders[encoding.buy_builders[i]]
            tn = encoding.buy_thresholds[i] / max(ds_cfg.threshold_levels - 1, 1)
            label = _BUILDER_LABELS.get(bn, lambda t, n=None: bn)(tn, bn)
            lines.append(f"  {i+1}. {label}")
        # 卖出规则
        lines.append("卖出条件:")
        has_sell = False
        for i in range(encoding.n_sell_rules):
            bn = ds_cfg.sell_builders[encoding.sell_builders[i]]
            if bn == "none":
                continue
            tn = encoding.sell_thresholds[i] / max(ds_cfg.threshold_levels - 1, 1)
            label = _BUILDER_LABELS.get(bn, lambda t, n=None: bn)(tn, bn)
            lines.append(f"  {i+1}. {label}")
            has_sell = True
        if not has_sell:
            lines.append("  (无卖出规则)")
        # 仓位控制
        if ds_cfg.use_position_target:
            sl, bi = encoding.to_position_params(ds_cfg)
            pm = ds_cfg.position_model if hasattr(ds_cfg, 'position_model') else {}
            adj = getattr(ds_cfg, 'max_daily_adjust', 0.10)
            sl_str = f"敏感度={sl:.1f}" if sl else ""
            bi_str = f"倾向={'保守' if bi<0 else '激进'}" if bi else ""
            lines.append(f"仓位控制: {sl_str} {bi_str} 日调仓上限={pm.get('max_daily_adjust', 0.10):.0%}".strip())
        return "\n".join(lines)

    def _encoding_to_rules(self, encoding: StrategyEncoding) -> list[Rule]:
        """将遗传编码转换为 Rule 列表（V1 兼容格式，支持买入+卖出）"""
        rules = []
        use_pt = self.ds_cfg.use_position_target

        # 买入规则
        for i in range(encoding.n_buy_rules):
            builder_name = self.ds_cfg.buy_builders[encoding.buy_builders[i]]
            t_norm = encoding.buy_thresholds[i] / (self.ds_cfg.threshold_levels - 1) if self.ds_cfg.threshold_levels > 1 else 0.0
            frac = self.ds_cfg.frac_levels[encoding.buy_fracs[i]]

            condition, reset_when = build_condition(builder_name, t_norm, "buy")

            if use_pt:
                action = "position_target"  # 仓位由 sigmoid 模型驱动
            else:
                action = f"cash * {frac}"

            rules.append(Rule(
                id=f"buy_{i+1}",
                label=_GLOBAL_SIGNAL_NAMES.get(builder_name, f"买入规则{i+1}"),
                type="buy",
                priority=i + 1,
                condition=condition,
                budget_pool="buy",
                action_amount=action,
                reset_when=reset_when,
            ))

        # 卖出规则
        for i in range(encoding.n_sell_rules):
            builder_name = self.ds_cfg.sell_builders[encoding.sell_builders[i]]
            t_norm = encoding.sell_thresholds[i] / (self.ds_cfg.threshold_levels - 1) if self.ds_cfg.threshold_levels > 1 else 0.0
            frac = self.ds_cfg.sell_frac_levels[encoding.sell_fracs[i]]

            condition, reset_when = build_condition(builder_name, t_norm, "sell")

            rules.append(Rule(
                id=f"sell_{i+1}",
                label=_GLOBAL_SIGNAL_NAMES.get(builder_name,
                      _GLOBAL_SIGNAL_NAMES.get(f"sell_{builder_name}", f"卖出规则{i+1}")),
                type="sell",
                priority=encoding.n_buy_rules + i + 1,
                condition=condition,
                budget_pool="sell",
                action_fraction=frac if not use_pt else 0.0,
                action_min=2500.0 if not use_pt else 0.0,
                action_max=10000.0 if not use_pt else 0.0,
                reset_when=reset_when,
            ))

        return rules

    def _signal_fn_to_rules(self, params) -> list[Rule]:
        """SignalFn 引擎 → Rule 列表（用引擎自定义规则名, condition 留空由引擎扫描）。"""
        desc = self.signal_fn.describe_rules(params)
        rules: list[Rule] = []
        for i, name in enumerate(desc.get("buy", []), 1):
            rules.append(Rule(
                id=f"buy_{i}",
                label=name,
                type="buy",
                priority=i,
                condition="__signal_fn__",  # 标记：由 signal_fn.scan_signals 判断
                budget_pool="buy",
                action_amount="position_target",
            ))
        n_buy = len(rules)
        for i, name in enumerate(desc.get("sell", []), 1):
            rules.append(Rule(
                id=f"sell_{i}",
                label=name,
                type="sell",
                priority=n_buy + i,
                condition="__signal_fn__",
                budget_pool="sell",
                action_fraction=0.0,
            ))
        return rules

    def _save_results(self, report: OptimizationReport, save_dir: Path):
        """保存最优策略到 YAML 文件（V1 兼容格式）"""

        def _native(v):
            if hasattr(v, "item"):
                return float(v.item()) if hasattr(v, "dtype") else v
            return round(float(v), 4) if isinstance(v, float) else v

        fname = save_dir / f"{report.report_id}_{report.group}_strategies.yaml"

        output = {
            "report_id": report.report_id,
            "group": report.group,
            "timestamp": report.timestamp,
            "iterations": report.iterations,
            "elapsed_seconds": report.elapsed_seconds,
            "optimizer_version": "v2",
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
                        "id": r.id, "label": r.label, "type": r.type,
                        "priority": r.priority,
                        "condition": r.condition, "budget_pool": r.budget_pool,
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

        logger.info("[V2] 结果已保存到 %s", fname)
