"""Replay the frozen CAPM-DCF value candidate through the shared backtester.

This is an A-share research command, not an activation path.  It builds the
same causal context and ``TradePlan`` used by daily scans, executes it through
the regular :class:`Backtester`, and writes one auditable JSON report.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import Backtester, build_trade_plan
from src.data.market_history import PointInTimeMarketStore
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.search.config import get_constraints, get_execution_config
from src.strategy import Params, get_strategy
from src.strategy.capm_dcf_value_context import (
    _load_benchmark_csv,
    build_capm_dcf_value_context_enricher,
)

CONTRACT = "capm-dcf-value-unified-backtest-1"


def _read_rates(path: Path) -> dict[date, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = raw.get("risk_free_rates", raw)
    if not isinstance(values, dict):
        raise TypeError("risk-free source must be a date-to-rate mapping")
    return {date.fromisoformat(str(key)): float(value) for key, value in values.items()}


def _qfq_frame(store: PointInTimeMarketStore, symbol: str) -> pd.DataFrame:
    bundle = store.read(symbol)
    if bundle is None:
        raise ValueError(f"point-in-time market history missing: {symbol}")
    fields = [
        "date",
        "qfq_open",
        "qfq_high",
        "qfq_low",
        "qfq_close",
        "volume",
    ]
    missing = [field for field in fields if field not in bundle.prices]
    if missing:
        raise ValueError(f"{symbol} is missing qfq fields: {', '.join(missing)}")
    frame = bundle.prices[fields].copy()
    frame.columns = ["date", "open", "high", "low", "close", "volume"]
    return frame


def _benchmark_frame(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    required = ["date", "open", "high", "low", "close"]
    missing = [field for field in required if field not in raw]
    if missing:
        raise ValueError(f"benchmark {path} lacks: {', '.join(missing)}")
    return raw[required].copy()


def _json_default(value: object) -> object:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--industry-history", required=True)
    parser.add_argument("--risk-free-rates-json", required=True)
    parser.add_argument("--frozen-policy-report", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--benchmark-dir", default="cache/data")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()

    root = Path(args.data_root)
    symbols = sorted({str(symbol) for symbol in args.symbols})
    store = PointInTimeMarketStore(root)
    stocks = {symbol: _qfq_frame(store, symbol) for symbol in symbols}
    rates_path = Path(args.risk_free_rates_json)
    context = build_capm_dcf_value_context_enricher(
        data_root=root,
        benchmark_bundle=_load_benchmark_csv(Path(args.benchmark_prices)),
        risk_free_rates=_read_rates(rates_path),
        industry_history=IndustryClassificationHistoryStore(args.industry_history),
        frozen_policy_report=args.frozen_policy_report,
        symbols=symbols,
    )
    strategy = get_strategy("capm_dcf_value")
    if strategy is None:
        raise RuntimeError("capm_dcf_value is not registered")
    params = Params(values={"buy_cash_tier": 0, "sell_cash_tier": 0})
    trade_plan, market_data, execution_prices, active_codes = build_trade_plan(
        stocks,
        symbols,
        strategy,
        params,
        start_date=args.start,
        end_date=args.end,
        context_enricher=context,
    )
    if trade_plan is None or market_data is None or execution_prices is None:
        raise RuntimeError("shared plan builder produced no executable value plan")

    constraints = get_constraints()
    constraints.set_group("a_share")
    benchmark_dir = Path(args.benchmark_dir)
    benchmark_data = {
        symbol: _benchmark_frame(benchmark_dir / f"{symbol}.csv")
        for symbol in constraints.benchmark_codes_for("a_share")
        if symbol not in {"risk_free", "universe_equal_weight"}
    }
    report = Backtester(get_execution_config(), "a_share").run(
        trade_plan,
        market_data,
        benchmark_data=benchmark_data,
        benchmark_codes=constraints.benchmark_codes_for("a_share"),
        primary_benchmark=constraints.primary_benchmark_for("a_share"),
        risk_free_rate=constraints.risk_free_rate,
        strategy_id=strategy.name,
        strategy_label=strategy.label,
        execution_prices=execution_prices,
    )
    event_rows = [
        {"date": trade_plan.dates[row], "symbol": trade_plan.symbols[column]}
        for row, column in zip(*np.asarray(trade_plan.entry_events).nonzero())
    ]
    output = {
        "contract": CONTRACT,
        "strategy_id": strategy.name,
        "symbols": active_codes,
        "date_range": {
            "start": trade_plan.dates[0],
            "end": trade_plan.dates[-1],
        },
        "context": {
            "contract": context.contract,
            "contract_hash": context.contract_hash,
            "frozen_policy_report": context.policy.source_report,
            "frozen_policy_hash": context.policy.source_hash,
            "train_beta_reference": context.policy.beta_reference,
            "snapshot_count": len(context.snapshots),
            "skipped": dict(context.skipped),
        },
        "entry_event_count": len(event_rows),
        "entry_events": event_rows,
        "evaluation_report": asdict(report),
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(target)
    print(
        json.dumps(
            {
                "output": str(target),
                "entry_event_count": len(event_rows),
                "total_return": report.total_return,
                "max_drawdown": report.max_drawdown,
                "sharpe_ratio": report.sharpe_ratio,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
