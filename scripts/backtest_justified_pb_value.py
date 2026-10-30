#!/usr/bin/env python3
"""Evaluate the justified_pb_value strategy over the 84-month A-share horizon.

Pipeline: PIT store (market bundles + statements) -> quarterly fundamental
dataset -> historical enricher -> WalkForwardManager -> fixed-parameter
evaluation.  Reports the full-period metrics (return / max drawdown / Sharpe /
benchmarks) and the authoritative 22-window walk-forward table, and writes
JSON/CSV artifacts.  This script never touches production configuration.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import (
    _load_optimizer_benchmark_bundles,
    _load_optimizer_market_bundles_with_errors,
    _optimizer_lookback_days,
    _stock_code,
    load_config,
)
from src.backtest.engine import FastEvaluator, WalkForwardManager, WindowSlice
from src.instruments.classifier import detect_market
from src.instruments.point_in_time import PointInTimeFundamentalStore
from src.markets import _detect_fine_group
from src.search.config import get_constraints
from src.search.workflow import _evaluate_params_wf, _partition_window_indexes
from src.strategy import Params, get_strategy
from src.strategy.justified_pb_value_context import (
    make_justified_pb_value_context,
)
from src.strategy.plugins.justified_pb_value import (
    GROWTH,
    KE_BUY,
    KE_SELL,
    PER_SYMBOL_CAP,
    TOTAL_EXPOSURE_CAP,
)

STRATEGY_ID = "justified_pb_value"
MARKET = "a_share"


def _a_share_configured_codes(config: dict) -> list[str]:
    """All A-share configured stocks (skip lists intentionally ignored)."""
    codes = []
    for stock in config.get("stocks", []) or []:
        code = _stock_code(stock)
        if code and _detect_fine_group(code) == MARKET:
            codes.append(code)
    return sorted(set(codes))


def _statement_codes(root: Path) -> list[str]:
    store = PointInTimeFundamentalStore(root)
    codes = []
    for path in sorted((root / "fundamentals").glob("*.statements.json")):
        code = path.name.split(".statements.json")[0]
        if detect_market(code) == MARKET:
            codes.append(code)
    return sorted(set(codes))


def _full_window(manager: WalkForwardManager, constraints) -> WindowSlice:
    """One pseudo window covering the newest 84 calendar months."""
    end_exclusive = pd.to_datetime(manager.dates[-1]).normalize()
    end_exclusive = end_exclusive + pd.Timedelta(days=1)
    horizon_start = end_exclusive - pd.DateOffset(
        months=constraints.walk_forward.total_months_needed,
    )
    test_start = int(manager.dates.searchsorted(horizon_start, side="left"))
    test_start = max(1, test_start)
    if test_start >= len(manager.dates):
        raise RuntimeError("84-month horizon exceeds available market data")
    return WindowSlice(
        train_start=0,
        train_end=test_start,
        test_start=test_start,
        test_end=len(manager.dates),
        window_index=0,
        train_start_date=str(manager.dates[0].date()),
        train_end_date=str(manager.dates[test_start - 1].date()),
        test_start_date=str(manager.dates[test_start].date()),
        test_end_date=str(manager.dates[-1].date()),
    )


def _stats_row(stat, window: WindowSlice, partition: str) -> dict:
    benchmarks = dict(getattr(stat, "benchmark_returns", {}) or {})
    return {
        "window_index": int(window.window_index),
        "partition": partition,
        "test_start": str(window.test_start_date),
        "test_end": str(window.test_end_date),
        "return_pct": float(getattr(stat, "strategy_return", 0.0) or 0.0),
        "excess_vs_strongest_pct": float(
            getattr(stat, "test_excess_return", 0.0) or 0.0
        ),
        "max_drawdown_pct": float(getattr(stat, "max_drawdown_pct", 0.0) or 0.0),
        "sharpe_ratio": float(getattr(stat, "sharpe_ratio", 0.0) or 0.0),
        "avg_position_pct": float(getattr(stat, "avg_position_pct", 0.0) or 0.0),
        "total_trades": int(getattr(stat, "total_trades", 0) or 0),
        "signal_events": int(getattr(stat, "signal_event_count", 0) or 0),
        "benchmark_returns": benchmarks,
        "strongest_benchmark": str(getattr(stat, "strongest_benchmark", "") or ""),
    }


def _summary(rows: list[dict]) -> dict:
    returns = [row["return_pct"] for row in rows]
    drawdowns = [row["max_drawdown_pct"] for row in rows]
    sharpes = [row["sharpe_ratio"] for row in rows]
    benchmark_keys = sorted({
        key
        for row in rows
        for key in row["benchmark_returns"]
    })
    wins = {}
    for key in benchmark_keys:
        wins[key] = sum(
            1
            for row in rows
            if row["benchmark_returns"].get(key) is not None
            and row["return_pct"] > float(row["benchmark_returns"][key])
        )
    return {
        "window_count": len(rows),
        "mean_return_pct": float(np.mean(returns)) if returns else None,
        "median_return_pct": float(np.median(returns)) if returns else None,
        "worst_drawdown_pct": float(min(drawdowns)) if drawdowns else None,
        "mean_sharpe": float(np.mean(sharpes)) if sharpes else None,
        "winning_benchmarks": wins,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "windows", "both"), default="both")
    parser.add_argument("--output-root", type=Path,
                        default=ROOT / "data" / "analysis" / "justified_pb_value"
    )
    parser.add_argument("--data-root", type=Path, default=None,
                        help="point-in-time store root (default: from config)")
    args = parser.parse_args()

    config = load_config()
    if args.data_root is not None:
        config.setdefault("point_in_time_data", {})[
            "output_dir"] = str(args.data_root)
    pit_root = Path(config.get("point_in_time_data", {}).get(
        "output_dir", "data/point_in_time"
    ))

    configured = _a_share_configured_codes(config)
    with_statements = _statement_codes(pit_root)
    universe = sorted(set(configured) & set(with_statements))
    print("configured A-share codes: %d" % len(configured))
    print("A-share codes with PIT statements: %d" % len(with_statements))
    if not universe:
        raise SystemExit("empty justified-PB universe; check PIT fundamentals")
    print("universe: %s" % ", ".join(universe))

    constraints = get_constraints()
    constraints.set_group(MARKET)
    lookback_days = _optimizer_lookback_days(constraints)
    bundles, coverage_errors = _load_optimizer_market_bundles_with_errors(
        config, universe, lookback_days
    )
    if coverage_errors:
        print("[warn] coverage exclusions: %s" % "; ".join(coverage_errors))
    universe = [code for code in universe if code in bundles]
    if not universe:
        raise SystemExit("no universe codes survive the 84-month coverage check")
    print("evaluated universe: %s" % ", ".join(universe))

    benchmark_bundles = _load_optimizer_benchmark_bundles(
        config, constraints, MARKET, lookback_days
    )
    enricher = make_justified_pb_value_context(
        config, market=MARKET, symbols=tuple(universe)
    )
    dataset = enricher.dataset

    manager = WalkForwardManager(
        {}, constraints, list(universe),
        benchmark_bundles=benchmark_bundles,
        market_bundles=bundles,
    )
    manager.market_group = MARKET
    manager.market_data_enricher = enricher
    windows = manager.iter_windows()
    expected = constraints.walk_forward.num_windows
    if len(windows) != expected:
        print(
            "[warn] walk-forward windows: %d != expected %d" % (len(windows), expected)
        )
    ranking_indexes, purged_indexes, holdout_indexes = (
        _partition_window_indexes(windows, constraints)
    )
    labels = {}
    for index in ranking_indexes:
        labels[index] = "ranking"
    for index in purged_indexes:
        labels[index] = "purged"
    for index in holdout_indexes:
        labels[index] = "holdout"

    evaluator = FastEvaluator(constraints.execution, MARKET)
    params = Params(values={}, _engine=STRATEGY_ID)
    strategy = get_strategy(STRATEGY_ID)
    if strategy is None:
        raise SystemExit("strategy %s is not registered" % STRATEGY_ID)

    output_dir = args.output_root / datetime.now(timezone.utc).astimezone().strftime(
        "%Y%m%d_%H%M%S"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    readiness = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market": MARKET,
        "strategy": STRATEGY_ID,
        "contract": {
            "ke_buy": KE_BUY,
            "ke_sell": KE_SELL,
            "growth": GROWTH,
            "per_symbol_cap": PER_SYMBOL_CAP,
            "total_exposure_cap": TOTAL_EXPOSURE_CAP,
        },
        "pit_root": str(pit_root),
        "market_end_date": str(manager.dates[-1].date()),
        "universe": list(universe),
        "configured_codes": configured,
        "statement_span": {},
        "coverage_exclusions": list(coverage_errors),
        "windows": {
            "expected": expected,
            "actual": len(windows),
            "ranking": len(ranking_indexes),
            "purged": len(purged_indexes),
            "holdout": len(holdout_indexes),
        },
        "benchmarks": sorted(set(benchmark_bundles)),
        "context": {
            "contract": str(dataset.metadata.get("contract", "")),
            "contract_hash": str(enricher.contract_hash),
            "rows": int(len(dataset.symbols)),
            "feature_dates": (
                [str(min(dataset.feature_dates)), str(max(dataset.feature_dates))]
            ) if len(dataset.feature_dates) else [],
        },
    }
    store = PointInTimeFundamentalStore(pit_root)
    for code in universe:
        records = store.read_all(code)
        if records:
            periods = [str(item.period_end) for item in records if item.period_end]
            readiness["statement_span"][code] = {
                "records": len(records),
                "first": periods[0] if periods else None,
                "last": periods[-1] if periods else None,
            }

    # ── full-period evaluation (84 months) ──
    full_report = None
    if args.mode in ("full", "both"):
        full_window = _full_window(manager, constraints)
        full_eval = _evaluate_params_wf(
            params, strategy, [full_window], constraints, evaluator, manager,
            validation_window_count=0,
        )
        full_stats = full_eval[0] if full_eval else None
        if full_stats:
            stat = full_stats[0]
            holdings = []
            if getattr(stat, "final_shares", None) is not None:
                shares = np.asarray(stat.final_shares, dtype=float)
                final_prices = np.asarray(stat.final_prices, dtype=float)
                costs = np.asarray(stat.cost_basis, dtype=float)
                for index, code in enumerate(universe):
                    if shares[index] > 0.5:
                        price = (
                            float(final_prices[-1, index])
                            if final_prices.ndim > 1
                            else float(final_prices[index])
                        )
                        cost = (
                            float(costs[-1, index])
                            if costs.ndim > 1
                            else float(costs[index])
                        ) if costs.ndim else 0.0
                        holdings.append({
                            "code": code,
                            "shares": float(shares[index]),
                            "price": price,
                            "cost": cost,
                        })
            full_report = {
                "test_start": str(full_window.test_start_date),
                "test_end": str(full_window.test_end_date),
                "return_pct": float(getattr(stat, "strategy_return", 0.0) or 0.0),
                "max_drawdown_pct": float(
                    getattr(stat, "max_drawdown_pct", 0.0) or 0.0
                ),
                "sharpe_ratio": float(getattr(stat, "sharpe_ratio", 0.0) or 0.0),
                "avg_position_pct": float(
                    getattr(stat, "avg_position_pct", 0.0) or 0.0
                ),
                "final_position_pct": float(
                    getattr(stat, "final_position_pct", 0.0) or 0.0
                ),
                "total_trades": int(getattr(stat, "total_trades", 0) or 0),
                "signal_events": int(getattr(stat, "signal_event_count", 0) or 0),
                "benchmark_returns": dict(getattr(stat, "benchmark_returns", {}) or {}),
                "excess_vs_strongest_pct": float(
                    getattr(stat, "test_excess_return", 0.0) or 0.0
                ),
                "strongest_benchmark": str(
                    getattr(stat, "strongest_benchmark", "") or ""
                ),
                "selected_basket_hold_return": getattr(
                    stat, "selected_basket_hold_return", None
                ),
                "timing_value_add": getattr(stat, "timing_value_add", None),
                "final_cash": float(getattr(stat, "final_cash", 0.0) or 0.0),
                "holdings": holdings,
            }

    # ── walk-forward window evaluation ──
    wf_report = None
    if args.mode in ("windows", "both"):
        wf_eval = _evaluate_params_wf(
            params, strategy, windows, constraints, evaluator, manager,
        )
        wf_stats = wf_eval[0] if wf_eval else None
        if wf_stats:
            rows = [
                _stats_row(stat, window, labels.get(index, "unknown"))
                for index, (window, stat) in enumerate(zip(windows, wf_stats))
            ]
            ranking = [row for row in rows if row["partition"] == "ranking"]
            purged_rows = [row for row in rows if row["partition"] == "purged"]
            holdout = [row for row in rows if row["partition"] == "holdout"]
            wf_report = {
                "windows": rows,
                "ranking_summary": _summary(ranking),
                "purged_summary": _summary(purged_rows),
                "holdout_summary": _summary(holdout),
            }

    report = {
        "readiness": readiness,
        "full_period": full_report,
        "walk_forward": wf_report,
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if wf_report and wf_report["windows"]:
        with (output_dir / "windows.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "window_index", "partition", "test_start", "test_end",
                "return_pct", "excess_vs_strongest_pct", "max_drawdown_pct",
                "sharpe_ratio", "avg_position_pct", "total_trades", "signal_events",
            ])
            writer.writeheader()
            for row in wf_report["windows"]:
                writer.writerow({
                    key: row[key] for key in ("window_index", "partition", "test_start",
                                               "test_end", "return_pct",
                                               "excess_vs_strongest_pct",
                                               "max_drawdown_pct", "sharpe_ratio",
                                               "avg_position_pct", "total_trades",
                                               "signal_events")
                })

    _print_report(report, universe)
    print()
    print("artifacts saved under: %s" % output_dir)
    return 0


def _print_report(report: dict, universe: list[str]) -> None:
    print()
    print("=" * 78)
    print("Justified-PB 价值策略（A股）")
    print("=" * 78)
    print("标的池: %s" % ", ".join(universe))
    print("数据终止日: %s | 窗口: %d排名/%d隔离/%d保留/%d总 (应%d)" % (
        report["readiness"]["market_end_date"],
        report["readiness"]["windows"]["ranking"],
        report["readiness"]["windows"]["purged"],
        report["readiness"]["windows"]["holdout"],
        report["readiness"]["windows"]["actual"],
        report["readiness"]["windows"]["expected"],
    ))
    full = report.get("full_period")
    if full:
        print()
        print("── 84 个月整段回测 %s → %s ──" % (full["test_start"], full["test_end"]))
        print("收益: %+.2f%% | 最大回撤: %.2f%% | Sharpe: %.4f" % (
            full["return_pct"], full["max_drawdown_pct"], full["sharpe_ratio"],
        ))
        print("平均仓位: %.1f%% | 期末仓位: %.1f%% | 交易: %d | 信号: %d" % (
            full["avg_position_pct"], full["final_position_pct"],
            full["total_trades"], full["signal_events"],
        ))
        print("基准收益: %s" % json.dumps(
            full["benchmark_returns"], ensure_ascii=False
        ))
        print("相对最强基准超额: %+.2f%%（最强: %s）" % (
            full["excess_vs_strongest_pct"], full["strongest_benchmark"]
        ))
        if full.get("holdings"):
            print("期末持仓:")
            for holding in full["holdings"]:
                print("  %s %d股 @ %.2f 成本 %.2f" % (
                    holding["code"], int(holding["shares"]),
                    holding["price"], holding["cost"],
                ))
    wf = report.get("walk_forward")
    if wf and wf["windows"]:
        print()
        print("── 22 窗口 walk-forward ──")
        print("%-4s %-9s %-12s %-12s %9s %8s %8s" % (
            "win", "partition", "test_start", "test_end", "return%", "maxdd%", "sharpe"
        ))
        for row in wf["windows"]:
            print(
                "%-4d %-9s %-12s %-12s %9.2f %8.2f %8.4f" % (
                    row["window_index"], row["partition"], row["test_start"],
                    row["test_end"], row["return_pct"], row["max_drawdown_pct"],
                    row["sharpe_ratio"],
                )
            )
        for key in ("ranking", "purged", "holdout"):
            summary = wf.get(key + "_summary", {})
            if summary is None or not summary.get("window_count"):
                continue
            mean_return = summary["mean_return_pct"]
            median_return = summary["median_return_pct"]
            worst_dd = summary["worst_drawdown_pct"]
            mean_sharpe = summary["mean_sharpe"]
            if mean_return is None:
                mean_return = float("nan")
            if median_return is None:
                median_return = float("nan")
            if worst_dd is None:
                worst_dd = float("nan")
            if mean_sharpe is None:
                mean_sharpe = float("nan")
            print(
                "  %s: %d窗 | 均值收益 %.2f%% | 中位 %.2f%% "
                "| 最差回撤 %.2f%% | 平均Sharpe %.4f | 胜基准: %s"
                % (
                    key,
                    summary["window_count"],
                    mean_return,
                    median_return,
                    worst_dd,
                    mean_sharpe,
                    json.dumps(summary["winning_benchmarks"], ensure_ascii=False),
                )
            )


if __name__ == "__main__":
    raise SystemExit(main())