"""Strict v4 candidate publication and independent market activation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

import main
from src.search.artifacts import (
    OptimizerGroupSummary,
    activate_run,
    load_latest_strategy_run,
    publish_complete_run,
)

GROUPS = ("a_share", "hk", "us")
SOLVERS = {
    "a_share": "local_genetic",
    "hk": "simulated_annealing",
    "us": "random",
}


def _candidate(
    root,
    run_id,
    group,
    strategy="regime_pullback",
    eligible=True,
    holdout_excesses=None,
):
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config_hash = f"hash-{run_id}-{group}"
    artifact = run_dir / f"{group}_best_params.yaml"
    artifact.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "group": group,
                "strategy_id": strategy,
                "solver_id": SOLVERS[group],
                "gate_profile": "standard",
                "market_config_hash": config_hash,
                "params": {"adx_min": 1},
                "execution": {"model": "cash_cap"},
                "activation": {
                    "eligible": eligible,
                    "holdout_passed": eligible,
                },
                "holdout_windows": [
                    {
                        "majority_benchmark_excess": value,
                    }
                    for value in (
                        holdout_excesses
                        if holdout_excesses is not None
                        else [1.0 if eligible else -1.0]
                    )
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return publish_complete_run(
        run_id,
        strategy,
        "2026-07-30T20:00:00",
        {
            group: OptimizerGroupSummary(
                group=group,
                status="completed",
                artifact=artifact.name,
                strategy_name=strategy,
                solver_id=SOLVERS[group],
                gate_profile="standard",
                market_config_hash=config_hash,
            )
        },
        required_groups=(group,),
        all_groups=(group,),
        root=root,
        activate=False,
        strategy_by_group={group: strategy},
    )


def _legacy_market(root, run_id, group, strategy="percentile"):
    """Write one pre-v4 market artifact and return its index-relative path."""
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact = run_dir / f"{group}_best_params.yaml"
    artifact.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "group": group,
                "strategy_id": strategy,
                "params": {"adx_min": 1},
                "execution": {"model": "cash_cap"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return f"runs/{run_id}/{artifact.name}"


def _legacy_pointer(root, run_id, artifacts, strategy="percentile"):
    """Write one pre-v4 pointer that names an artifact per market."""
    pointers = {group: {"artifact": path} for group, path in artifacts.items()}
    (root / "latest_strategy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "activated": True,
                "run_id": run_id,
                "strategy": strategy,
                "timestamp": "2026-07-30T04:11:38.145386",
                "groups": pointers,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_market_activation_is_independent_and_preserves_other_pointers(tmp_path):
    for group in GROUPS:
        assert _candidate(tmp_path, f"baseline_{group}", group)
        assert activate_run(f"baseline_{group}", group=group, root=tmp_path)

    assert _candidate(tmp_path, "candidate_a", "a_share", "technical_ensemble")
    assert activate_run("candidate_a", group="a_share", root=tmp_path)

    active = load_latest_strategy_run(groups=GROUPS, root=tmp_path)
    assert active is not None
    assert active.strategy_for("a_share").name == "technical_ensemble"
    assert active.strategy_for("hk").name == "regime_pullback"
    assert active.strategy_for("us").name == "regime_pullback"
    assert active.run_id_for("a_share") == "candidate_a"
    assert active.run_id_for("hk") == "baseline_hk"
    assert active.run_id_for("us") == "baseline_us"


def test_partial_active_pointer_is_fail_closed_for_full_market_view(tmp_path):
    assert _candidate(tmp_path, "candidate_a", "a_share")
    assert activate_run("candidate_a", group="a_share", root=tmp_path)
    assert load_latest_strategy_run(groups=("a_share",), root=tmp_path) is not None
    assert load_latest_strategy_run(groups=GROUPS, root=tmp_path) is None


def test_ineligible_candidate_cannot_activate(tmp_path):
    assert _candidate(tmp_path, "failed_holdout", "a_share", eligible=False)
    assert not activate_run("failed_holdout", group="a_share", root=tmp_path)
    assert load_latest_strategy_run(groups=("a_share",), root=tmp_path) is None


def test_four_holdout_windows_can_activate_one_market(tmp_path):
    assert _candidate(
        tmp_path,
        "four_holdout_windows",
        "a_share",
        holdout_excesses=[0.5, 1.0, 0.25, 2.0],
    )
    assert activate_run(
        "four_holdout_windows",
        group="a_share",
        root=tmp_path,
    )
    assert load_latest_strategy_run(groups=("a_share",), root=tmp_path) is not None


def test_old_mixed_manifest_is_not_read_as_current_active(tmp_path):
    path = tmp_path / "latest_strategy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "activated": True,
                "strategy": "mixed",
                "groups": {"a_share": {"strategy": "percentile"}},
            }
        ),
        encoding="utf-8",
    )
    assert load_latest_strategy_run(groups=("a_share",), root=tmp_path) is None


def test_activate_run_requires_one_explicit_market():
    assert not activate_run("candidate")


def test_activate_run_cli_requires_market_group():
    parser = main._build_argument_parser()
    args = parser.parse_args(["--activate-run", "candidate"])
    assert args.activate_run == "candidate"
    assert args.market_group is None


def test_legacy_pointer_still_resolves_every_market(tmp_path):
    artifacts = {
        group: _legacy_market(tmp_path, "legacy_run", group) for group in GROUPS
    }
    _legacy_pointer(tmp_path, "legacy_run", artifacts)

    active = load_latest_strategy_run(groups=GROUPS, root=tmp_path)

    assert active is not None
    assert active.run_id == "legacy_run"
    for group in GROUPS:
        assert active.strategy_for(group).name == "percentile"
        assert active.run_id_for(group) == "legacy_run"
        assert active.params_by_group[group].values == {"adx_min": 1}
        assert active.solver_by_group[group] == ""
        assert active.config_hash_by_group[group] == ""


def test_activation_carries_legacy_markets_forward(tmp_path):
    artifacts = {
        group: _legacy_market(tmp_path, "legacy_run", group) for group in GROUPS
    }
    _legacy_pointer(tmp_path, "legacy_run", artifacts)

    assert _candidate(tmp_path, "candidate_a", "a_share", "technical_ensemble")
    assert activate_run("candidate_a", group="a_share", root=tmp_path)

    index = yaml.safe_load(
        (tmp_path / "latest_strategy.yaml").read_text(encoding="utf-8")
    )
    assert index["schema_version"] == 4
    assert index["groups"]["a_share"]["run_id"] == "candidate_a"
    assert index["groups"]["hk"]["artifact"] == artifacts["hk"]
    assert index["groups"]["hk"]["legacy"] is True

    active = load_latest_strategy_run(groups=GROUPS, root=tmp_path)
    assert active is not None
    assert active.strategy_for("a_share").name == "technical_ensemble"
    assert active.strategy_for("hk").name == "percentile"
    assert active.strategy_for("us").name == "percentile"


def _annotate_activation(root, run_id, group, **fields):
    """Rewrite one candidate's activation block the way promotion does."""
    artifact = root / "runs" / run_id / f"{group}_best_params.yaml"
    data = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    data["activation"].update(fields)
    artifact.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_relative_promotion_overrides_a_failing_holdout(tmp_path):
    assert _candidate(
        tmp_path,
        "relative_passed",
        "a_share",
        holdout_excesses=[-1.0, -2.0, -0.5, -3.0],
    )
    _annotate_activation(
        tmp_path,
        "relative_passed",
        "a_share",
        eligible=True,
        relative_promotion_passed=True,
    )

    assert activate_run("relative_passed", group="a_share", root=tmp_path)
    assert load_latest_strategy_run(groups=("a_share",), root=tmp_path) is not None


