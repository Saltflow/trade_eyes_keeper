"""Trusted host data preparation and offline container research entrypoints.

``prepare`` never imports the approved experiment. ``execute`` evaluates the
frozen strategy using the shared raw-price execution contract. ``custom`` runs
approved Python in a second container with immutable baseline evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import runpy
import shutil
import signal
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .research import _SECRET_KEY, _json_write, _safe_path, _sha

_SECRET_VALUES: set[str] = set()


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        for secret in _SECRET_VALUES:
            value = value.replace(secret, "[REDACTED]")
    return value


def _configure_redaction(runtime: dict) -> None:
    def collect(mapping: dict) -> None:
        for key, value in mapping.items():
            if isinstance(value, dict):
                collect(value)
            elif (
                _SECRET_KEY.search(str(key))
                and isinstance(value, str)
                and len(value) >= 4
            ):
                _SECRET_VALUES.add(value)

    collect(runtime)
    collect(dict(os.environ))

    class RedactFilter(logging.Filter):
        def filter(self, record):
            record.msg = _redact(record.getMessage())
            record.args = ()
            return True

    logging.basicConfig(level=logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactFilter())


def _read_job(input_dir: Path) -> dict:
    return json.loads((input_dir / "job.json").read_text(encoding="utf-8"))


def _cached_inputs(config: dict, spec: dict, resolved: dict):
    """Use existing validator/calendar contracts for network-free smoke checks."""
    from ...data.backtest_data import (
        BacktestDataResult,
        DataReadinessIssue,
        validate_market_bundle,
    )
    from ...data.market_calendar import resolve_market_data_cutoff
    from ...data.market_history import PointInTimeMarketStore

    start, end = (
        date.fromisoformat(resolved["data_start"]),
        date.fromisoformat(spec["end"]),
    )
    result = BacktestDataResult("feishu_research_cached_smoke", start, end)
    settings = config["point_in_time_data"]
    store = PointInTimeMarketStore(settings["output_dir"])
    tolerance = int(
        settings.get("market_history", {}).get("coverage_tolerance_days", 31)
    )
    for code in dict.fromkeys([*spec["codes"], *resolved["benchmark_codes"]]):
        try:
            cutoff = resolve_market_data_cutoff(
                code, end, as_of=datetime.now(timezone.utc)
            )
            result.market_cutoffs[code] = cutoff.as_dict()
            item = store.read(code)
            if item is None:
                raise ValueError(
                    "cached market bundle is absent; smoke checks never fetch"
                )
            result.bundles[code] = validate_market_bundle(
                item,
                code,
                start,
                cutoff.effective_end,
                tolerance_days=tolerance,
            )
            result.reused_codes.append(code)
        except (ValueError, OSError) as exc:
            result.issues.append(
                DataReadinessIssue(code, "cache", str(start), str(end), str(exc))
            )
    return result


def prepare_inputs(
    job_dir: Path, project_root: Path, *, cached_only: bool = False
) -> dict:
    """Supplement real inputs through the shared providers, then select a copy."""
    import pandas as pd

    from ...core.config_store import ConfigStore
    from ...data.backtest_data import prepare_backtest_data
    from ...data.market_history import PointInTimeMarketStore
    from ...instruments.point_in_time import PointInTimeFundamentalStore
    from ...strategy import get_strategy

    job_dir, project_root = job_dir.resolve(), project_root.resolve()
    os.chdir(project_root)
    input_dir = job_dir / "input"
    job = _read_job(input_dir)
    spec, resolved = job["spec"], job["resolved"]
    config = json.loads((job_dir / "host_config.json").read_text(encoding="utf-8"))
    config = deepcopy(config)
    # Frozen modules have a task-local PROJECT_ROOT. Resolve this shared state
    # against the actual deployment before any provider controller is created.
    access = config.setdefault("provider_access", {}).setdefault("baostock", {})
    state_dir = Path(access.get("state_dir") or "data/provider_state/baostock")
    access["state_dir"] = str(
        (state_dir if state_dir.is_absolute() else project_root / state_dir).resolve()
    )
    # Credentials stay in host memory only. Freeze all non-secret behavior;
    # loading a current key permits credential rotation while a job is queued.
    runtime = ConfigStore(project_root / "config/config.yaml").load_runtime()
    _configure_redaction(runtime)
    api_key = runtime.get("llm", {}).get("api_key")
    if api_key:
        config.setdefault("llm", {})["api_key"] = api_key
    settings = config.setdefault("point_in_time_data", {})
    seed_root = input_dir / "data_seed"
    preparation_cache = job_dir / "preparation_cache"
    if spec.get("dataset_id"):
        # The selected catalog dataset is an immutable, job-local source. Copy
        # only the symbols frozen during preview; shared provider backfill may
        # supplement the private cache without mutating the imported dataset.
        _safe_path(seed_root, job_dir)
        if not seed_root.is_dir():
            raise ValueError("approved source dataset snapshot is missing")
        for relative in ("market", "fundamentals"):
            source_dir = seed_root / relative
            if source_dir.exists():
                _safe_path(source_dir, job_dir)
                for source in source_dir.rglob("*"):
                    if source.is_file():
                        _safe_path(source, seed_root, regular=True)
                        target = preparation_cache / relative / source.relative_to(source_dir)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
        source_root = preparation_cache
        settings["output_dir"] = str(source_root)
    else:
        if resolved.get("host_data_root"):
            settings["output_dir"] = resolved["host_data_root"]
        source_root = Path(settings.get("output_dir", "data/point_in_time"))
        if not source_root.is_absolute():
            source_root = project_root / source_root
    settings["output_dir"] = str(source_root)
    requested = set(spec["codes"]) | set(resolved["benchmark_codes"])
    # The shared backfill service normally expands its full watchlist. A
    # research task approves only this explicit stock and benchmark inventory.
    for key in ("market_only_symbols", "fx_symbols"):
        settings[key] = [
            str(code) for code in settings.get(key, []) if str(code) in requested
        ]
    strategy = get_strategy(resolved["strategy_id"])
    if strategy is None:
        raise ValueError("approved strategy is no longer registered")
    readiness_path = job_dir / "data_readiness.json"
    prepared = (
        _cached_inputs(config, spec, resolved)
        if cached_only
        else prepare_backtest_data(
            config,
            spec["codes"],
            resolved["data_start"],
            spec["end"],
            purpose="feishu_research",
            benchmark_codes=resolved["benchmark_codes"],
            strategy=strategy,
            readiness_path=None,
        )
    )
    missing = requested - set(prepared.bundles)
    issues = _redact([item.as_dict() for item in prepared.issues])
    issues.extend(
        {"code": code, "reason": "market bundle unavailable"}
        for code in sorted(missing)
    )
    data_root = input_dir / "data"
    data_root.mkdir()
    market_store = PointInTimeMarketStore(data_root)
    for code in sorted(requested & set(prepared.bundles)):
        bundle = prepared.bundles[code]
        # The source store remains shared; the container sees only approved
        # symbols and their accepted completed-session range.
        dates = pd.to_datetime(bundle.prices["date"]).dt.date
        bundle.prices = bundle.prices.loc[
            dates >= date.fromisoformat(resolved["data_start"])
        ].copy()
        if bundle.prices.empty:
            issues.append({"code": code, "reason": "empty evaluation data"})
            continue
        market_store.write(bundle)
    if resolved["fundamental_dependencies"] or spec.get("dataset_id"):
        source_store = PointInTimeFundamentalStore(source_root)
        target_store = PointInTimeFundamentalStore(data_root)
        for code in spec["codes"]:
            source = _safe_path(source_store._path(code), source_root)
            if not source.is_file():
                if resolved["fundamental_dependencies"]:
                    issues.append(
                        {"code": code, "reason": "required PIT statements missing"}
                    )
                continue
            rows = [
                row
                for row in source_store.read_all(code)
                if row.published_at is not None
                and row.published_at <= date.fromisoformat(spec["end"])
            ]
            if not rows:
                if resolved["fundamental_dependencies"]:
                    issues.append(
                        {
                            "code": code,
                            "reason": "no statements published by evaluation end",
                        }
                    )
                continue
            target_store.upsert(code, rows)
        pending = prepared.fundamental_backfill.get("pending_codes", [])
        issues.extend(
            {"code": code, "reason": "fundamental backfill not completed"}
            for code in pending
            if code in spec["codes"]
        )
    files = {
        path.relative_to(data_root).as_posix(): _sha(path)
        for path in data_root.rglob("*")
        if path.is_file()
    }
    manifest = {
        "contract": "feishu-research-inputs/1",
        "cached_only": cached_only,
        "ready": not issues,
        "requested_codes": spec["codes"],
        "benchmark_codes": resolved["benchmark_codes"],
        "source_dataset_id": spec.get("dataset_id"),
        "source_market_codes": resolved.get("source_market_codes", []),
        "source_statement_codes": resolved.get("source_statement_codes", []),
        "data_start": resolved["data_start"],
        "end": spec["end"],
        "market_cutoffs": prepared.market_cutoffs,
        "issues": issues,
        "files": files,
        "raw_prices": True,
        "corporate_actions": True,
        "explicit_dividends_and_withholding": True,
    }
    _json_write(input_dir / "data_manifest.json", manifest)
    readiness = _redact(prepared.as_dict())
    readiness["issues"] = issues
    _json_write(readiness_path, readiness)
    if issues:
        raise ValueError(
            "data readiness failed: " + json.dumps(issues, ensure_ascii=False)[:1500]
        )
    return manifest


def _enricher_with_coverage(strategy, config: dict, resolved: dict, spec: dict):
    """Use the strategy's existing causal join, rejecting silent missing symbols."""
    enricher = strategy.make_context_enricher(
        config,
        market=resolved["market"],
        symbols=tuple(spec["codes"]),
    )
    if not resolved["fundamental_dependencies"]:
        return enricher
    if enricher is None:
        raise ValueError("strategy requires a PIT context which is not configured")
    snapshots = getattr(enricher, "snapshots", None)
    if snapshots is not None:
        covered = {
            item.symbol
            for item in snapshots
            if item.feature_date <= date.fromisoformat(spec["end"])
        }
    elif getattr(enricher, "dataset", None) is not None:
        covered = set(enricher.dataset.symbols)
    else:
        raise ValueError("research context does not expose auditable symbol coverage")
    missing = set(spec["codes"]) - covered
    if missing:
        raise ValueError(
            "required valuation context missing for: " + ", ".join(sorted(missing))
        )
    return enricher


