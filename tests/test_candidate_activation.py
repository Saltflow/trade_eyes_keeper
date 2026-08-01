"""Manual candidate activation is complete, strict and atomic."""

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


def _candidate(root, run_id, eligible=True):
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    summaries = {}
    for group in GROUPS:
        artifact = run_dir / f"{group}_best_params.yaml"
        artifact.write_text(
            yaml.safe_dump(
                {
                    "strategy_id": "regime_pullback",
                    "params": {"adx_min": 1},
                    "execution": {
                        "model": "target_weight",
                        "per_symbol_cap": 0.2,
                        "total_exposure_cap": 0.8,
                    },
                    "activation": {
                        "eligible": eligible,
                        "holdout_passed": eligible,
                    },
                    "holdout_windows": [{"excess_return": 1.0 if eligible else -1.0}],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        summaries[group] = OptimizerGroupSummary(
            group=group,
            status="completed",
            artifact=artifact.name,
        )
    return publish_complete_run(
        run_id,
        "regime_pullback",
        "2026-07-30T20:00:00",
        summaries,
        required_groups=GROUPS,
        all_groups=GROUPS,
        root=root,
        activate=False,
    )


def test_candidate_does_not_change_active_run_until_manual_activation(tmp_path):
    assert _candidate(tmp_path, "candidate")
    assert load_latest_strategy_run(groups=GROUPS, root=tmp_path) is None

    assert activate_run("candidate", groups=GROUPS, root=tmp_path)
    active = load_latest_strategy_run(groups=GROUPS, root=tmp_path)
    assert active is not None
    assert active.run_id == "candidate"
    assert active.strategy_name == "regime_pullback"


def test_ineligible_candidate_cannot_activate(tmp_path):
    assert _candidate(tmp_path, "failed_holdout", eligible=False)
    assert not activate_run("failed_holdout", groups=GROUPS, root=tmp_path)
    assert load_latest_strategy_run(groups=GROUPS, root=tmp_path) is None


def test_legacy_copy_failure_after_commit_does_not_rollback_active_pointer(
    tmp_path, monkeypatch
):
    assert _candidate(tmp_path, "copy_failure")

    def fail_copy(*_args, **_kwargs):
        raise OSError("legacy copy unavailable")

    monkeypatch.setattr(
        "src.search.artifacts.shutil.copy2", fail_copy
    )
    assert activate_run("copy_failure", groups=GROUPS, root=tmp_path)
    active = load_latest_strategy_run(groups=GROUPS, root=tmp_path)
    assert active is not None
    assert active.run_id == "copy_failure"


def test_activate_run_cli_is_the_only_manual_switch_argument():
    parser = main._build_argument_parser()
    args = parser.parse_args(["--activate-run", "candidate"])
    assert args.activate_run == "candidate"
