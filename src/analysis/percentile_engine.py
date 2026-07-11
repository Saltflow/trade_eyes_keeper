"""分位评分策略引擎（§8 新参数化）。

每只标的独立评估自身历史分位，加权求和打分，分数够高则触发买卖。
与 GlobalThresholdEngine 共用同一 StrategyEncoding 数据结构，仅字段语义不同。

编码映射（5 买入 + 3 卖出）：
- buy_builders[i] ∈ [0,4]  → PERCENTILE_COLUMNS[idx]
- buy_thresholds[i] ∈ [0,9] → τ ∈ [0.1, 0.9]
- buy_fracs[i] ∈ [0,4]     → w ∈ [0.1, 0.5]
- position_slope ∈ [0,9]   → τ_buy ∈ [0.1, 0.9]
- position_bias ∈ [0,9]    → τ_sell ∈ [0.1, 0.9]
"""
from __future__ import annotations

import random as _random
from typing import TYPE_CHECKING

from .strategy_interface import StrategyEngine
from .fast_evaluator import IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT, \
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT

if TYPE_CHECKING:
    from .genetic_searcher import StrategyEncoding
    from .fast_evaluator import FastEvaluator, WindowStats
    from .optimizer_constraints import StrategyConstraints, DiscreteSearchConfig
    import numpy as np

# 分位列索引（全局阈值引擎用名称字符串，此引擎用列索引）
PERCENTILE_COLUMNS = [
    IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT,
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT,
]
PERCENTILE_LABELS = [
    "adx_pct", "rsi_pct", "deviation_pct",
    "vol_ratio_pct", "ma200_dev_pct",
]

N_BUY = 5
N_SELL = 3
N_COLS = len(PERCENTILE_COLUMNS)
THRESHOLD_LEVELS = 10   # [0,9] → [0.1, 0.9]
WEIGHT_LEVELS = 5       # [0,4] → [0.1, 0.3, 0.5, 0.7, 0.9]


