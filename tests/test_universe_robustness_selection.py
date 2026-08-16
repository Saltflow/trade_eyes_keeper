from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from src.search.config import StrategyConstraints, WindowStats
from src.search.contracts import (
    GateDecision,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
)
from src.search.validation import ValidationController
from src.search.workflow import _save_optimizer_result


def _search_result(
    candidate_id: str,
    score: float,
    parameters: dict[str, object] | None = None,
):
    return SimpleNamespace(
        candidate_id=candidate_id,
        parameters=dict(parameters or {}),
        ranking_stats=[],
        objective_score=score,
        selection_score=score,
        ranking_metrics={},
        gate_results=(),
    )


def test_universe_robustness_can_fall_back_from_fragile_first_place(monkeypatch):
    constraints = StrategyConstraints(
        {
            "validation": {
                "universe_robustness": {
                    "enabled": True,
                    "finalist_count": 2,
                    "penalty_weight": 2.0,
                    "activation_required": True,
                }
            }
        }
    )
    controller = ValidationController(
        strategy=SimpleNamespace(name="test"),
        constraints=constraints,
        wf_manager=SimpleNamespace(),
        evaluator=SimpleNamespace(),
        schema=ParameterSchema(()),
        ranking_service=SimpleNamespace(),
        gate_pipeline=SimpleNamespace(),
        all_windows=[],
    )
    monkeypatch.setattr(
        controller,
        "_sensitivity",
        lambda selected: {"drop": 0.0, "worst_score": selected.selection_score},
    )
    robustness = {
        "fragile": {"passed": False, "worst_drop": 0.1},
        "stable": {"passed": True, "worst_drop": 0.2},
    }
    monkeypatch.setattr(
        controller,
        "_universe_robustness",
        lambda selected: dict(robustness[selected.candidate_id]),
    )
    selected = []
    monkeypatch.setattr(
        controller,
        "_evaluate_full_windows",
        lambda result: selected.append(result.candidate_id),
    )

    results = controller.run(
        [_search_result("fragile", 10.0), _search_result("stable", 9.0)]
    )

    assert results[0].candidate_id == "stable"
    assert selected == ["stable"]
    assert results[1].universe_robustness["passed"] is False


def _local_sensitivity_controller(
    scores,
    *,
    raw_feasible=(True, True),
    gate_feasible=(True, True),
    minimum_feasible_ratio=0.80,
):
    constraints = StrategyConstraints(
        {
            "validation": {
                "local_sensitivity": {
                    "minimum_feasible_ratio": minimum_feasible_ratio,
                },
                "universe_robustness": {"enabled": False},
            }
        }
    )
    schema = ParameterSchema(
        (
            ParameterSpec(
                "x",
                ParameterKind.ORDINAL,
                values=(0, 1, 2),
            ),
        )
    )
    evaluated = SimpleNamespace(
        objective_scores=np.asarray(scores, dtype=np.float64),
        raw_metrics=tuple(
            {"gate_feasible": bool(value)} for value in gate_feasible
        ),
        feasible=np.asarray(raw_feasible, dtype=bool),
    )
    return ValidationController(
        strategy=SimpleNamespace(name="test"),
        constraints=constraints,
        wf_manager=SimpleNamespace(),
        evaluator=SimpleNamespace(),
        schema=schema,
        ranking_service=SimpleNamespace(
            evaluate_batch=lambda _batch: evaluated
        ),
        gate_pipeline=SimpleNamespace(
            evaluate=lambda metrics: GateDecision(metrics["gate_feasible"])
        ),
        all_windows=[],
    )


def test_negative_local_neighbour_scores_are_robust_when_feasible():
    controller = _local_sensitivity_controller((-2.0, -3.0))
    selected = controller._base_result(
        _search_result("base", -1.0, {"x": 1})
    )

    sensitivity = controller._sensitivity(selected)

    assert sensitivity["worst_score"] == -3.0
    assert sensitivity["drop"] == 2.0
    assert sensitivity["feasible_ratio"] == 1.0
    assert sensitivity["local_robustness_passed"] is True


