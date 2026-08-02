from types import SimpleNamespace

import numpy as np

from src.search.gates import CandidateGatePipeline, aggregate_ranking_metrics

A_SHARE_CONTROLS = ("510880", "510300", "risk_free")
NON_A_CONTROLS = ("VOO", "BRK.B", "risk_free")


def _window(strategy_return, risk_free, index_return, universe_return):
    benchmarks = {
        "risk_free": risk_free,
        "510300": index_return,
        "510880": universe_return,
    }
    return SimpleNamespace(
        strategy_return=strategy_return,
        benchmark_returns=benchmarks,
        test_excess_return=strategy_return - max(benchmarks.values()),
        avg_position_pct=80.0,
        max_drawdown_pct=-10.0,
        sharpe_ratio=1.0,
        total_trades=2,
    )


def test_majority_metrics_use_the_second_strongest_of_three_benchmarks():
    stats = [
        _window(5.0, 1.0, 4.0, 8.0),
        _window(3.0, 1.0, 2.0, 4.0),
        _window(0.0, 1.0, 2.0, -1.0),
    ]

    metrics = aggregate_ranking_metrics(
        stats, objective_score=0.0, control_benchmarks=A_SHARE_CONTROLS
    )

    assert metrics["mean_majority_benchmark_excess"] == 1.0 / 3.0
    assert metrics["majority_benchmark_win_windows"] == 2
    assert metrics["majority_benchmark_win_ratio"] == 2.0 / 3.0
    assert metrics["strongest_benchmark_win_windows"] == 0


def test_majority_metrics_fail_closed_when_a_control_is_missing():
    stat = _window(5.0, 1.0, 4.0, 8.0)
    del stat.benchmark_returns["510880"]

    metrics = aggregate_ranking_metrics(
        [stat], objective_score=0.0, control_benchmarks=A_SHARE_CONTROLS
    )

    assert np.isneginf(metrics["mean_majority_benchmark_excess"])
    assert metrics["majority_benchmark_win_windows"] == -1
    assert metrics["majority_benchmark_win_ratio"] == -1.0


def test_non_a_majority_uses_its_configured_controls_only():
    benchmarks = {
        "VOO": 6.0,
        "BRK.B": 2.0,
        "risk_free": 1.0,
        "510300": 99.0,
    }
    stat = SimpleNamespace(
        strategy_return=3.0,
        benchmark_returns=benchmarks,
        test_excess_return=-96.0,
        avg_position_pct=50.0,
        max_drawdown_pct=-5.0,
        sharpe_ratio=1.0,
        total_trades=1,
    )
    metrics = aggregate_ranking_metrics(
        [stat], 0.0, control_benchmarks=NON_A_CONTROLS
    )
    assert metrics["mean_majority_benchmark_excess"] == 1.0
    assert metrics["majority_benchmark_win_windows"] == 1


def test_standard_shape_accepts_six_positive_and_six_majority_windows():
    config = {
        "gate_profiles": {
            "standard": {
                "activation_eligible": True,
                "rules": [
                    {
                        "id": "positive",
                        "metric": "positive_return_windows",
                        "mode": "hard",
                        "operator": "ge",
                        "value": 6,
                    },
                    {
                        "id": "majority_mean",
                        "metric": "mean_majority_benchmark_excess",
                        "mode": "hard",
                        "operator": "gt",
                        "value": 0,
                    },
                    {
                        "id": "majority_windows",
                        "metric": "majority_benchmark_win_windows",
                        "mode": "hard",
                        "operator": "ge",
                        "value": 6,
                    },
                ],
            }
        }
    }
    pipeline = CandidateGatePipeline.from_config(config, "standard")

    passing = {
        "positive_return_windows": 6,
        "mean_majority_benchmark_excess": 0.01,
        "majority_benchmark_win_windows": 6,
    }
    assert pipeline.evaluate(passing).feasible
    assert not pipeline.evaluate({**passing, "positive_return_windows": 5}).feasible
    assert not pipeline.evaluate(
        {**passing, "majority_benchmark_win_windows": 5}
    ).feasible
