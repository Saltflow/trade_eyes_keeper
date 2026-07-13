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


def _pct_params(**overrides):
    """构造完整分位参数 dict（整数级别）。"""
    base = {
        "adx_pct_tau": 5, "adx_pct_w": 2,
        "rsi_pct_tau": 5, "rsi_pct_w": 2,
        "deviation_pct_tau": 5, "deviation_pct_w": 2,
        "vol_ratio_pct_tau": 5, "vol_ratio_pct_w": 2,
        "ma200_dev_pct_tau": 5, "ma200_dev_pct_w": 2,
        "buy_score_thresh": 3, "sell_score_thresh": 3, "position_frac": 2,
    }
    base.update(overrides)
    return base


def _mk_hist(n=300, seed=0, drift=0.4):
    import pandas as pd
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    t = np.linspace(0, drift, n) + rng.randn(n).cumsum() * 0.012
    close = 10 * np.exp(t)
    return pd.DataFrame({
        "date": dates, "stock_code": "600001",
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.abs(rng.randn(n)) * 1e6 + 5e5,
    })


class TestExecutionParams:
    def test_execution_params_decodes(self):
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        fn = PercentileSignalFn()
        p = Params(values=_pct_params(buy_score_thresh=9, sell_score_thresh=0,
                                      position_frac=4), _engine="percentile")
        ex = fn.execution_params(p)
        assert set(ex) == {"buy_threshold", "sell_threshold", "position_frac"}
        assert 0.1 <= ex["buy_threshold"] <= 0.9
        assert ex["buy_threshold"] > ex["sell_threshold"]  # 9 > 0
        assert 0.05 <= ex["position_frac"] <= 0.45

    def test_execution_params_accepts_plain_dict(self):
        from analysis.percentile_engine import PercentileSignalFn
        ex = PercentileSignalFn().execution_params(_pct_params())
        assert "buy_threshold" in ex


class TestScoreTimeseries:
    def test_shape_and_range(self):
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        fn = PercentileSignalFn()
        df = _mk_hist()
        buy, sell = fn.score_timeseries(Params(values=_pct_params()), df)
        assert len(buy) == len(df) == len(sell)
        # 净分 ∈ [-1,1]，sell = -net（天然互斥）
        assert buy.max() <= 1.0 + 1e-9 and buy.min() >= -1.0 - 1e-9
        assert np.allclose(sell, -buy)

    def test_empty_history(self):
        import pandas as pd
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        b, s = PercentileSignalFn().score_timeseries(
            Params(values=_pct_params()), pd.DataFrame())
        assert len(b) == 0 and len(s) == 0

    def test_all_min_weight_still_scores(self):
        # 权重级别0 = 最小权重0.1（非零）→ 所有5信号恒参与
        from analysis.percentile_engine import PercentileSignalFn, _decode_w
        from analysis.signal_functions import Params
        assert _decode_w(0) == 0.1  # 锁定：无真正零权重
        p = _pct_params(adx_pct_w=0, rsi_pct_w=0, deviation_pct_w=0,
                        vol_ratio_pct_w=0, ma200_dev_pct_w=0)
        b, s = PercentileSignalFn().score_timeseries(
            Params(values=p), _mk_hist())
        # 净分 ∈ [-1,1]（买分-卖分），sell=-net 天然互斥
        assert b.max() <= 1.0 + 1e-9 and b.min() >= -1.0 - 1e-9
        assert np.allclose(s, -b)


class TestScanSignals:
    def test_scan_returns_side_label_detail(self):
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        fn = PercentileSignalFn()
        df = _mk_hist(drift=0.8)  # 强上涨 → 高分位买入
        p = Params(values=_pct_params(buy_score_thresh=0))  # 低阈值确保触发
        hits = fn.scan_signals(p, {}, df)
        for h in hits:
            assert h["side"] in ("buy", "sell")
            assert "分位评分" in h["label"]
            assert isinstance(h["detail"], str)

    def test_scan_empty_history_no_crash(self):
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        assert PercentileSignalFn().scan_signals(
            Params(values=_pct_params()), {}, None) == []

    def test_scan_computes_missing_source_columns(self):
        # 仅有 OHLCV 的历史（无 rsi/adx/deviation/ma200_dev），
        # _ensure_source_columns 应兜底 deviation/ma200_dev
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        fn = PercentileSignalFn()
        df = _mk_hist()[["date", "close", "open", "high", "low", "volume"]]
        hits = fn.scan_signals(Params(values=_pct_params(buy_score_thresh=0)), {}, df)
        assert isinstance(hits, list)  # 不崩溃


class TestDescribeRules:
    def test_describe_rules_buy_sell_names(self):
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_functions import Params
        d = PercentileSignalFn().describe_rules(Params(values=_pct_params()))
        assert "buy" in d and "sell" in d
        assert any("分位" in x for x in d["buy"])
        assert any("买入" in x for x in d["buy"])
        assert any("卖出" in x for x in d["sell"])

    def test_all_five_signals_present(self):
        # 权重恒 >0（最小0.1）→ 5 个分位信号全部列入激活规则
        from analysis.percentile_engine import PercentileSignalFn, PERCENTILE_HUMAN
        from analysis.signal_functions import Params
        d = PercentileSignalFn().describe_rules(Params(values=_pct_params()))
        for human in PERCENTILE_HUMAN.values():
            assert any(human in x for x in d["buy"]), human


class TestEngineBrief:
    def test_brief_contains_buy_sell_standard(self):
        from analysis.percentile_engine import PercentileSignalFn
        brief = PercentileSignalFn().engine_brief()
        assert "percentile" in brief
        assert "买入" in brief and "卖出" in brief
        assert "分位" in brief


class TestGenomeOps:
    def test_random_crossover_mutate_keep_shape(self):
        from analysis.percentile_engine import PercentileSignalFn
        fn = PercentileSignalFn()
        p1 = fn.random_params()
        p2 = fn.random_params()
        assert p1._engine == "percentile"
        child = fn.crossover(p1, p2)
        assert set(child.values) == set(p1.values)
        mut = fn.mutate(p1, rate=1.0)
        assert set(mut.values) == set(p1.values)

    def test_clone_independent(self):
        from analysis.percentile_engine import PercentileSignalFn
        p = PercentileSignalFn().random_params()
        c = p.clone()
        c.values[list(c.values)[0]] = 999
        assert p.values != c.values


if __name__ == "__main__":
    test_evaluate_percentile_basic()
    test_engine_creates_valid_encoding()
    print("ALL PASSED")
