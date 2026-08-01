from types import SimpleNamespace

import pandas as pd
import pytest

from src.analysis.strategy_benchmark import (
    BenchmarkCandidate,
    aggregate_strategy_results,
    frame_fingerprint,
    select_benchmark_candidate,
    summarize_windows,
)


def _candidate(score, *, return_gate=False, hard=False):
    return BenchmarkCandidate(
        encoding=SimpleNamespace(),
        ranking_stats=[],
        wf_score=score,
        ranking_diagnostics={},
        return_gate_passed=return_gate,
        hard_constraints_passed=hard,
    )


def test_candidate_selection_prefers_eligible_pool():
    raw = _candidate(10.0)
    eligible = _candidate(2.0, return_gate=True, hard=True)

    selected, basis = select_benchmark_candidate([raw, eligible])

    assert selected is eligible
    assert basis == "best_eligible"


def test_candidate_selection_reports_raw_fallback():
    first = _candidate(-3.0)
    second = _candidate(-1.0, hard=True)

    selected, basis = select_benchmark_candidate([first, second])

    assert selected is second
    assert not selected.ranking_eligible
    assert basis == "best_raw_no_eligible"


def test_summarize_windows_keeps_core_contract():
    summary = summarize_windows(
        [
            {
                "strategy_return_pct": 8.0,
                "strongest_benchmark_excess_pct": 2.0,
                "max_drawdown_pct": -4.0,
                "sharpe_ratio": 1.2,
                "trade_count": 3,
                "final_asset": 108000.0,
            },
            {
                "strategy_return_pct": -2.0,
                "strongest_benchmark_excess_pct": -5.0,
                "max_drawdown_pct": -9.0,
                "sharpe_ratio": -0.2,
                "trade_count": 1,
                "final_asset": 98000.0,
            },
        ]
    )

    assert summary["mean_return_pct"] == pytest.approx(3.0)
    assert summary["mean_excess_pct"] == pytest.approx(-1.5)
    assert summary["worst_excess_pct"] == pytest.approx(-5.0)
    assert summary["winning_windows"] == 1
    assert summary["worst_drawdown_pct"] == pytest.approx(-9.0)
    assert summary["mean_sharpe"] == pytest.approx(0.5)
    assert summary["total_trades"] == 4
    assert summary["mean_final_asset"] == pytest.approx(103000.0)


def test_aggregate_strategy_results_is_equal_market_weighted():
    def market(name, group, ranking_excess, holdout_excess):
        return {
            "strategy_id": name,
            "market": group,
            "search_depth": 1000,
            "wf_score": ranking_excess,
            "selected_candidate": {"ranking_eligible": ranking_excess > 0},
            "ranking_summary": {
                "mean_return_pct": ranking_excess + 1,
                "mean_excess_pct": ranking_excess,
                "winning_windows": 6,
                "window_count": 11,
            },
            "holdout_summary": {
                "mean_return_pct": holdout_excess + 1,
                "mean_excess_pct": holdout_excess,
                "winning_windows": int(holdout_excess > 0),
                "window_count": 1,
            },
            "elapsed_seconds": 2.0,
        }

    rows = aggregate_strategy_results(
        [
            market("alpha", "a_share", 2.0, -1.0),
            market("alpha", "hk", 4.0, 2.0),
            market("alpha", "us", 6.0, 5.0),
        ]
    )

    assert len(rows) == 1
    assert rows[0]["mean_ranking_excess_pct"] == pytest.approx(4.0)
    assert rows[0]["mean_holdout_excess_pct"] == pytest.approx(2.0)
    assert rows[0]["ranking_wins"] == 18
    assert rows[0]["ranking_windows"] == 33
    assert rows[0]["holdout_wins"] == 2


def test_frame_fingerprint_changes_with_market_input():
    first = pd.DataFrame(
        {"date": ["2026-01-01"], "open": [1.0], "close": [2.0]}
    )
    same = first.copy()
    changed = first.copy()
    changed.loc[0, "close"] = 3.0

    assert frame_fingerprint(first) == frame_fingerprint(same)
    assert frame_fingerprint(first) != frame_fingerprint(changed)
