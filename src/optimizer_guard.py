"""Lightweight parent process that reports abnormal optimizer termination."""

from __future__ import annotations

import os
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


OPTIMIZER_GROUPS = ("a_share", "hk", "us")
CHILD_MARKER = "OPTIMIZER_GUARD_CHILD"


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip())


def _discover_run(
    runs_root: Path,
    previous: set[str],
    started_epoch: float,
) -> Path | None:
    if not runs_root.exists():
        return None
    directories = [item for item in runs_root.iterdir() if item.is_dir()]
    created = [item for item in directories if item.name not in previous]
    if created:
        return max(created, key=lambda item: item.stat().st_mtime)
    recent = [
        item
        for item in directories
        if item.stat().st_mtime >= started_epoch - 1.0
    ]
    return max(recent, key=lambda item: item.stat().st_mtime) if recent else None


def _discover_runs(
    runs_root: Path,
    previous: set[str],
    started_epoch: float,
) -> dict[str, Path]:
    """Discover the independent child run for each market, if it exists."""
    if not runs_root.exists():
        return {}
    directories = [item for item in runs_root.iterdir() if item.is_dir()]
    candidates = [
        item
        for item in directories
        if item.name not in previous or item.stat().st_mtime >= started_epoch - 1.0
    ]
    result: dict[str, Path] = {}
    for group in OPTIMIZER_GROUPS:
        matching = [
            item
            for item in candidates
            if item.name.endswith(f"_{group}")
        ]
        if matching:
            result[group] = max(matching, key=lambda item: item.stat().st_mtime)
    return result


def _failure_reason(returncode: int) -> str:
    if returncode < 0:
        signal_number = -returncode
        if signal_number == 9:
            return (
                "optimizer child received SIGKILL (signal 9); "
                "check the system journal for an OOM or external kill"
            )
        return f"optimizer child terminated by signal {signal_number}"
    if returncode in {9, 137}:
        return (
            f"optimizer child exited with code {returncode}; "
            "check the system journal for an OOM or external kill"
        )
    return f"optimizer child exited with code {returncode}"


def _group_summary(application, run_dir: Path | None, group: str):
    summary_type = application.OptimizerGroupSummary
    if run_dir is None:
        return summary_type(group=group)
    artifact = run_dir / f"{group}_best_params.yaml"
    if artifact.exists():
        try:
            payload = application.yaml.safe_load(
                artifact.read_text(encoding="utf-8")
            ) or {}
            search = payload.get("search", {}) or {}
            sensitivity = payload.get("sensitivity", {}) or {}
            activation = payload.get("activation", {}) or {}
            survivor_count = int(search.get("survivor_count", 0) or 0)
            return summary_type(
                group=group,
                candidate_count=survivor_count,
                evaluated_count=int(search.get("evaluated_count", 0) or 0),
                survivor_count=survivor_count,
                wf_score=float(payload.get("wf_score", 0.0) or 0.0),
                params=dict(payload.get("params", {}) or {}),
                execution=dict(payload.get("execution", {}) or {}),
                ranking_window_count=int(
                    search.get("ranking_window_count", 0) or 0
                ),
                validation_window_count=int(
                    search.get("validation_window_count", 0) or 0
                ),
                purged_window_count=int(
                    search.get("purged_overlap_window_count", 0) or 0
                ),
                ranking_diagnostics=dict(
                    search.get("ranking_diagnostics", {}) or {}
                ),
                sensitivity=dict(sensitivity),
                activation=dict(activation),
                status="completed",
                artifact=artifact.name,
            )
        except Exception as exc:
            # Best-effort parse of a partial artifact; fall back to the
            # search archive line count below.
            print(f"optimizer guard: could not parse {artifact}: {exc}", file=sys.stderr)
    evaluated = _line_count(run_dir / f"{group}_search_archive.jsonl")
    return summary_type(
        group=group,
        evaluated_count=evaluated,
        status="interrupted" if evaluated else "not_run",
    )


