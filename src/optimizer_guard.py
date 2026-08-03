"""Lightweight parent process that reports abnormal optimizer termination."""

from __future__ import annotations

import os
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
        except Exception:
            pass
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
    strategy = application._get_configured_strategy(config)
    runs_root = repository / "data" / "optimizer" / "runs"
    run_dir = _discover_run(runs_root, previous_runs, started_epoch)
    run_id = run_dir.name if run_dir is not None else ""
    inferred_name = run_id.partition("_")[2] if "_" in run_id else ""
    strategy_name = getattr(strategy, "name", "") or inferred_name or "unknown"
    strategy_label = getattr(strategy, "label", "") or strategy_name
    report = application.OptimizerRunSummary(
        strategy_name=strategy_name,
        strategy_label=strategy_label,
        timestamp=started_at.isoformat(),
        elapsed_seconds=elapsed_seconds,
        groups={
            group: _group_summary(application, run_dir, group)
            for group in OPTIMIZER_GROUPS
        },
        activated=False,
        run_id=run_id,
        candidate=False,
        status="failed",
        failure_reason=_failure_reason(returncode),
    )
    _write_failure_state(application, run_dir, report, returncode)
    application._notify_optimizer_run(config, report)


def main() -> int:
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
    completed = subprocess.run(
        [sys.executable, str(repository / "main.py"), "--optimize"],
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
