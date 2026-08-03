from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import yaml

import src.search.evaluator as evaluator_module

from src.search.config import StrategyConstraints
from src.search.contracts import (
    Candidate,
    CandidateBatch,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
)
from src.search.evaluator import EvaluationService
from src.search.workflow import run_optimizer
from src.strategy import get_strategy


def _history(rows=520):
    dates = pd.date_range("2023-01-02", periods=rows, freq="B")
    close = 20 + np.linspace(0, 8, rows) + np.sin(np.arange(rows) / 12)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
            "volume": 100_000 + (np.arange(rows) % 13) * 1_000,
        }
    )


def test_run_optimizer_uses_configured_solver_and_persists_contracts(tmp_path):
    raw = {
        "benchmarks": {
            "a_share": ["510880", "510300", "risk_free"],
            "risk_free_rates": {"a_share": 0.02},
        },
        "walk_forward": {
            "train_months": 6,
            "test_months": 3,
            "step_months": 2,
            "num_windows": 3,
            "validation_windows": 1,
            "purge_overlapping_windows": False,
            "window_weights": [1, 1],
        },
        "genetic_search": {
            "sensitivity_top_candidates": 1,
            "sensitivity_samples": 2,
            "evaluation_workers": 1,
        },
        "search": {
            "solver_id": "random",
            "gate_profile": "test_off",
            "batch_size": 128,
            "workers": 1,
            "checkpoint": False,
            "solvers": {"random": {"budget": 3, "random_seed": 11}},
        },
        "gate_profiles": {"test_off": {"activation_eligible": False, "rules": []}},
        "simplified_search": {
            "buy_limit_levels": [10_000, 20_000],
            "sell_limit_levels": [10_000, 20_000],
        },
        "execution_params": {
            "initial_capital": 100_000,
            "commission_rate": 0.005,
            "min_holding_days": 30,
            "lot_sizes": {"a_share": 100},
            "fx_rates": {"a_share": 1.0},
        },
    }
    history = _history()
    results, _constraints = run_optimizer(
        get_strategy("percentile"),
        {"510880": history},
        ["510880"],
        group="a_share",
        _constraints=StrategyConstraints(raw),
        output_dir=tmp_path,
        benchmark_data={"510880": history, "510300": history},
    )

    assert results
    artifact = yaml.safe_load(
        (tmp_path / "a_share_best_params.yaml").read_text(encoding="utf-8")
    )
    assert artifact["solver_id"] == "random"
    assert artifact["gate_profile"] == "test_off"
    assert artifact["activation"]["eligible"] is False
    assert {
        "search_contract_hash",
        "parameter_schema_hash",
        "feature_contract_hash",
        "data_contract_hash",
        "execution_contract_hash",
        "window_contract_hash",
        "gate_profile_hash",
    }.issubset(artifact["contracts"])
    serialized = yaml.safe_dump(artifact)
    assert "optimizer_version" not in serialized
    assert (
        "holdout"
        not in (tmp_path / "a_share_search_archive.jsonl")
        .read_text(encoding="utf-8")
        .lower()
    )


def test_process_candidate_workers_match_scalar_results(tmp_path):
    def run(workers: int, backend: str, output_name: str):
        raw = {
            "benchmarks": {
                "a_share": ["510880", "510300", "risk_free"],
                "risk_free_rates": {"a_share": 0.02},
            },
            "walk_forward": {
                "train_months": 6,
                "test_months": 3,
                "step_months": 2,
                "num_windows": 3,
                "validation_windows": 1,
                "purge_overlapping_windows": False,
                "window_weights": [1, 1],
            },
            "genetic_search": {
                "sensitivity_top_candidates": 1,
                "sensitivity_samples": 2,
                "evaluation_workers": workers,
            },
            "search": {
                "solver_id": "random",
                "gate_profile": "test_off",
                "batch_size": 128,
                "parallel_axis": "candidate_window",
                "evaluation_backend": backend,
                "workers": workers,
                "checkpoint": False,
                "solvers": {"random": {"budget": 4, "random_seed": 29}},
            },
            "gate_profiles": {
                "test_off": {"activation_eligible": False, "rules": []}
            },
            "simplified_search": {
                "buy_limit_levels": [10_000, 20_000],
                "sell_limit_levels": [10_000, 20_000],
            },
            "execution_params": {
                "initial_capital": 100_000,
                "commission_rate": 0.005,
                "min_holding_days": 30,
                "lot_sizes": {"a_share": 100},
                "fx_rates": {"a_share": 1.0},
            },
        }
        history = _history()
        results, _constraints = run_optimizer(
            get_strategy("percentile"),
            {"510880": history},
            ["510880"],
            group="a_share",
            _constraints=StrategyConstraints(raw),
            output_dir=tmp_path / output_name,
            benchmark_data={"510880": history, "510300": history},
        )
        return results

    scalar = run(1, "scalar", "scalar")
    process = run(2, "process", "process")

    assert [item.candidate_id for item in process] == [
        item.candidate_id for item in scalar
    ]
    assert [item.parameters for item in process] == [
        item.parameters for item in scalar
    ]
    assert [item.objective_score for item in process] == [
        item.objective_score for item in scalar
    ]
    assert [item.ranking_metrics for item in process] == [
        item.ranking_metrics for item in scalar
    ]


def test_scalar_ranking_cache_is_compact_until_materialized(monkeypatch):
    schema = ParameterSchema(
        (ParameterSpec("x", ParameterKind.ORDINAL, values=(0, 1)),)
    )
    candidate = Candidate.create({"x": 0}, schema, "test")
    batch = CandidateBatch.from_candidates([candidate], schema)
    ranking_stat = SimpleNamespace(marker="ranking")
    calls = []

    def fake_evaluate(*_args, **_kwargs):
        calls.append("evaluate")
        return [ranking_stat], [ranking_stat], [], 1.25

    monkeypatch.setattr(evaluator_module, "_evaluate_params_wf", fake_evaluate)
    monkeypatch.setattr(
        evaluator_module,
        "aggregate_ranking_metrics",
        lambda *_args, **_kwargs: {"objective_score": 1.25},
    )
    constraints = SimpleNamespace(
        walk_forward=SimpleNamespace(
            ranking_weights=lambda count: [1.0] * count
        ),
        benchmark_codes=(),
    )
    service = EvaluationService(
        SimpleNamespace(name="stub", window_state_scope="train"),
        constraints,
        SimpleNamespace(),
        SimpleNamespace(),
        [SimpleNamespace()],
        workers=1,
        evaluation_backend="scalar",
    )

    evaluated = service.evaluate_batch(batch)
    compact = service.records[candidate.candidate_id]

    assert evaluated.objective_scores.tolist() == [1.25]
    assert compact.parameters == {}
    assert compact.all_stats == []
    assert compact.ranking_stats == []
    assert compact.materialized is False

    service.materialize_batch(batch)
    complete = service.records[candidate.candidate_id]

    assert complete.parameters == {"x": 0}
    assert complete.all_stats == [ranking_stat]
    assert complete.ranking_stats == [ranking_stat]
    assert complete.materialized is True
    assert calls == ["evaluate", "evaluate"]
