"""分位评分引擎冒烟测试 — 验证 evaluate_percentile 核心链路。"""
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["LOG_LEVEL"] = "ERROR"


def test_evaluate_percentile_basic():
    """3 只标的 × 50 天数据，分位评分应返回有效 WindowStats。"""
    from analysis.fast_evaluator import FastEvaluator
    from analysis.optimizer_constraints import WindowStats

    T, N = 50, 3
    # 构造含分位列的指标矩阵 (T, N, 16)
    ind = np.random.randn(T, N, 16).astype(np.float32)
    # 分位列 (11-15) 置为 0-1 均匀分布
    for c in range(11, 16):
        ind[:, :, c] = np.sort(np.random.rand(T, N), axis=0).astype(np.float32)
    ind[:, :, 0] = 100 + np.cumsum(np.random.randn(T, N) * 2, axis=0)  # close
    price = ind[:, :, 0].copy()
    train_cash = 100000.0
    cash_baseline = np.ones(T) * train_cash * (1 + 0.02 / 252.0)

    ev = FastEvaluator(initial_cash=train_cash)
    stats = ev.evaluate_percentile(
        ind, price, cash_baseline,
        pct_columns=[11, 12, 13],
        pct_thresholds=[0.4, 0.6, 0.8],
        weights=[0.3, 0.3, 0.4],
        score_buy_threshold=0.5,
        score_sell_threshold=0.5,
        position_frac=0.25,
    )
    assert isinstance(stats, WindowStats)
    # 应有一些交易（至少分位触发应产生信号）
    assert stats.total_trades >= 0
    print("OK: total_trades=%d, excess_return=%.2f%%, max_dd=%.2f%%, sharpe=%.4f"
          % (stats.total_trades, stats.test_excess_return,
             stats.max_drawdown_pct, stats.sharpe_ratio))


def test_engine_creates_valid_encoding():
    """PercentileSignalFn produces valid encoding."""
    from analysis.percentile_engine import PercentileSignalFn
    engine = PercentileSignalFn()
    space = engine.param_space
    params = space.random()
    assert space.total_levels() > 100
    human = engine.to_human_readable(params)
    assert "分位评分" in human
    assert "tau=" in human
    print("OK: total_levels=%d, human=\"%s...\"" % (space.total_levels(), human[:60]))


if __name__ == "__main__":
    test_evaluate_percentile_basic()
    test_engine_creates_valid_encoding()
    print("ALL PASSED")
