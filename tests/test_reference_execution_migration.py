"""The explicit legacy migration changes only the execution fingerprint."""

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import migrate_reference_execution_contract as migration
from src.core.process_lock import exclusive_process_lock
from src.core.ref_portfolio import (
    Holding,
    RefPortfolio,
    Trade,
    reference_execution_binding_status,
    reference_execution_contract,
)
from src.data.corporate_action_currency import CORPORATE_ACTION_CURRENCY_CONTRACT
from src.search.config import get_market_optimizer_configs
from src.search.contracts import stable_hash


@pytest.fixture
def account_files(tmp_path):
    app = {
        "optimizer": {
            "markets": {
                group: {
                    "strategy": "percentile",
                    "solver_id": "random",
                    "gate_profile": "standard",
                    "walk_forward_profile": f"{group}_84m",
                    "execution_profile": profile,
                    "benchmark_profile": group,
                }
                for group, profile in (
                    ("a_share", "a_share_cny"),
                    ("hk", "hk_hkd"),
                    ("us", "us_usd"),
                )
            }
        }
    }
    constraints = Path("config/optimizer_constraints.yaml").resolve()
    configs = get_market_optimizer_configs(app, constraints)
    optimizer_root = tmp_path / "optimizer"
    run_id = "migration-test"
    run_dir = optimizer_root / "runs" / run_id
    run_dir.mkdir(parents=True)
    params = {"threshold": 1}
    execution = {"model": "cash_cap", "buy_cash_limit": 10000, "sell_cash_limit": 10000}
    manifest = {"schema_version": 4, "run_id": run_id, "activated": True, "groups": {}}
    contracts = {}
    for group, code in (("a_share", "601728"), ("hk", "00883"), ("us", "QQQ")):
        artifact_name = f"{group}.yaml"
        artifact = {
            "schema_version": 2,
            "group": group,
            "strategy_id": "percentile",
            "solver_id": "random",
            "gate_profile": "standard",
            "market_config_hash": f"config-{group}",
            "params": params,
            "execution": execution,
            "execution_contracts": {
                "currency_cash_version": CORPORATE_ACTION_CURRENCY_CONTRACT
            },
        }
        (run_dir / artifact_name).write_text(yaml.safe_dump(artifact), encoding="utf-8")
        manifest["groups"][group] = {
            "group": group,
            "run_id": run_id,
            "artifact": f"runs/{run_id}/{artifact_name}",
            "strategy": "percentile",
            "solver_id": "random",
            "gate_profile": "standard",
            "config_hash": f"config-{group}",
        }
        contract = reference_execution_contract(
            execution, configs[group].execution, group
        )
        contracts[group] = contract
        legacy = {
            key: value for key, value in contract.items() if key != "withholding_rate"
        }
        portfolio = RefPortfolio(
            inception_date="2026-09-02",
            cash=50000.0,
            holdings={code: Holding(code, 100, 40.1234, "2026-09-03")},
            trade_log=[
                Trade("2026-09-03", code, "buy", 100, 40.1234, 4012.34, "signal")
            ],
            processed_events=["original-buy", "original-dividend"],
            gross_dividend_cash=30.0,
            dividend_tax_cost=6.0,
            net_dividend_cash=24.0,
            market_group=group,
            strategy_run_id=run_id,
            strategy_id="percentile",
            params_hash=stable_hash({"strategy_id": "percentile", "values": params}),
            execution_hash=stable_hash(legacy),
        )
        raw = {"ref_portfolio": portfolio.to_dict(), "operator_note": "preserve"}
        raw["ref_portfolio"]["custom_field"] = {"preserve": True}
        raw["ref_portfolio"]["cash"] = 50000.123456
        (tmp_path / migration.PORTFOLIO_FILES[group]).write_text(
            yaml.safe_dump(raw), encoding="utf-8"
        )
    (run_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(app), encoding="utf-8")
    return {
        "app": app,
        "data_dir": tmp_path,
        "optimizer_root": optimizer_root,
        "constraints_path": constraints,
        "contracts": contracts,
        "run_dir": run_dir,
        "config_path": config_path,
    }


def _run(files, **kwargs):
    return migration.migrate_reference_contracts(
        files["app"],
        data_dir=files["data_dir"],
        optimizer_root=files["optimizer_root"],
        constraints_path=files["constraints_path"],
        **kwargs,
    )


def _files_snapshot(directory):
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_dry_run_is_read_only_and_reports_exact_change(account_files):
    before = _files_snapshot(account_files["data_dir"])

    report = _run(account_files)

    assert report["status"] == "ready"
    assert report["mode"] == "dry_run"
    assert report["applied_groups"] == []
    assert len(report["accounts"]) == 3
    for account in report["accounts"]:
        assert account["status"] == "eligible"
        assert account["old_execution_hash"] != account["new_execution_hash"]
        assert account["holdings_count"] == account["trade_count"] == 1
        assert account["processed_event_count"] == 2
        assert account["account_state_unchanged"]
    assert _files_snapshot(account_files["data_dir"]) == before


