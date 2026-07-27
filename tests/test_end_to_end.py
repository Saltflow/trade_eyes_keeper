"""端到端验收测试：run_optimizer() → 产出有效策略 → 日报链路可消费。

使用本地真实 CSV 数据（601088/600938/600795 各 ~832 行）。
"""

import sys
import os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["LOG_LEVEL"] = "ERROR"

# 测试用的 3 只票（都在 A 股，日期重叠，数据够长）
TEST_CODES = ["601088", "600938", "600795"]


def _load_stocks():
    """加载本地 CSV 为 {code: DataFrame}。"""
    data = {}
    for code in TEST_CODES:
        path = os.path.join("data", f"{code}_history.csv")
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        data[code] = df
    return data


# ═══════════════════════════════════════════════════════════════
# 1. run_optimizer() 产出有效策略
# ═══════════════════════════════════════════════════════════════

def test_optimizer_produces_valid_results():
    """GA 搜索产出 ≥1 个通过约束的策略，且指标在合理范围内。"""
    from src.analysis.strategies import get_strategy
    from src.analysis.optimizer import run_optimizer
    from src.analysis.config import StrategyConstraints

    stocks_data = _load_stocks()
    strategy = get_strategy("percentile")
    assert strategy is not None

    # 构造最小配置（train=3月, test=3月, Phase1=50抽样, 0代GA）
    constraints = StrategyConstraints({
        "walk_forward": {"train_months": 3, "test_months": 3, "step_months": 1, "num_windows": 1, "validation_windows": 0},
        "genetic_search": {
            "phase1_random_samples": 50,
            "phase1_top_keep": 10,
            "num_generations": 0,
            "population_size": 10,
            "offspring_size": 0,
            "crossover_rate": 0.7,
            "mutation_rate": 0.3,
            # This one-window smoke test is not an absolute-return gate test.
            "min_weighted_strategy_return": -999.0,
            "min_positive_return_windows": 0,
        },
        "discrete_search": {"mode": "frac", "buy_builders": ["deviation_cross", "rsi_signal"], "sell_builders": ["sell_deviation_cross", "sell_rsi_signal"], "num_buy_rules": 3, "num_sell_rules": 2},
        "hard_constraints": {"min_avg_position_pct": 0.0, "max_drawdown_pct": -50.0, "max_return_std_pct": 100.0, "min_trades_per_month": 0, "max_trades_per_month": 50},
        "benchmarks": {"a_share": [], "risk_free_rates": {"a_share": 0.02}},
        "execution_params": {"monthly_buy_limit": 15000, "initial_capital": 100000, "commission_rate": 0.005, "min_holding_days": 5, "lot_sizes": {"a_share": 100}},
    })
    constraints.set_group("a_share")

    results, _ = run_optimizer(strategy, stocks_data, TEST_CODES, "a_share", _constraints=constraints)

    # 验收 1: 至少产出 1 个结果
    assert len(results) > 0, "GA 应至少产出一个有效策略"

    top = results[0]
    stats = top.wf_stats[0]

    # 验收 2: WF 得分合理
    assert -30 < top.wf_score < 30, f"WF得分={top.wf_score} 应在 [-30,30]"

    # 验收 3: 交易数合理
    assert 0 <= stats.total_trades <= 30, f"交易数={stats.total_trades} 应在 [0,30]"

    # 验收 4: 回撤合理
    assert -50 <= stats.max_drawdown_pct <= 0, f"回撤={stats.max_drawdown_pct}% 应在 [-50,0]"

    # 验收 5: 仓位合理
    assert 0 <= stats.avg_position_pct <= 100, f"仓位={stats.avg_position_pct}% 应在 [0,100]"

    # 验收 6: 夏普合理
    assert -10 < stats.sharpe_ratio < 10, f"夏普={stats.sharpe_ratio} 应在 (-10,10)"

    # 验收 7: 季度快照结构
    if stats.quarter_shares is not None:
        assert stats.quarter_shares.shape == (4, len(TEST_CODES)), "季度快照 shape 应为 (4,N)"

    # 验收 8: 硬约束通过
    passed, violations = constraints.check_hard_constraints(top.wf_stats, top.wf_score)
    assert passed, f"Top1 应通过硬约束: {violations}"

    # 验收 9: make_signals 链路通
    params = top.encoding.to_params(strategy)
    ind = _build_indicator_matrix(stocks_data)
    buy, sell = strategy.make_signals(params, ind)
    assert buy.shape == sell.shape == (ind.shape[0], len(TEST_CODES))
    assert buy.dtype == bool and sell.dtype == bool


