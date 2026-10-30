#!/usr/bin/env python3
"""Explicitly add the missing withholding-rate binding to legacy accounts.

Dry-run is the default. --apply requires a stopped runtime service, preserves
all account data, and creates byte-exact backups plus a separate audit receipt.
No strategy, cash, holdings, transaction, or corporate-action record is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.process_lock import exclusive_process_lock
from src.core.ref_portfolio import (
    RefPortfolio,
    reference_execution_binding_status,
    reference_execution_contract,
)
from src.search.artifacts import load_strategy_run
from src.search.config import get_market_optimizer_configs
from src.search.contracts import stable_hash

PORTFOLIO_FILES = {
    "a_share": "ref_portfolio_a.yaml",
    "hk": "ref_portfolio_hk.yaml",
    "us": "ref_portfolio_us.yaml",
}
MIGRATION_ID = "reference-execution-add-withholding-rate-v1"


@dataclass
class MigrationPlan:
    path: Path
    original: bytes
    updated: bytes
    summary: dict


def _account_fingerprint(document: dict) -> str:
    state = deepcopy(document)
    state["ref_portfolio"].pop("execution_hash", None)
    return stable_hash(state)


def prepare_migration(
    path: Path,
    group: str,
    execution_config,
    optimizer_root: Path,
) -> MigrationPlan:
    """Validate the exact pinned run and return a plan without writing files."""
    original = path.read_bytes()
    document = yaml.safe_load(original)
    if not isinstance(document, dict) or not isinstance(
        document.get("ref_portfolio"), dict
    ):
        raise TypeError("reference account document is missing or malformed")
    raw = document["ref_portfolio"]
    portfolio = RefPortfolio.from_dict(raw)
    if not portfolio.inception_date or not portfolio.is_bound:
        raise ValueError("account is uninitialized or has no pinned strategy run")
    if portfolio.market_group != group:
        raise ValueError("account market does not match the selected market")
    pinned = load_strategy_run(
        portfolio.strategy_run_id, groups=(group,), root=optimizer_root
    )
    if pinned is None or pinned.run_id_for(group) != portfolio.strategy_run_id:
        raise ValueError("exact activated strategy run cannot be restored")
    manifest_path = (
        optimizer_root / "runs" / portfolio.strategy_run_id / "manifest.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_bytes())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 4:
        raise ValueError("pinned run must have a restored v4 manifest")
    strategy = pinned.strategy_for(group)
    params = pinned.params_by_group.get(group)
    if strategy is None or strategy.name != portfolio.strategy_id or params is None:
        raise ValueError("pinned strategy does not match the account")
    params_hash = stable_hash({"strategy_id": strategy.name, "values": params.values})
    if params_hash != portfolio.params_hash:
        raise ValueError("pinned parameter fingerprint does not match the account")
    contract = reference_execution_contract(
        params.execution_snapshot, execution_config, group
    )
    if not 0.0 <= contract["withholding_rate"] <= 1.0:
        raise ValueError("withholding rate must be finite and within [0, 1]")
    binding = reference_execution_binding_status(portfolio.execution_hash, contract)
    if binding == "mismatch":
        raise ValueError("execution differs beyond the missing withholding-rate field")

    updated_document = deepcopy(document)
    new_hash = stable_hash(contract)
    updated_document["ref_portfolio"]["execution_hash"] = new_hash
    fingerprint = _account_fingerprint(document)
    if _account_fingerprint(updated_document) != fingerprint:
        raise ValueError("migration would change account data")
    summary = {
        "group": group,
        "path": str(path),
        "status": "already_current" if binding == "current" else "eligible",
        "run_id": portfolio.strategy_run_id,
        "strategy_id": portfolio.strategy_id,
        "params_hash": params_hash,
        "old_execution_hash": portfolio.execution_hash,
        "new_execution_hash": new_hash,
        "withholding_rate": contract["withholding_rate"],
        "execution_contract": contract,
        "holdings_count": len(raw.get("holdings") or {}),
        "trade_count": len(raw.get("trade_log") or []),
        "processed_event_count": len(raw.get("processed_events") or []),
        "account_state_sha256": fingerprint,
        "account_state_unchanged": True,
        "original_file_sha256": hashlib.sha256(original).hexdigest(),
    }
    updated = (
        original
        if binding == "current"
        else yaml.safe_dump(
            updated_document, allow_unicode=True, sort_keys=False
        ).encode("utf-8")
    )
    return MigrationPlan(path, original, updated, summary)


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace one file after flushing its complete contents."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary_path, path.stat().st_mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_receipt(path: Path, report: dict) -> None:
    _atomic_write(
        path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    )


def migrate_reference_contracts(
    application_config: dict,
    *,
    data_dir: Path,
    optimizer_root: Path,
    constraints_path: Path,
    groups: tuple[str, ...] = tuple(PORTFOLIO_FILES),
    apply: bool = False,
    service_lock_path: Path | None = None,
) -> dict:
    """Prepare every account first; apply only when all guards pass."""
    configs = get_market_optimizer_configs(
        application_config, constraints_path, groups=groups
    )
    plans = []
    report = {
        "migration": MIGRATION_ID,
        "mode": "apply" if apply else "dry_run",
        "status": "ready",
        "accounts": [],
        "applied_groups": [],
    }
    for group in groups:
        path = data_dir / PORTFOLIO_FILES[group]
        try:
            plan = prepare_migration(
                path, group, configs[group].execution, optimizer_root
            )
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
            report["status"] = "blocked"
            report["accounts"].append(
                {
                    "group": group,
                    "path": str(path),
                    "status": "blocked",
                    "reason": str(exc),
                }
            )
        else:
            plans.append(plan)
            report["accounts"].append(dict(plan.summary))
    if report["status"] == "blocked" or not apply:
        return report
    changes = [plan for plan in plans if plan.summary["status"] == "eligible"]
    if not changes:
        report["status"] = "already_current"
        return report

    lock_path = service_lock_path or data_dir / "runtime" / ".service.lock"
    with exclusive_process_lock(lock_path) as acquired:
        if not acquired:
            report.update(status="blocked", reason="runtime service is running")
            return report
        if any(plan.path.read_bytes() != plan.original for plan in plans):
            report.update(status="blocked", reason="account changed after validation")
            return report
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = data_dir / "reference_migrations" / f"{stamp}-{uuid4().hex[:8]}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        receipt = backup_dir / "audit.json"
        report.update(status="prepared", audit_path=str(receipt), created_at=stamp)
        for plan in changes:
            backup = backup_dir / plan.path.name
            with backup.open("xb") as stream:
                stream.write(plan.original)
                stream.flush()
                os.fsync(stream.fileno())
            if backup.read_bytes() != plan.original:
                raise OSError(f"backup verification failed: {backup}")
            for account in report["accounts"]:
                if account["group"] == plan.summary["group"]:
                    account["backup_path"] = str(backup)
        _write_receipt(receipt, report)
        try:
            for plan in changes:
                if plan.path.read_bytes() != plan.original:
                    raise ValueError("account changed after backup; refusing overwrite")
                _atomic_write(plan.path, plan.updated)
                report["applied_groups"].append(plan.summary["group"])
                written = yaml.safe_load(plan.path.read_bytes())
                if (
                    _account_fingerprint(written)
                    != plan.summary["account_state_sha256"]
                    or written["ref_portfolio"]["execution_hash"]
                    != plan.summary["new_execution_hash"]
                ):
                    raise ValueError("written account failed preservation check")
                for account in report["accounts"]:
                    if account["group"] == plan.summary["group"]:
                        account["status"] = "applied"
                        account["write_verified"] = True
                        account["written_file_sha256"] = hashlib.sha256(
                            plan.path.read_bytes()
                        ).hexdigest()
                _write_receipt(receipt, report)
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
            report.update(status="failed", reason=str(exc))
            _write_receipt(receipt, report)
            return report
        report["status"] = "applied"
        _write_receipt(receipt, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Explicitly apply migration"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config/config.yaml")
    parser.add_argument(
        "--constraints", type=Path, default=ROOT / "config/optimizer_constraints.yaml"
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--optimizer-root", type=Path, default=ROOT / "data/optimizer")
    parser.add_argument("--group", choices=tuple(PORTFOLIO_FILES), action="append")
    args = parser.parse_args(argv)
    try:
        application_config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        from src.core.runtime_service import runtime_directory

        report = migrate_reference_contracts(
            application_config,
            data_dir=args.data_dir.resolve(),
            optimizer_root=args.optimizer_root.resolve(),
            constraints_path=args.constraints.resolve(),
            groups=tuple(dict.fromkeys(args.group or PORTFOLIO_FILES)),
            apply=args.apply,
            service_lock_path=runtime_directory(application_config) / ".service.lock",
        )
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        report = {"status": "blocked", "reason": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"ready", "applied", "already_current"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
