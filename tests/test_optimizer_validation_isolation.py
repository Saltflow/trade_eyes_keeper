"""Regression tests for the optimizer's held-out daily-report window."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.config import GeneticSearchConfig, StrategyConstraints, WindowStats
from src.analysis.backtester import (
    FastEvaluator,
    WalkForwardManager,
    WindowSlice,
    _compute_stats,
    simulate_portfolio,
)
from src.analysis.config import ExecutionConfig
from src.analysis.search_interface import StrategyMarketData, TradePlan
from src.analysis.optimizer import (
    GeneticOptimizer,
    ScoredEncoding,
    StrategyEncoding,
    _compute_ranking_wf_score,
    _passes_ranking_return_gate,
    _partition_window_indexes,
    _ranking_return_diagnostics,
    _save_optimizer_result,
    _split_ranking_and_validation_stats,
)
from src.analysis.search_interface import ParamDim, ParamSpace, Params
from src.analysis.strategy_artifacts import OptimizerGroupSummary, persist_group_summary
from main import _min_optimizer_history_rows, _optimizer_lookback_days


def _constraints() -> StrategyConstraints:
    return StrategyConstraints(
        {
            "walk_forward": {
                "num_windows": 3,
                "validation_windows": 1,
                "window_weights": [1.0, 2.0],
                "stability_penalty": 0.5,
            },
            "genetic_search": {
                "phase1_random_samples": 1,
                "phase1_top_keep": 1,
                "num_generations": 0,
                "population_size": 1,
                "offspring_size": 0,
                "min_weighted_strategy_return": -999.0,
                "min_positive_return_windows": 0,
            },
        }
    )


def test_ranking_score_excludes_held_out_window_and_extends_weights():
    constraints = _constraints()
    stats = [
        WindowStats(test_excess_return=2.0),
        WindowStats(test_excess_return=8.0),
        WindowStats(test_excess_return=-999.0),
    ]

    ranking, validation = _split_ranking_and_validation_stats(stats, constraints)

    assert ranking == stats[:2]
    assert validation == stats[2:]
    # Weighted mean = (2 * 1 + 8 * 2) / 3 = 6; stability penalty = 1.5;
    # default Sharpe shortfall adds a 0.15 soft penalty.
    assert _compute_ranking_wf_score(ranking, constraints) == pytest.approx(4.35)
    assert constraints.walk_forward.ranking_window_count == 2
    # A short legacy weight array must not silently discard a historical window.
    assert constraints.walk_forward.ranking_weights(4) == [1.0, 2.0, 2.0, 2.0]


def test_absolute_return_gate_uses_ranking_stats_not_held_out_window():
    constraints = _constraints()
    constraints.genetic_search.min_weighted_strategy_return = 0.0
    constraints.genetic_search.min_positive_return_windows = 1
    ranking = [
        WindowStats(strategy_return=1.0),
        WindowStats(strategy_return=-0.25),
    ]
    held_out = [WindowStats(strategy_return=-999.0)]

    diagnostics = _ranking_return_diagnostics(ranking, constraints)

    assert diagnostics == {
        "weighted_strategy_return": pytest.approx(1.0 / 6.0),
        "positive_return_windows": 1,
        "ranking_window_count": 2,
    }
    assert _passes_ranking_return_gate(diagnostics, constraints)
    assert held_out[0].strategy_return == -999.0


def test_absolute_return_gate_rejects_positive_excess_with_negative_strategy_return():
    constraints = _constraints()
    constraints.genetic_search.min_weighted_strategy_return = 0.0
    constraints.genetic_search.min_positive_return_windows = 1
    ranking = [WindowStats(test_excess_return=8.0, strategy_return=-0.1)]

    diagnostics = _ranking_return_diagnostics(ranking, constraints)

    assert diagnostics["weighted_strategy_return"] == pytest.approx(-0.1)
    assert not _passes_ranking_return_gate(diagnostics, constraints)


class _FakeStrategy:
    name = "fake"
    param_space = ParamSpace([ParamDim("level", 3)])

    def random_perturbations(self, params, n=10):
        return []


def test_ga_hard_constraints_receive_ranking_stats_only(monkeypatch):
    """A bad held-out window must not remove a candidate from the GA pool."""
    constraints = _constraints()
    ranking = [WindowStats(test_excess_return=3.0, avg_position_pct=20.0)]
    validation = [WindowStats(test_excess_return=-999.0, avg_position_pct=0.0)]
    received = []

    def fake_check(window_stats, score):
        received.append((window_stats, score))
        return True, []

    constraints.check_hard_constraints = fake_check

    def fake_evaluate(*_args, **_kwargs):
        return ranking + validation, ranking, validation, 3.0

    monkeypatch.setattr("src.analysis.optimizer._evaluate_encoding_wf", fake_evaluate)
    optimizer = GeneticOptimizer(
        _FakeStrategy(),
        constraints,
        SimpleNamespace(iter_windows=lambda: [object(), object()]),
        object(),
    )

    results = optimizer.run()

    assert len(results) == 1
    assert received == [(ranking, 3.0)]
    assert results[0].ranking_stats == ranking
    assert results[0].validation_stats == validation


def test_sensitivity_reads_only_ranking_windows(monkeypatch):
    constraints = _constraints()
    strategy = _FakeStrategy()
    strategy.random_perturbations = lambda _params, n=10: [
        Params(values={"level": 1}, _engine="fake")
    ]
    optimizer = GeneticOptimizer(strategy, constraints, SimpleNamespace(), object())
    selected = ScoredEncoding(
        StrategyEncoding(
            genome=[1], engine_name="fake", params=Params({"level": 1}, "fake")
        ),
        wf_stats=[WindowStats(), WindowStats(), WindowStats()],
        wf_score=2.0,
        ranking_stats=[WindowStats(), WindowStats()],
        validation_stats=[WindowStats(test_excess_return=-999.0)],
    )
    windows = ["rank-1", "rank-2", "held-out"]
    calls = []

    def fake_evaluate(*args, **kwargs):
        calls.append((args[2], kwargs.get("validation_window_count")))
        return [], [], [], 1.25

    monkeypatch.setattr("src.analysis.optimizer._evaluate_encoding_wf", fake_evaluate)

    sensitivity = optimizer._evaluate_sensitivity(selected, windows)

    assert calls == [(["rank-1", "rank-2"], 0)]
    assert sensitivity["base_score"] == 2.0
    assert sensitivity["worst_score"] == 1.25


def test_robust_selection_can_choose_the_51st_base_wf_candidate():
    optimizer = object.__new__(GeneticOptimizer)
    optimizer.ga_cfg = SimpleNamespace(
        sensitivity_top_candidates=51,
        sensitivity_penalty_weight=1.0,
    )
    candidates = [
        ScoredEncoding(
            StrategyEncoding(genome=[index], engine_name="fake"),
            wf_stats=[],
            wf_score=float(100 - index),
        )
        for index in range(51)
    ]

    def fake_sensitivity(candidate, _windows):
        index = candidate.encoding.genome[0]
        return {"drop": 0.0 if index == 50 else 100.0}

    optimizer._evaluate_sensitivity = fake_sensitivity

    selected = optimizer._apply_robust_selection(candidates, ["ranking-only"])

    assert selected[0].encoding.genome == [50]
    assert selected[0].selection_score == pytest.approx(50.0)
    assert selected[0].sensitivity["selection_score"] == pytest.approx(50.0)


def test_default_full_sensitivity_penalty_is_applied_exactly():
    optimizer = object.__new__(GeneticOptimizer)
    optimizer.ga_cfg = GeneticSearchConfig({"sensitivity_top_candidates": 1})
    candidate = ScoredEncoding(
        StrategyEncoding(genome=[0], engine_name="fake"),
        wf_stats=[],
        wf_score=5.0,
    )
    optimizer._evaluate_sensitivity = lambda *_args: {"drop": 2.25}

    selected = optimizer._apply_robust_selection([candidate], ["ranking-only"])

    assert optimizer.ga_cfg.sensitivity_penalty_weight == 1.0
    assert selected[0].selection_score == pytest.approx(5.0 - 1.0 * 2.25)


def test_persisted_summary_converts_numpy_scalars_to_yaml(tmp_path):
    run_id = "run"
    artifact = tmp_path / "runs" / run_id / "a_share_best_params.yaml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        yaml.safe_dump({"engine": "fake", "params": {"level": 1}}),
        encoding="utf-8",
    )
    summary = OptimizerGroupSummary(
        group="a_share",
        artifact=artifact.name,
        sensitivity={"worst_score": np.float64(97.7)},
        validation={"latest_holdings": {"pos_pct": np.float64(25.0)}},
    )

    persist_group_summary(run_id, summary, root=tmp_path)

    saved = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    assert saved["sensitivity"]["worst_score"] == 97.7
    assert saved["validation"]["latest_holdings"]["pos_pct"] == 25.0


def test_initial_optimizer_artifact_is_safe_yaml_with_numpy_scalars(tmp_path):
    stat = WindowStats(
        strategy_return=np.float64(8.5),
        test_excess_return=np.float64(3.25),
        max_drawdown_pct=np.float64(-4.0),
        sharpe_ratio=np.float64(1.1),
        total_trades=np.int64(7),
        initial_asset=np.float64(100_000.0),
        final_asset=np.float64(108_500.0),
        final_cash=np.float64(8_500.0),
        final_position_pct=np.float64(92.17),
        final_shares=np.array([100.0]),
        final_prices=np.array([1_000.0]),
        cost_basis=np.array([900.0]),
        benchmark_returns={"510300": np.float64(5.25)},
    )
    strategy = SimpleNamespace(
        name="fake",
        param_space=SimpleNamespace(dims=[]),
        execution_params=lambda _params: {"buy_cash": np.float64(10_000.0)},
    )
    top = SimpleNamespace(
        encoding=StrategyEncoding(genome=[], engine_name="fake"),
        ranking_stats=[stat],
        validation_stats=[stat],
        purged_window_count=0,
        wf_stats=[stat, stat],
        wf_score=np.float64(2.5),
        selection_score=np.float64(2.0),
        ranking_diagnostics={"weighted_return": np.float64(8.5)},
        sensitivity={"drop": np.float64(0.5)},
    )

    _save_optimizer_result(
        [top],
        strategy,
        "a_share",
        output_dir=tmp_path,
        constraints=_constraints(),
        strategy_codes=["510300"],
    )

    artifact = tmp_path / "a_share_best_params.yaml"
    saved = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    assert saved["strategy_id"] == "fake"
    assert saved["ranking_windows"][0]["benchmark_returns"] == {"510300": 5.25}
    assert saved["execution"]["buy_cash"] == 10_000.0
    assert "python/object" not in artifact.read_text(encoding="utf-8")


def test_optimizer_history_preflight_requires_the_full_configured_horizon():
    constraints = StrategyConstraints(
        {
            "walk_forward": {
                "train_months": 12,
                "test_months": 9,
                "step_months": 3,
                "num_windows": 14,
            }
        }
    )

    # The final (14th) window must include its complete test horizon, not
    # merely reach its start.  Otherwise a short-history symbol can reduce a
    # market to 12 ranking windows plus a partial held-out window.
    assert _min_optimizer_history_rows(constraints) == 1260
    # The calendar lookback must retain margin beyond the exact WF horizon;
    # otherwise date intersections can leave a market a few trading days short.
    assert _optimizer_lookback_days(constraints) == 2006


def test_optimizer_lookback_honors_configured_history_years():
    constraints = StrategyConstraints(
        {"walk_forward": {"num_windows": 14, "data_years": 7}}
    )

    assert _optimizer_lookback_days(constraints) == int(7 * 365.25) + 180


def test_walk_forward_holdout_is_anchored_to_the_newest_market_date():
    constraints = StrategyConstraints(
        {
            "walk_forward": {
                "train_months": 12,
                "test_months": 9,
                "step_months": 3,
                "num_windows": 14,
                "validation_windows": 1,
            }
        }
    )
    manager = object.__new__(WalkForwardManager)
    manager.wf_config = constraints.walk_forward
    # Includes extra rows for rolling indicators.  The newest held-out window
    # must still end at the most recent row, rather than before that buffer.
    manager.T = 1_350
    manager.dates = pd.bdate_range("2020-01-01", periods=manager.T)

    windows = manager.iter_windows()

    assert len(windows) == 14
    assert windows[-1].test_end == manager.T
    assert pd.Timestamp(windows[0].train_start_date) + pd.DateOffset(
        months=12
    ) == pd.Timestamp(windows[0].test_start_date)
    assert pd.Timestamp(windows[-1].test_start_date) + pd.DateOffset(
        months=9
    ) - pd.Timedelta(days=1) == pd.Timestamp(windows[-1].test_end_date)


def test_overlapping_windows_are_purged_from_strict_holdout_ranking():
    constraints = StrategyConstraints(
        {
            "walk_forward": {
                "num_windows": 14,
                "validation_windows": 1,
                "purge_overlapping_windows": True,
            }
        }
    )
    windows = [
        WindowSlice(0, 0, 301 + index * 63, 490 + index * 63, index)
        for index in range(14)
    ]

    ranking, purged, validation = _partition_window_indexes(windows, constraints)

    assert ranking == list(range(11))
    assert purged == [11, 12]
    assert validation == [13]
    validation_start = windows[validation[0]].test_start
    assert all(windows[index].test_end <= validation_start for index in ranking)
    assert all(windows[index].test_end > validation_start for index in purged)


def test_fast_optimizer_scores_primary_benchmark_excess_return():
    stats = _compute_stats(
        np.array([100.0, 110.0]),
        np.array([[10.0], [11.0]]),
        np.array([100.0, 100.1]),
        trade_count=1,
        signal_count=0,
        benchmark_series={"510880": np.array([100.0, 105.0])},
    )

    assert stats.strategy_return == pytest.approx(10.0)
    assert stats.benchmark_returns == {"510880": 5.0}
    assert stats.test_excess_return == pytest.approx(5.0)


def test_optimizer_execution_matches_daily_portfolio_execution():
    prices = np.array(
        [[10.0], [11.0], [12.0], [11.0], [10.0], [12.0]], dtype=np.float32
    )
    buy = np.array([[True], [False], [False], [False], [True], [False]])
    sell = np.array([[False], [False], [False], [True], [False], [False]])
    execution = ExecutionConfig(
        initial_capital=100000.0,
        monthly_buy_limit=15000.0,
        commission_rate=0.005,
        min_holding_days=30,
        lot_sizes={"a_share": 100},
    )
    evaluator = FastEvaluator(execution, "a_share")
    plan = TradePlan(
        buy_signals=buy,
        sell_signals=sell,
        buy_priority=np.where(buy, 1.0, -np.inf),
        sell_priority=np.where(sell, 1.0, -np.inf),
        buy_cash_limit=45_000.0,
        sell_cash_limit=45_000.0,
        warmup_rows=0,
    )
    fast = evaluator.evaluate(
        np.zeros((len(prices), 1, 16), dtype=np.float32),
        prices,
        np.full(len(prices), 100000.0),
        trade_plan=plan,
    )
    daily = simulate_portfolio(
        plan,
        StrategyMarketData(
            indicator_matrix=np.empty((*prices.shape, 0), dtype=np.float32),
            dates=[
                f"2026-01-{index + 1:02d}" for index in range(len(prices))
            ],
            symbols=["000001"],
            prices=prices,
            tradable=np.ones_like(prices, dtype=bool),
        ),
        initial_cash=100000.0,
        lot_size=100,
        commission_rate=0.005,
        min_holding_days=30,
    )

    assert fast.total_trades == daily.total_trades
    assert fast.strategy_return == pytest.approx(daily.total_return_pct)
    assert fast.max_drawdown_pct == pytest.approx(daily.max_drawdown_pct)
