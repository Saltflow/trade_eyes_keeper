from pathlib import Path

import yaml

from src.search.artifacts import OptimizerGroupSummary, publish_complete_run


def test_ineligible_complete_run_cannot_replace_active_pointer(tmp_path):
    run_id = "ineligible"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    group = "a_share"
    config_hash = "hash-ineligible-a-share"
    artifact = run_dir / f"{group}_best_params.yaml"
    artifact.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "group": group,
                "strategy_id": "regime_pullback",
                "solver_id": "local_genetic",
                "gate_profile": "standard",
                "market_config_hash": config_hash,
                "params": {},
                "execution": {"model": "cash_cap"},
                "activation": {"eligible": False},
            }
        ),
        encoding="utf-8",
    )
    groups = {
        group: OptimizerGroupSummary(
            group=group,
            status="completed",
            artifact=artifact.name,
            strategy_name="regime_pullback",
            solver_id="local_genetic",
            gate_profile="standard",
            market_config_hash=config_hash,
        )
    }

    assert not publish_complete_run(
        run_id,
        "regime_pullback",
        "2026-08-02T00:00:00",
        groups,
        required_groups=(group,),
        all_groups=(group,),
        root=tmp_path,
        activate=True,
    )
    assert not (Path(tmp_path) / "latest_strategy.yaml").exists()
    manifest = yaml.safe_load(
        (run_dir / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["candidate"] is True
    assert manifest["groups"][group]["config_hash"] == config_hash
    saved = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    assert saved["activation"]["eligible"] is False