def _build_indicator_matrix(stocks_data):
    """构建 (T, N, K) 指标矩阵。"""
    from src.data.technical_indicators import compute_all
    computed = compute_all(stocks_data)

    dates_sets = [set(df.index) for df in computed.values() if df is not None and not df.empty]
    if not dates_sets:
        return np.zeros((1, 1, 16), dtype=np.float32)
    common = sorted(dates_sets[0].intersection(*dates_sets[1:]))
    if not common:
        return np.zeros((1, 1, 16), dtype=np.float32)

    T = len(common)
    N = len(TEST_CODES)
    from src.analysis.backtester import INDICATOR_NAMES
    K = len(INDICATOR_NAMES)
    mat = np.full((T, N, K), np.nan, dtype=np.float32)
    for i, code in enumerate(TEST_CODES):
        df = computed.get(code)
        if df is None or df.empty:
            continue
        aligned = df.reindex(pd.DatetimeIndex(common))
        for k, name in enumerate(INDICATOR_NAMES):
            if name in aligned.columns:
                mat[:, i, k] = aligned[name].values.astype(np.float32)
    return mat


# ═══════════════════════════════════════════════════════════════
# 2. 日报报告链路通
# ═══════════════════════════════════════════════════════════════

def test_daily_report_pipeline():
    """验证日报的 eval_yaml_strategy() 等价逻辑能正常产出报告。"""
    from src.analysis.strategies import get_strategy
    from src.analysis.backtester import simulate_portfolio
    from src.analysis.search_interface import Params
    from src.analysis.config import get_execution_config

    stocks_data = _load_stocks()
    strategy = get_strategy("percentile")
    exec_cfg = get_execution_config()

    # 使用一组典型 percentile 参数
    params = Params(values={
        "adx_pct_tau": 5, "adx_pct_w": 3, "rsi_pct_tau": 5, "rsi_pct_w": 3,
        "deviation_pct_tau": 6, "deviation_pct_w": 2, "vol_ratio_pct_tau": 5,
        "vol_ratio_pct_w": 2, "ma200_dev_pct_tau": 3, "ma200_dev_pct_w": 1,
        "buy_score_thresh": 5, "sell_score_thresh": 5, "position_frac": 2,
    }, _engine="percentile")

    # 构建评分矩阵
    from src.data.technical_indicators import compute_all
    computed = compute_all(stocks_data)
    dates_sets = [set(df.index) for df in computed.values() if df is not None and not df.empty]
    common = sorted(dates_sets[0].intersection(*dates_sets[1:]))
    T, N = len(common), len(TEST_CODES)

    buy_scores = np.zeros((T, N), dtype=np.float32)
    sell_scores = np.zeros((T, N), dtype=np.float32)
    price = np.zeros((T, N), dtype=np.float32)

    for i, code in enumerate(TEST_CODES):
        df = computed.get(code)
        if df is None:
            continue
        aligned = df.reindex(pd.DatetimeIndex(common))
        if "close" in aligned.columns:
            price[:len(aligned), i] = aligned["close"].values.astype(np.float32)

        bs, ss = strategy.score_timeseries(params, df)
        L = min(T, len(bs))
        buy_scores[:L, i] = bs[:L]
        sell_scores[:L, i] = ss[:L]

    dates = [d.strftime("%Y-%m-%d") for d in common]

    trace = simulate_portfolio(
        buy_scores, sell_scores, price,
        float(exec_cfg.initial_capital), 0.5, 0.5, 0.25,
        exec_cfg.lot_sizes.get("a_share", 100),
        float(exec_cfg.monthly_buy_limit), float(exec_cfg.commission_rate),
        dates, TEST_CODES,
    )

    # 报告应包含有效数据
    assert trace.total_trades >= 0
    assert abs(trace.total_return_pct) < 200, f"收益率={trace.total_return_pct}% 异常"
    assert -100 <= trace.max_drawdown_pct <= 0
    assert len(trace.quarterly_holdings) > 0, "应有季度持仓"
    assert isinstance(trace.nav_series, list) and len(trace.nav_series) > 0
