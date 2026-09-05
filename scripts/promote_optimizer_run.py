#!/usr/bin/env python3
"""Re-evaluate one completed optimizer run against the active incumbent."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as app
from src.backtest import evaluate_all_groups
from src.data.data_source import DataSource
from src.markets import _detect_fine_group, get_skip_search
from src.search.config import get_market_optimizer_config
from src.search.artifacts import (
    OptimizerGroupSummary,
    ACTIVE_SCHEMA_VERSION,
    _artifact_matches_entry,
    _manifest_entry,
    load_latest_strategy_run,
    publish_complete_run,
)
from src.search.promotion import (
    PromotionPolicy,
    compare_with_incumbent,
    record_promotion_decision,
)
from src.strategy import Params, get_strategy


GROUPS = ("a_share", "hk", "us")
OPTIMIZER_ROOT = ROOT / "data" / "optimizer"
logger = logging.getLogger(__name__)


def _load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"expected YAML mapping: {path}")
    return value


def _params(artifact: dict, strategy_name: str) -> Params:
    values = artifact.get("params")
    if not isinstance(values, dict):
        raise ValueError("candidate artifact has no parameter mapping")
    execution = artifact.get("execution")
    return Params(
        values=dict(values),
        _engine=strategy_name,
        execution_snapshot=dict(execution) if isinstance(execution, dict) else {},
    )


def promote_optimizer_run(run_id: str, group: str) -> dict[str, object]:
    """Compare one market candidate with that market's active incumbent."""

    if not run_id or Path(run_id).name != run_id:
        raise ValueError("run_id must be one optimizer run directory name")
    if group not in GROUPS:
        raise ValueError("group must be one of a_share, hk, us")
    run_dir = OPTIMIZER_ROOT / "runs" / run_id
    manifest_path = run_dir / "manifest.yaml"
    manifest = _load_yaml(manifest_path)
    if int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION:
        raise ValueError("candidate must use the current v4 market manifest")
    if set((manifest.get("groups") or {}).keys()) != {group}:
        raise ValueError("candidate must contain exactly the requested market")
    entry = _manifest_entry(manifest, group)
    if entry is None:
        raise ValueError("candidate market entry is incomplete")
    artifact_path = ROOT / str(entry["artifact"])
    artifact = _load_yaml(artifact_path)
    if not _artifact_matches_entry(artifact, entry, group):
        raise ValueError("candidate artifact does not match its market contract")
    strategy_name = str(entry["strategy"])
    strategy = get_strategy(strategy_name)
    if strategy is None:
        raise ValueError(f"unregistered candidate strategy: {strategy_name}")

    config = app.load_config()
    market_config = get_market_optimizer_config(
        group, application_config=config
    )
    constraints = market_config.constraints
    incumbent = load_latest_strategy_run(groups=(group,))
    incumbent_strategy = (
        incumbent.strategy_for(group) if incumbent is not None else None
    )
    if incumbent is None or incumbent_strategy is None:
        raise ValueError("no active incumbent is available for this market")
    lookback_days = app._optimizer_lookback_days(constraints)
    data_source = DataSource(config)
    skipped = get_skip_search(config)
    configured = [
        app._stock_code(stock) for stock in (config.get("stocks", []) or [])
    ]

    summaries: dict[str, OptimizerGroupSummary] = {}
    candidate_reports: dict[str, dict[str, object]] = {}
    incumbent_reports: dict[str, dict[str, object]] = {}
    validation = artifact.get("validation", {})
    composition = (
        list(validation.get("composition", []) or [])
        if isinstance(validation, dict)
        else []
    )
    codes = composition or [
        code
        for code in configured
        if code and code not in skipped and _detect_fine_group(code) == group
    ]
    stocks_data = {}
    for code in codes:
        data = data_source.fetch_stock_data(code, days=lookback_days)
        if data is not None and not data.empty:
            stocks_data[code] = data
    if set(stocks_data) != set(codes):
        missing = sorted(set(codes) - set(stocks_data))
        raise ValueError(f"{group} comparison data missing: {missing}")

    benchmarks = app._load_optimizer_benchmarks(
        data_source,
        constraints,
        group,
        lookback_days,
    )
    period = artifact.get("validation_period", {})
    start = (
        str(period.get("start"))
        if isinstance(period, dict) and period.get("start")
        else app._optimizer_validation_start(constraints)
    )
    end = (
        str(period.get("end"))
        if isinstance(period, dict) and period.get("end")
        else None
    )
    candidate_result = evaluate_all_groups(
        stocks_data,
        list(stocks_data),
        strategy,
        _params(artifact, strategy_name),
        constraints.execution,
        benchmark_data=benchmarks,
        target_groups=[group],
        start_date=start,
        end_date=end,
        market_constraints=constraints,
    ).get(group)
    incumbent_result = evaluate_all_groups(
        stocks_data,
        list(stocks_data),
        incumbent_strategy,
        incumbent.params_by_group[group],
        constraints.execution,
        benchmark_data=benchmarks,
        target_groups=[group],
        start_date=start,
        end_date=end,
        market_constraints=constraints,
    ).get(group)
    if candidate_result is None or incumbent_result is None:
        raise ValueError(f"{group} comparison produced no report")
    candidate_reports[group] = app._optimizer_validation_snapshot(candidate_result)
    incumbent_reports[group] = app._optimizer_validation_snapshot(incumbent_result)
    summaries[group] = OptimizerGroupSummary(
        group=group,
        status="completed",
        artifact=artifact_path.name,
        strategy_name=strategy_name,
        solver_id=str(entry["solver_id"]),
        gate_profile=str(entry["gate_profile"]),
        market_config_hash=str(entry["config_hash"]),
        execution=dict(artifact.get("execution", {}) or {}),
        activation=dict(artifact.get("activation", {}) or {}),
    )

    # This command promotes one explicitly selected market.  Do not apply the
    # combined three-market policy to a market-local comparison.
    policy = PromotionPolicy.load().for_independent_market()
    decision = compare_with_incumbent(
        candidate_reports,
        incumbent_reports,
        policy,
    )
    record_promotion_decision(
        run_id,
        summaries,
        decision,
        policy,
        incumbent_run_id=incumbent.run_id,
        incumbent_strategy=incumbent.strategy_name,
        root=OPTIMIZER_ROOT,
    )
    activated = False
    if decision.passed and policy.auto_activate_if_better:
        activated = publish_complete_run(
            run_id,
            strategy_name,
            str(manifest.get("timestamp", "")),
            summaries,
        required_groups=(group,),
        all_groups=(group,),
        root=OPTIMIZER_ROOT,
        activate=True,
        strategy_by_group={group: strategy_name},
    )
    return {
        "run_id": run_id,
        "group": group,
        "candidate_strategy": strategy_name,
        "incumbent_run_id": incumbent.run_id,
        "incumbent_strategy": incumbent_strategy.name,
        "activated": activated,
        "promotion": decision.to_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--group", required=True, choices=GROUPS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = promote_optimizer_run(args.run_id, args.group)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
