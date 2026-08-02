from types import SimpleNamespace

from src.search.config import StrategyConstraints
from src.search.contracts import ParameterSchema
from src.search.validation import ValidationController


def _search_result(candidate_id: str, score: float):
    return SimpleNamespace(
        candidate_id=candidate_id,
        parameters={},
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