def _write_failure_state(
    application,
    run_dir: Path | None,
    report,
    returncode: int,
) -> None:
    if run_dir is None:
        return
    payload = {
        "status": report.status,
        "failure_reason": report.failure_reason,
        "returncode": int(returncode),
        "timestamp": report.timestamp,
        "elapsed_seconds": float(report.elapsed_seconds),
        "groups": {
            group: {
                "status": summary.status,
                "evaluated_count": int(summary.evaluated_count),
                "artifact": summary.artifact,
            }
            for group, summary in report.groups.items()
        },
    }
    path = run_dir / "optimizer_failure.yaml"
    path.write_text(
        application.yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _notify_failure(
    repository: Path,
    previous_runs: set[str],
    started_at: datetime,
    started_epoch: float,
    elapsed_seconds: float,
    returncode: int,
) -> None:
    sys.path.insert(0, str(repository))
    import main as application

    config = application.load_config()
    runs_root = repository / "data" / "optimizer" / "runs"
    run_dirs = _discover_runs(runs_root, previous_runs, started_epoch)
    try:
        market_configs = application.get_market_optimizer_configs(config)
    except Exception as exc:
        print(
            f"optimizer guard: invalid market configuration: {exc}",
            file=sys.stderr,
        )
        market_configs = {}
    summaries = {}
    strategy_by_group = {}
    run_ids_by_group = {}
    for group in OPTIMIZER_GROUPS:
        run_dir = run_dirs.get(group)
        summary = _group_summary(application, run_dir, group)
        market_config = market_configs.get(group)
        if market_config is not None:
            strategy = market_config.strategy
            summary.strategy_name = strategy.name
            summary.strategy_label = strategy.label
            summary.solver_id = market_config.solver_id
            summary.gate_profile = market_config.gate_profile
            summary.walk_forward_profile = market_config.walk_forward_profile
            summary.execution_profile = market_config.execution_profile
            summary.benchmark_profile = market_config.benchmark_profile
            summary.market_config_hash = market_config.config_hash
            strategy_by_group[group] = strategy.name
        if run_dir is not None:
            summary.run_id = run_dir.name
            run_ids_by_group[group] = run_dir.name
        summaries[group] = summary
    report = application.OptimizerRunSummary(
        strategy_name="",
        strategy_label="按市场独立搜参",
        timestamp=started_at.isoformat(),
        elapsed_seconds=elapsed_seconds,
        groups=summaries,
        activated=False,
        run_id="",
        candidate=False,
        status="failed",
        failure_reason=_failure_reason(returncode),
        strategy_by_group=strategy_by_group,
        run_ids_by_group=run_ids_by_group,
    )
    for run_dir in run_dirs.values():
        _write_failure_state(application, run_dir, report, returncode)
    application._notify_optimizer_run(config, report)
    if market_configs:
        keep_completed = max(
            market_config.search.run_retention_count
            for market_config in market_configs.values()
        )
        application.prune_optimizer_runs(
            root=repository / "data" / "optimizer",
            keep_completed=keep_completed,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard the optimizer child process")
    parser.add_argument(
        "--group",
        choices=OPTIMIZER_GROUPS,
        help="run and report only one independent optimizer market",
    )
    # ``main()`` is also called directly by the test suite, where the process
    # argv belongs to pytest.  The guard only owns its explicit ``--group``
    # option, so ignore unrelated host-process arguments in that case.
    args, _unknown = parser.parse_known_args(argv)
    repository = Path(__file__).resolve().parent.parent
    runs_root = repository / "data" / "optimizer" / "runs"
    previous_runs = (
        {item.name for item in runs_root.iterdir() if item.is_dir()}
        if runs_root.exists()
        else set()
    )
    started_at = datetime.now()
    started_epoch = time.time()
    started = time.monotonic()
    environment = dict(os.environ)
    environment[CHILD_MARKER] = "1"
    child_argv = [sys.executable, str(repository / "main.py"), "--optimize"]
    if args.group:
        child_argv.extend(["--group", args.group])
    completed = subprocess.run(
        child_argv,
        cwd=repository,
        env=environment,
        check=False,
    )
    if completed.returncode == 0:
        return 0
    try:
        _notify_failure(
            repository,
            previous_runs,
            started_at,
            started_epoch,
            time.monotonic() - started,
            completed.returncode,
        )
    except Exception as exc:
        print(f"optimizer guard could not send failure summary: {exc}", file=sys.stderr)
    if completed.returncode < 0:
        return 128 + min(-completed.returncode, 127)
    return completed.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