def run_registered(
    input_dir: Path = Path("/input"), output_dir: Path = Path("/output")
) -> dict:
    """Run the registered adapter using the same daily/IM evaluation engine."""
    import numpy as np
    import pandas as pd

    from ...backtest.engine import evaluate_all_groups
    from ...data.market_history import PointInTimeMarketStore
    from ...search.config import get_market_optimizer_config
    from ...strategy import Params, get_strategy

    job = _read_job(input_dir)
    spec, resolved = job["spec"], job["resolved"]
    manifest = json.loads(
        (input_dir / "data_manifest.json").read_text(encoding="utf-8")
    )
    if not manifest.get("ready"):
        raise ValueError("research inputs were not prepared")
    for name, digest in manifest["files"].items():
        path = _safe_path(input_dir / "data" / name, input_dir / "data", regular=True)
        if _sha(path) != digest:
            raise ValueError("input inventory hash mismatch")
    config = yaml.safe_load(
        (input_dir / "code/config/config.yaml").read_text(encoding="utf-8")
    )
    # Tests and local offline diagnostics can use a temporary input root; all
    # container invocations naturally retain /input/data.
    config["point_in_time_data"]["output_dir"] = str(input_dir / "data")
    market_config = get_market_optimizer_config(
        resolved["market"],
        application_config=config,
        constraints_path=input_dir / "code/config/optimizer_constraints.yaml",
    )
    store = PointInTimeMarketStore(input_dir / "data")
    all_codes = list(dict.fromkeys([*spec["codes"], *resolved["benchmark_codes"]]))
    bundles = {code: store.read(code) for code in all_codes}
    if any(bundle is None for bundle in bundles.values()):
        raise ValueError("an approved market/benchmark bundle is absent")
    stocks = {}
    for code in spec["codes"]:
        frame = bundles[code].prices
        stocks[code] = pd.DataFrame(
            {
                "date": pd.to_datetime(frame["date"]),
                **{
                    field: pd.to_numeric(frame[f"qfq_{field}"])
                    for field in ("open", "high", "low", "close")
                },
                "volume": pd.to_numeric(frame["volume"]),
                "tradable": frame["tradable"].astype(bool),
            }
        )
    strategy = get_strategy(resolved["strategy_id"])
    if strategy is None:
        raise ValueError("strategy missing from frozen code")
    params = Params(
        values=resolved["parameters"],
        _engine=strategy.name,
        execution_snapshot=resolved["execution_snapshot"],
    )
    enricher = _enricher_with_coverage(strategy, config, resolved, spec)
    reports = evaluate_all_groups(
        stocks,
        spec["codes"],
        strategy,
        params,
        market_config.execution,
        market_bundles={code: bundles[code] for code in spec["codes"]},
        benchmark_bundles={code: bundles[code] for code in resolved["benchmark_codes"]},
        target_groups=[resolved["market"]],
        start_date=spec["start"],
        end_date=spec["end"],
        context_enricher=enricher,
        market_constraints=market_config.constraints,
    )
    report = reports.get(resolved["market"])
    if report is None:
        raise ValueError("shared backtest produced no evaluation report")
    metrics = {
        key: getattr(report, key)
        for key in (
            "total_return",
            "max_drawdown",
            "sharpe_ratio",
            "trade_count",
            "benchmark_returns",
        )
    }
    payload = {
        "contract": "feishu-research-shared-execution/1",
        "formal_acceptance": False,
        "job_id": job["job_id"],
        "spec": spec,
        "strategy_id": strategy.name,
        "market": resolved["market"],
        "metrics": metrics,
        "evaluation_report": asdict(report),
        "context_contract": str(getattr(enricher, "contract", "")),
        "context_hash": str(getattr(enricher, "contract_hash", "")),
        "data_manifest": manifest,
    }

    def convert(value):
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (date, pd.Timestamp)):
            return value.isoformat()
        raise TypeError(f"unsupported report value: {type(value).__name__}")

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(
            payload, ensure_ascii=False, indent=2, default=convert, allow_nan=False
        ),
        encoding="utf-8",
    )
    with (output_dir / "nav.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "nav"])
        writer.writerows(zip(report.nav_dates, report.nav_series))
    if report.nav_dates and report.nav_series:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import pyplot as plt

        figure, axis = plt.subplots(figsize=(9, 4))
        axis.plot(pd.to_datetime(report.nav_dates), report.nav_series)
        axis.set(
            title=f"{strategy.name} | {resolved['market']} | research", ylabel="NAV"
        )
        axis.grid(alpha=0.2)
        figure.tight_layout()
        figure.savefig(output_dir / "nav.png", dpi=140)
        plt.close(figure)
    print(
        json.dumps(
            {"status": "completed", "metrics": metrics},
            ensure_ascii=False,
            default=convert,
        ),
        flush=True,
    )
    return payload


def run_custom() -> None:
    input_dir, output_dir = Path("/input"), Path("/output")
    job = _read_job(input_dir)
    if job["spec"]["script_id"] != "generated":
        raise ValueError("custom execution requires a generated proposal")
    baseline = json.loads(
        (input_dir / "baseline/report.json").read_text(encoding="utf-8")
    )
    runpy.run_path(
        str(input_dir / "research.py"),
        run_name="__main__",
        init_globals={
            "CONTEXT": {
                "spec": job["spec"],
                "baseline": baseline,
                "input_dir": str(input_dir),
                "output_dir": str(output_dir),
            },
        },
    )


def _watch_parent(parent_pid: int) -> None:
    """A killed service must not leave a host data-download process running."""

    def watch() -> None:
        while True:
            time.sleep(1)
            alive = True
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.windll.kernel32
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, parent_pid)
                if handle:
                    alive = kernel.WaitForSingleObject(handle, 0) == 0x102
                    kernel.CloseHandle(handle)
                else:
                    alive = False
            else:
                alive = os.getppid() == parent_pid
            if not alive:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                else:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(1)

    threading.Thread(target=watch, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "execute", "custom"))
    parser.add_argument("--job-dir", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            if not args.job_dir or not args.project_root:
                parser.error("prepare requires --job-dir and --project-root")
            if args.parent_pid:
                _watch_parent(args.parent_pid)
            prepare_inputs(
                args.job_dir, args.project_root, cached_only=args.cached_only
            )
        elif args.mode == "execute":
            run_registered()
        else:
            run_custom()
    except Exception as exc:  # noqa: BLE001 - process boundary must report failure
        message = _redact(str(exc))[:2000]
        if args.mode == "prepare" and args.job_dir:
            target = _safe_path(args.job_dir / "data_readiness.json", args.job_dir)
            if not target.exists():
                _json_write(target, {"ready": False, "issues": [{"reason": message}]})
        print(f"{type(exc).__name__}: {message}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
