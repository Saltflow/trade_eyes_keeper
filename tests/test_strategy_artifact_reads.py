"""Historical strategy runs stay readable without optimizer write APIs."""

import pytest
import yaml

from src.data.corporate_action_currency import CORPORATE_ACTION_CURRENCY_CONTRACT
from src.search.artifacts import (
    execution_contract_compatibility,
    load_latest_strategy_run,
    load_strategy_run,
)

GROUPS = ("a_share", "hk", "us")

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


def test_legacy_pointer_preserves_native_markets_but_blocks_unproven_hk(tmp_path):
    artifacts = {
        group: _legacy_market(tmp_path, "legacy_run", group) for group in GROUPS
    }
    _legacy_pointer(tmp_path, "legacy_run", artifacts)

    active = load_latest_strategy_run(groups=("a_share", "us"), root=tmp_path)

    assert active is not None
    assert active.run_id == "legacy_run"
    for group in ("a_share", "us"):
        assert active.strategy_for(group).name == "percentile"
        assert active.run_id_for(group) == "legacy_run"
        assert active.params_by_group[group].values == {"adx_min": 1}
        assert active.solver_by_group[group] == ""
        assert active.config_hash_by_group[group] == ""

    diagnostics = {}
    assert (
        load_latest_strategy_run(
            groups=("hk",), root=tmp_path, compatibility_diagnostics=diagnostics
        )
        is None
    )
    assert diagnostics["hk"]["requires_reoptimization"] is True
    assert diagnostics["hk"]["status"] == "incompatible_execution_contract"
    for group in ("a_share", "us"):
        assert active.execution_compatibility_by_group[group]["status"] == (
            "legacy_native_currency_compatible"
        )


@pytest.mark.parametrize("group", GROUPS)
def test_explicit_unknown_currency_contract_is_rejected(group):
    result = execution_contract_compatibility(
        {"execution_contracts": {"currency_cash_version": "future/999"}}, group
    )
    assert result["compatible"] is False
    assert result["requires_reoptimization"] is True


@pytest.mark.parametrize("group", GROUPS)
def test_current_currency_contract_is_compatible(group):
    result = execution_contract_compatibility(
        {
            "execution_contracts": {
                "currency_cash_version": CORPORATE_ACTION_CURRENCY_CONTRACT,
            }
        },
        group,
    )
    assert result["status"] == "compatible"
    assert result["requires_reoptimization"] is False


@pytest.mark.parametrize("contract", [None, [], {"currency_cash_version": ""}])
def test_malformed_contract_cannot_use_native_legacy_policy(contract):
    result = execution_contract_compatibility({"execution_contracts": contract}, "us")
    assert result["compatible"] is False


def _current_market(root, run_id, group, value=1):
    folder = root / "runs" / run_id
    folder.mkdir(parents=True)
    entry = {"group": group, "run_id": run_id, "strategy": "percentile",
             "solver_id": "retired_solver_metadata", "gate_profile": "historical",
             "config_hash": "fixed-config", "artifact": f"runs/{run_id}/{group}.yaml"}
    data = {"schema_version": 2, "group": group, "strategy_id": "percentile",
            "solver_id": entry["solver_id"], "gate_profile": entry["gate_profile"],
            "market_config_hash": entry["config_hash"], "params": {"adx_min": value},
            "execution": {"model": "cash_cap", "buy_cash_limit": 10000,
                          "sell_cash_limit": 10000},
            "execution_contracts": {"currency_cash_version": CORPORATE_ACTION_CURRENCY_CONTRACT}}
    artifact = folder / f"{group}.yaml"
    artifact.write_text(yaml.safe_dump(data), encoding="utf-8")
    manifest = {"schema_version": 4, "activated": True, "run_id": run_id,
                "timestamp": "2026-11-04", "groups": {group: entry}}
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (root / "latest_strategy.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return artifact


def test_exact_historical_run_is_not_replaced_by_latest_pointer(tmp_path):
    _current_market(tmp_path, "old", "us", value=1)
    _current_market(tmp_path, "new", "us", value=2)
    assert load_strategy_run("old", groups=("us",), root=tmp_path).params_by_group["us"].values == {"adx_min": 1}
    assert load_latest_strategy_run(groups=("us",), root=tmp_path).params_by_group["us"].values == {"adx_min": 2}
    assert load_latest_strategy_run(groups=GROUPS, root=tmp_path) is None
    assert load_strategy_run("../old", groups=("us",), root=tmp_path) is None


def test_incompatible_historical_run_is_rejected_without_rewriting(tmp_path):
    artifact = _current_market(tmp_path, "old_hk", "hk")
    data = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    data["execution_contracts"]["currency_cash_version"] = "old/0"
    artifact.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.yaml")}
    diagnostics = {}
    assert load_latest_strategy_run(groups=("hk",), root=tmp_path, compatibility_diagnostics=diagnostics) is None
    assert diagnostics["hk"]["status"] == "incompatible_execution_contract"
    assert all(p.read_bytes() == data for p, data in before.items())
