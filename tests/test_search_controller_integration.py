from __future__ import annotations

import numpy as np
import pandas as pd
import yaml

from src.search.config import StrategyConstraints
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
            "a_share": ["510300", "risk_free"],
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
        benchmark_data={"510300": history},
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