def test_apply_preserves_every_account_field_and_is_idempotent(account_files):
    before = _files_snapshot(account_files["data_dir"])

    report = _run(account_files, apply=True)

    assert report["status"] == "applied"
    assert report["applied_groups"] == ["a_share", "hk", "us"]
    assert json.loads(Path(report["audit_path"]).read_text(encoding="utf-8")) == report
    for account in report["accounts"]:
        original_bytes = before[Path(migration.PORTFOLIO_FILES[account["group"]])]
        original = yaml.safe_load(original_bytes)
        updated = yaml.safe_load(Path(account["path"]).read_bytes())
        assert Path(account["backup_path"]).read_bytes() == original_bytes
        assert (
            updated["ref_portfolio"]["execution_hash"] == account["new_execution_hash"]
        )
        updated["ref_portfolio"]["execution_hash"] = original["ref_portfolio"][
            "execution_hash"
        ]
        assert updated == original
        assert account["write_verified"]
        assert account["status"] == "applied"
    after = _files_snapshot(account_files["data_dir"])

    repeated = _run(account_files, apply=True)

    assert repeated["status"] == "already_current"
    assert repeated["applied_groups"] == []
    assert _files_snapshot(account_files["data_dir"]) == after


@pytest.mark.parametrize(
    "fault", ["params", "execution", "unbound", "market", "strategy"]
)
def test_any_invalid_account_blocks_all_writes(account_files, fault):
    path = account_files["data_dir"] / migration.PORTFOLIO_FILES["us"]
    raw = yaml.safe_load(path.read_bytes())
    portfolio = raw["ref_portfolio"]
    if fault == "params":
        portfolio["params_hash"] = "not-the-pinned-params"
    elif fault == "execution":
        legacy = deepcopy(account_files["contracts"]["us"])
        legacy.pop("withholding_rate")
        legacy["commission_rate"] *= 2
        portfolio["execution_hash"] = stable_hash(legacy)
    elif fault == "unbound":
        portfolio["strategy_run_id"] = ""
    elif fault == "market":
        portfolio["market_group"] = "hk"
    else:
        portfolio["strategy_id"] = "technical_ensemble"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = _files_snapshot(account_files["data_dir"])

    report = _run(account_files, apply=True)

    assert report["status"] == "blocked"
    assert report["applied_groups"] == []
    assert report["accounts"][-1]["status"] == "blocked"
    assert _files_snapshot(account_files["data_dir"]) == before


@pytest.mark.parametrize(
    "fault", ["missing", "deactivated", "schema", "currency_contract"]
)
def test_unrestorable_run_blocks_migration(account_files, fault):
    path = account_files["run_dir"] / "manifest.yaml"
    if fault == "missing":
        path.unlink()
    elif fault == "currency_contract":
        artifact_path = account_files["run_dir"] / "hk.yaml"
        artifact = yaml.safe_load(artifact_path.read_bytes())
        artifact.pop("execution_contracts")
        artifact_path.write_text(yaml.safe_dump(artifact), encoding="utf-8")
    else:
        manifest = yaml.safe_load(path.read_bytes())
        manifest["activated" if fault == "deactivated" else "schema_version"] = (
            False if fault == "deactivated" else 99
        )
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    before = _files_snapshot(account_files["data_dir"])

    report = _run(account_files, apply=True)

    assert report["status"] == "blocked"
    assert _files_snapshot(account_files["data_dir"]) == before


def test_running_service_blocks_apply(account_files):
    lock_path = account_files["data_dir"] / "runtime" / ".service.lock"
    with exclusive_process_lock(lock_path) as acquired:
        assert acquired
        before = {
            path: path.read_bytes()
            for path in account_files["data_dir"].glob("ref_portfolio_*.yaml")
        }
        report = _run(account_files, apply=True)
        assert report["status"] == "blocked"
        assert report["reason"] == "runtime service is running"
        assert all(path.read_bytes() == content for path, content in before.items())
        assert not (account_files["data_dir"] / "reference_migrations").exists()


def test_concurrent_account_change_is_not_overwritten(account_files, monkeypatch):
    path = account_files["data_dir"] / migration.PORTFOLIO_FILES["us"]
    raw = yaml.safe_load(path.read_bytes())
    raw["ref_portfolio"]["cash"] += 123
    external = yaml.safe_dump(raw).encode("utf-8")

    @contextmanager
    def acquire_after_account_changed(_path):
        path.write_bytes(external)
        yield True

    monkeypatch.setattr(
        migration, "exclusive_process_lock", acquire_after_account_changed
    )

    report = _run(account_files, apply=True)

    assert report["status"] == "blocked"
    assert report["reason"] == "account changed after validation"
    assert path.read_bytes() == external
    assert not (account_files["data_dir"] / "reference_migrations").exists()


def test_cli_defaults_to_dry_run(account_files, capsys):
    before = _files_snapshot(account_files["data_dir"])

    exit_code = migration.main(
        [
            "--config",
            str(account_files["config_path"]),
            "--constraints",
            str(account_files["constraints_path"]),
            "--data-dir",
            str(account_files["data_dir"]),
            "--optimizer-root",
            str(account_files["optimizer_root"]),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["mode"] == "dry_run"
    assert _files_snapshot(account_files["data_dir"]) == before


def test_legacy_binding_is_diagnosed_but_not_accepted_as_current(account_files):
    contract = account_files["contracts"]["hk"]
    legacy = {
        key: value for key, value in contract.items() if key != "withholding_rate"
    }
    assert reference_execution_binding_status(stable_hash(legacy), contract) == (
        "legacy_missing_withholding"
    )
    assert (
        reference_execution_binding_status(stable_hash(contract), contract) == "current"
    )
    assert reference_execution_binding_status("unknown", contract) == "mismatch"
