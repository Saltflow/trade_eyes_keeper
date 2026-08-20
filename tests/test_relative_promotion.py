from types import SimpleNamespace

import yaml

from src.search.promotion import (
    PromotionPolicy,
    compare_with_incumbent,
    record_promotion_decision,
)


GROUPS = ("a_share", "hk", "us")
BENCHMARKS = {"risk_free": 1.0, "broad": 2.0, "dividend": 3.0}


def _snapshot(total_return, drawdown=-10.0, trades=4, benchmarks=None):
    return {
        "total_return": total_return,
        "max_drawdown": drawdown,
        "trade_count": trades,
        "benchmark_returns": dict(benchmarks or BENCHMARKS),
    }


def test_relative_promotion_accepts_portfolio_improvement_without_unanimity():
    candidate = {
        "a_share": _snapshot(4.0),
        "hk": _snapshot(3.0),
        "us": _snapshot(-1.0),
    }
    incumbent = {group: _snapshot(0.0) for group in GROUPS}

    decision = compare_with_incumbent(candidate, incumbent, PromotionPolicy())

    assert decision.passed
    assert decision.improved_group_count == 2
    assert decision.portfolio_return_improvement_pct == 2.0


def test_relative_promotion_keeps_per_market_safety_floor():
    candidate = {
        "a_share": _snapshot(10.0),
        "hk": _snapshot(10.0),
        "us": _snapshot(-6.0),
    }
    incumbent = {group: _snapshot(0.0) for group in GROUPS}

    decision = compare_with_incumbent(candidate, incumbent, PromotionPolicy())

    assert not decision.passed
    assert "us:return_regression" in decision.reasons


def test_relative_promotion_rejects_mismatched_benchmark_slice():
    candidate = {group: _snapshot(2.0) for group in GROUPS}
    incumbent = {group: _snapshot(0.0) for group in GROUPS}
    incumbent["hk"] = _snapshot(
        0.0,
        benchmarks={"risk_free": 1.0, "broad": 2.1, "dividend": 3.0},
    )

    decision = compare_with_incumbent(candidate, incumbent, PromotionPolicy())

    assert not decision.passed
    assert "hk:benchmark_slice_mismatch" in decision.reasons


def test_promotion_record_preserves_absolute_gate_evidence(tmp_path):
    run_id = "candidate"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    summaries = {}
    for group in GROUPS:
        artifact = run_dir / f"{group}_best_params.yaml"
        artifact.write_text(
            yaml.safe_dump(
                {
                    "strategy_id": "technical_ensemble",
                    "activation": {
                        "eligible": False,
                        "holdout_passed": False,
                        "universe_robustness_passed": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        summaries[group] = SimpleNamespace(
            artifact=artifact.name,
            status="completed",
        )

    candidate = {group: _snapshot(2.0) for group in GROUPS}
    incumbent = {group: _snapshot(0.0) for group in GROUPS}
    policy = PromotionPolicy()
    decision = compare_with_incumbent(candidate, incumbent, policy)
    record_promotion_decision(
        run_id,
        summaries,
        decision,
        policy,
        incumbent_run_id="active",
        incumbent_strategy="percentile",
        root=tmp_path,
    )

    saved = yaml.safe_load(
        (run_dir / "a_share_best_params.yaml").read_text(encoding="utf-8")
    )
    assert saved["activation"]["eligible"] is True
    assert saved["activation"]["absolute_eligible"] is False
    assert saved["activation"]["holdout_passed"] is False
    assert saved["activation"]["universe_robustness_passed"] is False
    assert saved["promotion"]["passed"] is True
    assert saved["promotion"]["incumbent_run_id"] == "active"
