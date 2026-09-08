"""No-candidate and failure reports retain evidence without creating an active run."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

import main
from src.notification.email_notifier import build_optimizer_summary
from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary
from src.search.config import StrategyConstraints, get_market_optimizer_config
from src.search.run_diagnostics import persist_run_summary, summarize_ranking_archive
from src.search.workflow import run_optimizer
from src.strategy import get_strategy


def _record(market="hk", *, feasible=False):
    return {
        "market": market,
        "feasible": feasible,
        "gate_results": [
            {"rule_id": "return", "mode": "hard", "passed": feasible},
            {"rule_id": "drawdown", "mode": "hard", "passed": True},
            {"rule_id": "variance", "mode": "penalty", "passed": False},
        ],
    }


def test_archive_diagnostics_count_actual_evaluations_not_configured_budget(tmp_path):
    path = tmp_path / "hk_search_archive.jsonl"
    records = [_record(), _record(), _record(feasible=True), _record(market="us")]
    path.write_text(
        "\n".join(map(json.dumps, records)) + "\n{broken\n", encoding="utf-8"
    )

    result = summarize_ranking_archive(path, "hk")

    assert result == {
        "evaluated_count": 3,
        "ranking_feasible_count": 1,
        "invalid_record_count": 2,
        "hard_gate_failure_counts": {"return": 2},
    }


def test_receipt_is_single_market_escaped_and_not_an_activation_artifact(tmp_path):
    run_id = "20260908T010000_regime_pullback_hk"
    summary = OptimizerGroupSummary(
        group="hk",
        run_id=run_id,
        status="no_candidates",
        evaluated_count=10_000,
        ranking_diagnostics={"hard_gate_failure_counts": {"<return>": 10_000}},
    )
    report = OptimizerRunSummary(
        "regime_pullback",
        "test",
        "2026-09-08T01:00:00+08:00",
        600,
        {"hk": summary},
        run_id=run_id,
    )
    run_dir = tmp_path / run_id

    persist_run_summary(report, run_dir)

    payload = yaml.safe_load((run_dir / "run_summary.yaml").read_text("utf-8"))
    assert payload["schema_version"] == 1
    assert set(payload["groups"]) == {"hk"}
    assert not payload["candidate"] and not payload["activated"]
    assert not (run_dir / "manifest.yaml").exists()
    html = (run_dir / "hk_run_status.html").read_text("utf-8")
    assert "&lt;return&gt;" in html and "<return>" not in html
    body = build_optimizer_summary(report)
    assert "搜索已完成 10,000" in body
    assert "中止前" not in body
    assert "&lt;return&gt;: 10,000" in body

    report.groups["us"] = OptimizerGroupSummary(group="us")
    with pytest.raises(ValueError, match="one matching run"):
        persist_run_summary(report, run_dir)


@pytest.mark.parametrize("outcome", ["no_symbols", "no_candidates", "failed"])
def test_main_persists_terminal_report_before_cleanup(tmp_path, monkeypatch, outcome):
    config = main.load_config()
    config.pop("point_in_time_data", None)
    config["stocks"] = (
        [] if outcome == "no_symbols" else ["00883", "01816", "00700", "00728", "01339"]
    )
    market_config = get_market_optimizer_config("hk", config)
    monkeypatch.chdir(tmp_path)
    history = pd.DataFrame(
        {"date": pd.date_range("2026-01-01", periods=2), "close": [10, 11]}
    )

    class FixtureDataSource:
        def __init__(self, config):
            pass

        def fetch_stock_data(self, code, days):
            return history.copy()

    def search(*args, output_dir, _constraints, **kwargs):
        if outcome == "failed":
            raise RuntimeError("PIT benchmark corporate action missing")
        (output_dir / "hk_search_archive.jsonl").write_text(
            json.dumps(_record()) + "\n", encoding="utf-8"
        )
        return [], _constraints

    prune_calls = []

    def prune(**kwargs):
        if not kwargs.get("protected_run_ids"):
            assert list(Path("data/optimizer/runs").glob("*/run_summary.yaml"))
        prune_calls.append(kwargs)

    monkeypatch.setattr("src.data.data_source.DataSource", FixtureDataSource)
    monkeypatch.setattr(main, "_has_optimizer_history", lambda *args: True)
    monkeypatch.setattr(main, "_load_optimizer_benchmarks", lambda *args: {})
    monkeypatch.setattr(main, "run_optimizer", search)
    monkeypatch.setattr(main, "prune_optimizer_runs", prune)
    reports = []

    completed = main._run_optimization_group(
        config, "hk", market_config, report_sink=reports
    )

    assert completed == ({"hk": 0} if outcome == "no_candidates" else {})
    assert len(reports) == 1
    report = reports[0]
    assert report.groups["hk"].status == outcome
    assert not report.activated and not report.candidate
    run_dir = Path("data/optimizer/runs") / report.run_id
    assert (run_dir / "run_summary.yaml").exists()
    assert (run_dir / "hk_run_status.html").exists()
    assert not Path("data/optimizer/latest_strategy.yaml").exists()
    if outcome == "failed":
        assert "PIT benchmark corporate action missing" in report.failure_reason
    if outcome == "no_candidates":
        assert report.groups["hk"].evaluated_count == 1
        assert report.groups["hk"].ranking_diagnostics["hard_gate_failure_counts"] == {
            "return": 1
        }
    assert len(prune_calls) == (0 if outcome == "no_symbols" else 2)


def test_workflow_saves_solver_gate_contracts_even_when_all_candidates_fail(tmp_path):
    raw = {
        "benchmarks": {"a_share": ["510880", "510300", "risk_free"]},
        "walk_forward": {
            "train_months": 6,
            "test_months": 3,
            "step_months": 2,
            "num_windows": 3,
            "validation_windows": 1,
            "purge_overlapping_windows": False,
            "window_weights": [1, 1],
        },
        "genetic_search": {"sensitivity_top_candidates": 1, "sensitivity_samples": 2},
        "search": {
            "solver_id": "random",
            "gate_profile": "reject_all",
            "workers": 1,
            "checkpoint": False,
            "solvers": {"random": {"budget": 3, "random_seed": 11}},
        },
        "gate_profiles": {
            "reject_all": {
                "activation_eligible": False,
                "rules": [
                    {
                        "id": "return",
                        "metric": "weighted_strategy_return",
                        "mode": "hard",
                        "operator": "ge",
                        "value": 1_000_000,
                    }
                ],
            },
        },
    }
    history = pd.DataFrame(
        {
            "date": pd.date_range("2023-01-02", periods=520, freq="B"),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 100_000,
        }
    )

    results, _ = run_optimizer(
        get_strategy("percentile"),
        {"510880": history},
        ["510880"],
        "a_share",
        _constraints=StrategyConstraints(raw),
        output_dir=tmp_path,
        benchmark_data={"510880": history, "510300": history},
    )

    assert results == []
    diagnostics = yaml.safe_load(
        (tmp_path / "a_share_search_diagnostics.yaml").read_text("utf-8")
    )
    assert diagnostics["status"] == "no_candidates"
    assert diagnostics["evaluated_count"] == 3
    assert diagnostics["hard_gate_failure_counts"] == {"return": 3}
    assert diagnostics["search"]["solver_id"] == "random"
    assert diagnostics["search"]["gate_contract"]["profile_id"] == "reject_all"
    assert diagnostics["search"]["parameter_schema_hash"]
    assert not (tmp_path / "a_share_best_params.yaml").exists()