class PercentileScoringEngine(StrategyEngine):
    """分位评分引擎。

    评估流程：
    1. 将编码解释为分位列索引 + τ + 权重
    2. 调 FastEvaluator.evaluate_percentile()
    3. 返回 WindowStats + wf_score
    """

    def param_count(self) -> int:
        return N_BUY * 3 + N_SELL * 3 + 2  # 26

    def random_encoding(self, ds_cfg) -> "StrategyEncoding":
        from .genetic_searcher import StrategyEncoding
        return StrategyEncoding(
            buy_builders=[_random.randint(0, N_COLS - 1) for _ in range(N_BUY)],
            buy_thresholds=[_random.randint(0, THRESHOLD_LEVELS - 1) for _ in range(N_BUY)],
            buy_fracs=[_random.randint(0, WEIGHT_LEVELS - 1) for _ in range(N_BUY)],
            sell_builders=[_random.randint(0, N_COLS - 1) for _ in range(N_SELL)],
            sell_thresholds=[_random.randint(0, THRESHOLD_LEVELS - 1) for _ in range(N_SELL)],
            sell_fracs=[_random.randint(0, WEIGHT_LEVELS - 1) for _ in range(N_SELL)],
            position_slope=_random.randint(0, THRESHOLD_LEVELS - 1),
            position_bias=_random.randint(0, THRESHOLD_LEVELS - 1),
        )

    def evaluate_encoding(
        self,
        encoding: "StrategyEncoding",
        windows,
        ds_cfg: "DiscreteSearchConfig",
        constraints: "StrategyConstraints",
        evaluator: "FastEvaluator",
        wf_manager,
    ) -> tuple[list["WindowStats"], float] | None:
        # ── 编码 → 分位参数 ──
        buy_cols = [PERCENTILE_COLUMNS[i % N_COLS] for i in encoding.buy_builders]
        buy_taus = [_tau(encoding.buy_thresholds[i]) for i in range(min(N_BUY, len(encoding.buy_thresholds)))]
        buy_ws = [_weight(encoding.buy_fracs[i]) for i in range(min(N_BUY, len(encoding.buy_fracs)))]

        tau_buy = _tau(encoding.position_slope)
        tau_sell = _tau(encoding.position_bias)

        all_stats = []
        for w in windows:
            test_ind = wf_manager.build_matrices(w, "test")
            test_price = wf_manager.get_price_matrix(w, "test")
            T_test = test_ind.shape[0]
            if T_test == 0 or test_ind.shape[1] == 0:
                continue

            rf_rate = getattr(constraints, "risk_free_rate", 0.02)
            train_ind = wf_manager.build_matrices(w, "train")
            train_end_cash = evaluator.initial_cash * (1.0 + rf_rate / 252.0) ** train_ind.shape[0]
            cash_baseline = np.cumsum(np.ones(T_test) * train_end_cash * rf_rate / 252.0) + train_end_cash

            from collections import OrderedDict
            import numpy as np
            benchmark_series = OrderedDict()
            for bcode in constraints.benchmark_codes:
                if bcode == "risk_free":
                    rr_daily = rf_rate / 252.0
                    rf_series = np.cumsum(np.ones(T_test) * train_end_cash * rr_daily) + train_end_cash
                    benchmark_series["risk_free"] = rf_series
                else:
                    b_close = wf_manager.get_benchmark_price(bcode, w, "test")
                    if b_close is not None and len(b_close) == T_test and not np.isnan(b_close[0]):
                        benchmark_series[bcode] = b_close

            stats = evaluator.evaluate_percentile(
                test_ind, test_price, cash_baseline,
                pct_columns=buy_cols,
                pct_thresholds=buy_taus,
                weights=buy_ws,
                score_buy_threshold=tau_buy,
                score_sell_threshold=tau_sell,
                position_frac=0.25,
                benchmark_series=benchmark_series if benchmark_series else None,
            )
            all_stats.append(stats)

        if not all_stats:
            return None

        v_win = getattr(constraints.walk_forward, "validation_windows", 0)
        ranking_stats = all_stats[:-v_win] if v_win > 0 and len(all_stats) > v_win else all_stats
        returns = [s.test_excess_return for s in ranking_stats]
        wfw = constraints.walk_forward.window_weights[:len(returns)]
        total_w = sum(wfw)
        wfw = [w / total_w for w in wfw] if total_w > 0 else [1.0 / len(returns)] * len(returns)
        mean_return = sum(r * w for r, w in zip(returns, wfw))
        std_return = float(np.std(returns)) if len(returns) >= 2 else 0.0
        wf_score = mean_return - constraints.walk_forward.stability_penalty * std_return
        return all_stats, wf_score

    def crossover_encoding(
        self, p1: "StrategyEncoding", p2: "StrategyEncoding",
    ) -> "StrategyEncoding":
        from .genetic_searcher import StrategyEncoding
        def _cross(a, b, n):
            if n == 0:
                return []
            k = _random.randint(1, n - 1) if n > 1 else 0
            return a[:k] + b[k:]
        return StrategyEncoding(
            buy_builders=_cross(p1.buy_builders, p2.buy_builders, N_BUY),
            buy_thresholds=_cross(p1.buy_thresholds, p2.buy_thresholds, N_BUY),
            buy_fracs=_cross(p1.buy_fracs, p2.buy_fracs, N_BUY),
            sell_builders=_cross(p1.sell_builders, p2.sell_builders, N_SELL),
            sell_thresholds=_cross(p1.sell_thresholds, p2.sell_thresholds, N_SELL),
            sell_fracs=_cross(p1.sell_fracs, p2.sell_fracs, N_SELL),
            position_slope=p1.position_slope if _random.random() < 0.5 else p2.position_slope,
            position_bias=p1.position_bias if _random.random() < 0.5 else p2.position_bias,
        )

    def mutate_encoding(
        self, encoding: "StrategyEncoding", ds_cfg,
    ) -> "StrategyEncoding":
        from .genetic_searcher import StrategyEncoding
        def _mut_list(lst, max_val):
            if len(lst) == 0:
                return lst
            i = _random.randint(0, len(lst) - 1)
            new = lst[:]
            new[i] = _random.randint(0, max_val)
            return new
        return StrategyEncoding(
            buy_builders=_mut_list(encoding.buy_builders, N_COLS - 1),
            buy_thresholds=_mut_list(encoding.buy_thresholds, THRESHOLD_LEVELS - 1),
            buy_fracs=_mut_list(encoding.buy_fracs, WEIGHT_LEVELS - 1),
            sell_builders=_mut_list(encoding.sell_builders, N_COLS - 1),
            sell_thresholds=_mut_list(encoding.sell_thresholds, THRESHOLD_LEVELS - 1),
            sell_fracs=_mut_list(encoding.sell_fracs, WEIGHT_LEVELS - 1),
            position_slope=encoding.position_slope if _random.random() < 0.7 else _random.randint(0, THRESHOLD_LEVELS - 1),
            position_bias=encoding.position_bias if _random.random() < 0.7 else _random.randint(0, THRESHOLD_LEVELS - 1),
        )

    def to_human_readable(self, encoding: "StrategyEncoding", ds_cfg) -> str:
        lines = ["分位评分策略 (PercentileScoringEngine)"]
        lines.append(f"  买入信号 ({N_BUY}条):")
        for i in range(N_BUY):
            if i < len(encoding.buy_builders):
                col_name = PERCENTILE_LABELS[encoding.buy_builders[i] % N_COLS]
                tau = _tau(encoding.buy_thresholds[i]) if i < len(encoding.buy_thresholds) else 0.5
                w = _weight(encoding.buy_fracs[i]) if i < len(encoding.buy_fracs) else 0.3
                lines.append(f"    {i+1}. {col_name} τ={tau:.2f} w={w:.2f}")
        lines.append(f"  卖出信号 ({N_SELL}条):")
        for i in range(N_SELL):
            if i < len(encoding.sell_builders):
                col_name = PERCENTILE_LABELS[encoding.sell_builders[i] % N_COLS]
                tau = _tau(encoding.sell_thresholds[i]) if i < len(encoding.sell_thresholds) else 0.5
                w = _weight(encoding.sell_fracs[i]) if i < len(encoding.sell_fracs) else 0.3
                lines.append(f"    {i+1}. {col_name} τ={tau:.2f} w={w:.2f}")
        lines.append(f"  买入分数阈值 τ_buy = {_tau(encoding.position_slope):.2f}")
        lines.append(f"  卖出分数阈值 τ_sell = {_tau(encoding.position_bias):.2f}")
        return "\n".join(lines)


def _tau(level: int, levels: int = THRESHOLD_LEVELS) -> float:
    """分位阈值: [0, levels-1] → [0.1, 0.9]"""
    return 0.1 + (level / max(levels - 1, 1)) * 0.8


def _weight(level: int, levels: int = WEIGHT_LEVELS) -> float:
    """权重: [0, levels-1] → [0.1, 0.9]"""
    return 0.1 + (level / max(levels - 1, 1)) * 0.8