def test_hard_gate_failure_is_counted_without_collapsing_finite_drop():
    controller = _local_sensitivity_controller(
        (-2.0, -100.0),
        gate_feasible=(True, False),
        minimum_feasible_ratio=0.50,
    )
    selected = controller._base_result(
        _search_result("base", -1.0, {"x": 1})
    )

    sensitivity = controller._sensitivity(selected)

    assert sensitivity["feasible_sample_count"] == 1
    assert sensitivity["infeasible_sample_count"] == 1
    assert sensitivity["worst_score"] == -2.0
    assert sensitivity["drop"] == 1.0
    assert sensitivity["local_robustness_passed"] is True


def test_local_feasible_ratio_is_configurable_and_validated():
    controller = _local_sensitivity_controller(
        (-2.0, -100.0),
        gate_feasible=(True, False),
        minimum_feasible_ratio=0.80,
    )
    selected = controller._base_result(
        _search_result("base", -1.0, {"x": 1})
    )

    assert controller._sensitivity(selected)["local_robustness_passed"] is False
    with pytest.raises(ValueError, match="minimum_feasible_ratio"):
        StrategyConstraints(
            {
                "validation": {
                    "local_sensitivity": {"minimum_feasible_ratio": 1.1}
                }
            }
        )


def test_all_infeasible_universe_finalists_preserve_metric_order(monkeypatch):
    constraints = StrategyConstraints(
        {
            "validation": {
                "local_sensitivity": {"enabled": False},
                "universe_robustness": {
                    "enabled": True,
                    "finalist_count": 2,
                },
            }
        }
    )
    controller = ValidationController(
        strategy=SimpleNamespace(name="test"),
        constraints=constraints,
        wf_manager=SimpleNamespace(),
        evaluator=SimpleNamespace(),
        schema=ParameterSchema(()),
        ranking_service=SimpleNamespace(),
        gate_pipeline=SimpleNamespace(),
        all_windows=[],
    )
    monkeypatch.setattr(
        controller,
        "_universe_robustness",
        lambda _selected: {"passed": False, "worst_drop": float("inf")},
    )
    selected = []
    monkeypatch.setattr(
        controller,
        "_evaluate_full_windows",
        lambda result: selected.append(result.candidate_id),
    )

    results = controller.run(
        [_search_result("a-first", 10.0), _search_result("z-second", 9.0)]
    )

    assert results[0].candidate_id == "a-first"
    assert selected == ["a-first"]


def test_negative_selection_score_can_pass_local_activation(tmp_path):
    controls = {"510880": 1.0, "510300": 2.0, "risk_free": 0.5}
    constraints = StrategyConstraints(
        {
            "benchmarks": {"a_share": list(controls)},
            "validation": {
                "local_sensitivity": {"activation_required": True},
                "universe_robustness": {"activation_required": False},
            },
        }
    )
    constraints.set_group("a_share")
    stat = WindowStats(
        strategy_return=4.0,
        benchmark_returns=controls,
        final_asset=104_000.0,
        final_shares=np.array([]),
        final_prices=np.array([]),
        cost_basis=np.array([]),
    )
    top = SimpleNamespace(
        parameters={},
        ranking_stats=[stat],
        validation_stats=[stat],
        purged_window_count=0,
        all_stats=[stat, stat],
        objective_score=-2.0,
        selection_score=-3.0,
        ranking_metrics={},
        sensitivity={
            "worst_score": -4.0,
            "drop": 2.0,
            "local_robustness_passed": True,
        },
        universe_robustness={},
        search_metadata={"gate_activation_eligible": True},
    )
    strategy = SimpleNamespace(
        name="test",
        execution_params=lambda _params: {"model": "cash_cap"},
    )

    _save_optimizer_result(
        [top],
        strategy,
        "a_share",
        output_dir=tmp_path,
        constraints=constraints,
    )

    saved = yaml.safe_load(
        (tmp_path / "a_share_best_params.yaml").read_text(encoding="utf-8")
    )
    assert saved["activation"]["local_robustness_passed"] is True
    assert saved["activation"]["eligible"] is True
    assert saved["search"]["selection_score"] == -3.0
