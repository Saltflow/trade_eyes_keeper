"""
仓位目标模型预览脚本

对比 Fixed-Frac (旧) 和 Position-Target (新) 两种交易执行逻辑。
用 Walk-Forward 第一个窗口的数据，跑一组简单策略，输出对比表格。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _run_preview():
    from src.analysis.fast_evaluator import (
        FastEvaluator,
    )
    from src.analysis.walk_forward import WalkForwardManager
    from src.utils.config_loader import load_config

    config = load_config()

    # 用 A 股第一个 Walk-Forward 窗口的测试期数据
    wf_mgr = WalkForwardManager(config, group="a_share")
    windows = wf_mgr.iter_windows()
    if not windows:
        logger.error("没有 Walk-Forward 窗口数据，请先运行一次主程序采集数据")
        return

    ws = windows[0]
    indicator = wf_mgr.build_matrices(ws, "test")
    price_close = wf_mgr.get_price_matrix(ws, "test")
    price_open = price_close.copy()  # 简化为开盘=收盘

    T, N, K = indicator.shape
    print(f"窗口 {ws.window_id}: {N} 只股票, {T} 个交易日")
    print(f"  训练期: {ws.train_start_date} → {ws.train_end_date}")
    print(f"  测试期: {ws.test_start_date} → {ws.test_end_date}")

    # 构建现金基准线
    rf_daily = 0.02 / 252.0
    initial = 100000.0
    train_end_cash = initial * (1.0 + rf_daily) ** ws.train_days
    cash_baseline = np.cumsum(np.ones(T) * train_end_cash * rf_daily) + train_end_cash

    # 策略: MA 偏离穿越买入 + RSI 超买卖出（简化版，不搜索参数）
    buy_builders = ["deviation_cross"] * 2  # 2条买入规则
    buy_thresholds = [0.3, 0.7]  # 不同阈值
    buy_fracs = [0.15, 0.25]     # 旧模式用

    sell_builders = ["sell_rsi_signal"]
    sell_thresholds = [0.5]
    sell_fracs = [0.3]           # 旧模式用

    evaluator = FastEvaluator(
        initial_cash=100000.0,
        monthly_buy_limit=15000.0,
        lot_size=100,
        commission_rate=0.002,
    )

    # ── 旧模式: Fixed-Frac ──
    stats_old = evaluator.evaluate(
        indicator, price_close, cash_baseline,
        buy_builders=buy_builders,
        buy_thresholds=buy_thresholds,
        buy_fracs=buy_fracs,
        sell_builders=sell_builders,
        sell_thresholds=sell_thresholds,
        sell_fracs=sell_fracs,
        price_open_matrix=price_open,
    )

    # ── 新模式: Position-Target ──
    stats_new = evaluator.evaluate_position_target(
        indicator, price_close, cash_baseline,
        buy_builders=buy_builders,
        buy_thresholds=buy_thresholds,
        sell_builders=sell_builders,
        sell_thresholds=sell_thresholds,
        position_slope=2.0,
        position_bias=0.0,
        price_open_matrix=price_open,
    )

    # ── 输出对比 ──
    print()
    print("=" * 62)
    print(f"{'指标':<20} {'Fixed-Frac (旧)':>18} {'Position-Target (新)':>22}")
    print("=" * 62)
    print(f"{'超额收益 %':<20} {stats_old.test_excess_return:>18.2f} {stats_new.test_excess_return:>22.2f}")
    print(f"{'最大回撤 %':<20} {stats_old.max_drawdown_pct:>18.2f} {stats_new.max_drawdown_pct:>22.2f}")
    print(f"{'平均仓位 %':<20} {stats_old.avg_position_pct:>18.2f} {stats_new.avg_position_pct:>22.2f}")
    print(f"{'夏普比率':<20} {stats_old.sharpe_ratio:>18.4f} {stats_new.sharpe_ratio:>22.4f}")
    print(f"{'总交易次数':<20} {stats_old.total_trades:>18} {stats_new.total_trades:>22}")
    print(f"{'月均交易':<20} {stats_old.trades_per_month:>18.1f} {stats_new.trades_per_month:>22.1f}")
    print("=" * 62)
    print()
    print("备注: Fixed-Frac 使用固定的买入/卖出比例档位；")
    print("       Position-Target 由 bullish_score 通过 sigmoid 映射到目标仓位，每日渐进调整。")

    # 额外：展示不同 slope/bias 的效果
    print()
    print("─ 灵敏度扫描 (slope 扫描, bias=0) ─")
    for slope in [1.0, 3.0, 5.0, 8.0]:
        s = evaluator.evaluate_position_target(
            indicator, price_close, cash_baseline,
            buy_builders=buy_builders,
            buy_thresholds=buy_thresholds,
            sell_builders=sell_builders,
            sell_thresholds=sell_thresholds,
            position_slope=slope,
            position_bias=0.0,
            price_open_matrix=price_open,
        )
        print(
            f"  slope={slope:<4.1f} | "
            f"超额={s.test_excess_return:>7.2f}% | "
            f"回撤={s.max_drawdown_pct:>6.2f}% | "
            f"仓位={s.avg_position_pct:>5.1f}% | "
            f"交易={s.total_trades:>4} | "
            f"月均={s.trades_per_month:>4.1f}"
        )

    print()
    print("─ 偏移扫描 (bias 扫描, slope=2) ─")
    for bias in [-1.5, -0.5, 0.0, 0.5, 1.5]:
        s = evaluator.evaluate_position_target(
            indicator, price_close, cash_baseline,
            buy_builders=buy_builders,
            buy_thresholds=buy_thresholds,
            sell_builders=sell_builders,
            sell_thresholds=sell_thresholds,
            position_slope=2.0,
            position_bias=bias,
            price_open_matrix=price_open,
        )
        print(
            f"  bias={bias:<5.1f} | "
            f"超额={s.test_excess_return:>7.2f}% | "
            f"回撤={s.max_drawdown_pct:>6.2f}% | "
            f"仓位={s.avg_position_pct:>5.1f}% | "
            f"交易={s.total_trades:>4} | "
            f"月均={s.trades_per_month:>4.1f}"
        )


if __name__ == "__main__":
    _run_preview()
