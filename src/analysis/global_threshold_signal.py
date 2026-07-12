"""GlobalThresholdSignalFn — 现有 H1-H6 全局阈值逻辑, 包装为 SignalFn。

验收标准 4: 旧系统标记为 deprecated — 此实现包装原有 CONDITION_BUILDERS_FAST 注册表,
保持每日输出与之前完全相同的逻辑。
"""
from __future__ import annotations

import numpy as np

from .signal_functions import SignalFn, ParamDim, ParamSpace, Params

# 从 fast_evaluator 拿构建器清单（与旧系统一致）
from .fast_evaluator import CONDITION_BUILDERS_FAST, _build_none
from .optimizer_constraints import DiscreteSearchConfig

GLOBAL_BUY_BUILDERS = [
    "trend_follow", "deviation_absolute", "volume_spike",
    "rsi_signal", "bollinger_signal",
    "deviation_cross", "none",
]
GLOBAL_SELL_BUILDERS = [
    "deviation_cross", "rsi_signal", "bollinger_signal",
    "deviation_absolute", "sell_overextended", "trend_follow", "none",
]
N_BUY = 5
N_SELL = 3
THRESH_LEVELS = 10
FRAC_LEVELS = 5  # [0.05, 0.15, 0.25, 0.35, 0.45]
POS_LEVELS = 20

# builder → 人类可读名（与 email_notifier.SIGNAL_NAMES 一致）
_BUILDER_HUMAN = {
    "deviation_cross": "偏离穿越",
    "deviation_absolute": "偏离达标",
    "rsi_signal": "RSI超卖",
    "bollinger_signal": "布林低位",
    "volume_spike": "放量异动",
    "trend_follow": "趋势跟踪",
    "deep_value": "深度价值",
    "absolute_discount": "绝对折价",
    "sell_overextended": "超涨卖出",
    "sell_deviation_cross": "偏离穿越(卖)",
    "sell_rsi_signal": "RSI超买",
    "sell_bollinger_signal": "布林高位",
    "sell_trend_follow": "趋势反转",
    "none": "无",
}


def _make_dims(prefix: str, n: int, builder_count: int) -> list[ParamDim]:
    dims = []
    for i in range(1, n + 1):
        dims.append(ParamDim(f"{prefix}_idx_{i}", builder_count))
        dims.append(ParamDim(f"{prefix}_t_{i}", THRESH_LEVELS, 0.0, 1.0))
        dims.append(ParamDim(f"{prefix}_frac_{i}", FRAC_LEVELS, 0.05, 0.45))
    return dims


def _decode_frac(level: int) -> float:
    fracs = [0.05, 0.15, 0.25, 0.35, 0.45]
    return fracs[min(level, len(fracs) - 1)]