def test_failing_holdout_without_relative_evidence_cannot_activate(tmp_path):
    assert _candidate(
        tmp_path,
        "no_relative_evidence",
        "a_share",
        holdout_excesses=[-1.0, -2.0, -0.5, -3.0],
    )
    _annotate_activation(
        tmp_path,
        "no_relative_evidence",
        "a_share",
        eligible=True,
    )

    assert not activate_run("no_relative_evidence", group="a_share", root=tmp_path)


@pytest.mark.parametrize(
    "enabled,auto_activate,candidate_return,incumbent_available,comparison_available",
    [
        pytest.param(True, True, 12.0, True, True, id="better-auto-activates"),
        pytest.param(True, False, 12.0, True, True, id="auto-disabled"),
        pytest.param(False, True, 12.0, True, True, id="promotion-disabled"),
        pytest.param(True, True, 8.0, True, True, id="worse-candidate"),
        pytest.param(True, True, 10.0, True, True, id="equal-candidate"),
        pytest.param(True, True, 12.0, False, False, id="no-incumbent"),
        pytest.param(True, True, 12.0, True, False, id="missing-comparison"),
    ],
)
def test_optimizer_auto_activation_updates_only_the_improved_market(
    tmp_path,
    monkeypatch,
    enabled,
    auto_activate,
    candidate_return,
    incumbent_available,
    comparison_available,
):
    """Exercise real promotion, publication and active pointers with fake scoring."""
    config = main.load_config()
    config.pop("point_in_time_data", None)
    config["stocks"] = ["00883", "01816", "00700", "00728", "01339"]
    config["skip_search"] = []
    market_config = main.get_market_optimizer_config("hk", config)
    monkeypatch.chdir(tmp_path)
    root = Path("data/optimizer")
    for group in GROUPS:
        if group == "hk" and not incumbent_available:
            continue
        assert _candidate(root, f"baseline_{group}", group)
        assert activate_run(f"baseline_{group}", group=group, root=root)
    before = (root / "latest_strategy.yaml").read_bytes()
    before_entries = yaml.safe_load(before)["groups"]
    policy_path = Path("config/promotion_policy.yaml")
    policy_path.parent.mkdir()
    policy_path.write_text(
        yaml.safe_dump(
            {"enabled": enabled, "auto_activate_if_better": auto_activate}
        ),
        encoding="utf-8",
    )
    history = pd.DataFrame(
        {"date": pd.date_range("2026-01-01", periods=2), "close": [10.0, 11.0]}
    )

    class FixtureDataSource:
        def __init__(self, config):
            pass

        def fetch_stock_data(self, code, days):
            return history.copy()

    def search(*args, output_dir, _constraints, **kwargs):
        assert _candidate(root, output_dir.name, "hk", eligible=False)
        path = output_dir / "hk_best_params.yaml"
        artifact = yaml.safe_load(path.read_text(encoding="utf-8"))
        artifact["market_config_hash"] = market_config.config_hash
        path.write_text(yaml.safe_dump(artifact), encoding="utf-8")
        result = SimpleNamespace(parameters={"adx_min": 1}, objective_score=1.0)
        return [result], _constraints

    def snapshot(total_return):
        return {
            "total_return": total_return,
            "max_drawdown": -10.0,
            "trade_count": 4,
            "benchmark_returns": {"VOO": 20.0, "BRK.B": 15.0, "risk_free": 3.0},
        }

    evaluations = iter(
        [
            {"hk": snapshot(candidate_return)},
            {"hk": snapshot(10.0)} if comparison_available else {},
        ]
    )
    monkeypatch.setattr("src.data.data_source.DataSource", FixtureDataSource)
    monkeypatch.setattr(main, "_has_optimizer_history", lambda *args: True)
    monkeypatch.setattr(main, "_load_optimizer_benchmarks", lambda *args: {})
    monkeypatch.setattr(main, "run_optimizer", search)
    monkeypatch.setattr(
        main, "evaluate_all_groups", lambda *args, **kwargs: next(evaluations)
    )
    monkeypatch.setattr(main, "_optimizer_validation_snapshot", lambda report: report)
    reports = []

    assert main._run_optimization_group(
        config, "hk", market_config, report_sink=reports
    ) == {"hk": 1}

    report = reports[0]
    expected_activation = bool(
        enabled
        and auto_activate
        and candidate_return > 10.0
        and incumbent_available
        and comparison_available
    )
    assert report.activated is expected_activation
    assert report.candidate is not expected_activation
    run_dir = root / "runs" / report.run_id
    assert (run_dir / "manifest.yaml").exists()
    receipt = yaml.safe_load((run_dir / "run_summary.yaml").read_text("utf-8"))
    assert receipt["activated"] is expected_activation
    after = yaml.safe_load((root / "latest_strategy.yaml").read_text("utf-8"))
    for group in ("a_share", "us"):
        assert after["groups"][group] == before_entries[group]
    if expected_activation:
        assert after["groups"]["hk"]["run_id"] == report.run_id
        artifact = yaml.safe_load(
            (run_dir / "hk_best_params.yaml").read_text("utf-8")
        )
        assert artifact["activation"]["relative_promotion_passed"] is True
        assert artifact["activation"]["holdout_passed"] is False
    else:
        assert (root / "latest_strategy.yaml").read_bytes() == before
