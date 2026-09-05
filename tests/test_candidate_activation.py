"""Strict v4 candidate publication and independent market activation."""

from __future__ import annotations

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