class GlobalThresholdSignalFn(SignalFn):
    """全局阈值引擎 — v1.18 版本操作逻辑 (deprecated 标记)。

    参数: 5 买入 + 3 卖出规则, 每条规则选择一种 Builder、一个归一化阈值、一个仓位比例。
    信号输出: 各独立规则的条件矩阵 × 仓位比例的总和。
    """

    def __init__(self):
        self._space = ParamSpace(
            _make_dims("buy", N_BUY, len(GLOBAL_BUY_BUILDERS))
            + _make_dims("sell", N_SELL, len(GLOBAL_SELL_BUILDERS))
            + [
                ParamDim("pos_slope", POS_LEVELS, 0.5, 10.0),
                ParamDim("pos_bias", POS_LEVELS, -3.0, 3.0),
            ]
        )

    @property
    def name(self) -> str:
        return "global"

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def evaluate(self, params: Params, indicator_matrix: np.ndarray) -> np.ndarray:
        T, N, K = indicator_matrix.shape
        buy_scores = np.zeros((T, N), dtype=np.float32)
        sell_scores = np.zeros((T, N), dtype=np.float32)

        for i in range(1, N_BUY + 1):
            idx = params.values.get(f"buy_idx_{i}", 0) % len(GLOBAL_BUY_BUILDERS)
            builder = GLOBAL_BUY_BUILDERS[idx]
            if builder == "none":
                continue
            t_norm = params.decode(self._space.dims[(i - 1) * 3 + 1])
            frac = _decode_frac(params.values.get(f"buy_frac_{i}", 2))
            fn = CONDITION_BUILDERS_FAST.get(builder, _build_none)
            cond, _ = fn(indicator_matrix, t_norm)
            buy_scores += cond.astype(np.float32) * frac

        for i in range(1, N_SELL + 1):
            idx = params.values.get(f"sell_idx_{i}", 0) % len(GLOBAL_SELL_BUILDERS)
            builder = GLOBAL_SELL_BUILDERS[idx]
            if builder == "none":
                continue
            t_norm = params.decode(self._space.dims[(N_BUY + i - 1) * 3 + 1])
            frac = _decode_frac(params.values.get(f"sell_frac_{i}", 2))
            fn = CONDITION_BUILDERS_FAST.get(builder, _build_none)
            cond, _ = fn(indicator_matrix, t_norm)
            sell_scores += cond.astype(np.float32) * frac

        return np.stack([buy_scores, sell_scores], axis=-1)

    def to_human_readable(self, params: Params) -> str:
        lines = ["全局阈值策略 (GlobalThresholdSignalFn — deprecated)"]
        for i in range(1, N_BUY + 1):
            idx = params.values.get(f"buy_idx_{i}", 0)
            builder = GLOBAL_BUY_BUILDERS[min(idx % len(GLOBAL_BUY_BUILDERS), len(GLOBAL_BUY_BUILDERS) - 1)]
            t_norm = params.decode(self._space.dims[(i - 1) * 3 + 1])
            frac = _decode_frac(params.values.get(f"buy_frac_{i}", 2))
            lines.append(f"  买{i}: {builder} t={t_norm:.2f} frac={frac:.2f}")
        for i in range(1, N_SELL + 1):
            idx = params.values.get(f"sell_idx_{i}", 0)
            builder = GLOBAL_SELL_BUILDERS[min(idx % len(GLOBAL_SELL_BUILDERS), len(GLOBAL_SELL_BUILDERS) - 1)]
            t_norm = params.decode(self._space.dims[(N_BUY + i - 1) * 3 + 1])
            frac = _decode_frac(params.values.get(f"sell_frac_{i}", 2))
            lines.append(f"  卖{i}: {builder} t={t_norm:.2f} frac={frac:.2f}")
        return "\n".join(lines)

    # ── 显示层：全局引擎沿用旧 YAML condition 扫描路径 ──
    # scan_signals 返回 [] → SignalScanner 走 legacy rules.condition 评估,
    # 保证 criterion 1/2（默认引擎日报版式与逻辑完全不变）。

    def scan_signals(self, params, today: dict, history=None) -> list[dict]:
        return []

    def describe_rules(self, params) -> dict:
        """从 YAML params (buy_N_signal) 翻译买卖规则名。"""
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        buy, sell = [], []
        for k, v in vals.items():
            if k.endswith("_signal") and v and v != "none":
                name = _BUILDER_HUMAN.get(str(v), str(v))
                if k.startswith("buy"):
                    buy.append(name)
                elif k.startswith("sell"):
                    sell.append(name)
        return {"buy": buy, "sell": sell}

    def engine_brief(self) -> str:
        return (
            "全局阈值引擎 (global, 默认)\n"
            "  原理: 各指标用固定绝对阈值判断, 满足条件即触发\n"
            "  买入: 偏离穿越 / 偏离达标 / RSI超卖 / 布林低位 / 放量 / 趋势跟踪\n"
            "  卖出: 偏离穿越 / RSI超买 / 布林高位 / 超涨 / 趋势反转"
        )
