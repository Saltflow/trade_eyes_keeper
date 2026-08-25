#!/usr/bin/env python3
"""Run a point-in-time reverse DCF for every usable reference instrument.

This is deliberately a thin batch/reporting layer over the existing valuation
engine.  It uses the engine's historical beta and dated CAPM assumptions, but
keeps the reverse DCF diagnostic-only: the implied growth rate is never fed
back into fair value or into the cash-flow forecast.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_history import PriceHistoryBundle  # noqa: E402
from src.fundamental_embedding.intrinsic_value import (  # noqa: E402
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
    SubjectiveRiskAdjustment,
)

plt.switch_backend("Agg")


def _load_benchmark(path: Path) -> PriceHistoryBundle:
    """Read a real local benchmark quote file for point-in-time beta."""

    raw = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"benchmark is missing columns: {missing}")
    volume = raw["volume"] if "volume" in raw.columns else 0.0
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce"),
            "raw_open": pd.to_numeric(raw["open"], errors="coerce"),
            "raw_high": pd.to_numeric(raw["high"], errors="coerce"),
            "raw_low": pd.to_numeric(raw["low"], errors="coerce"),
            "raw_close": pd.to_numeric(raw["close"], errors="coerce"),
            "volume": pd.to_numeric(volume, errors="coerce").fillna(0.0),
        }
    ).dropna(subset=["date", "raw_close"])
    for name in ("open", "high", "low", "close"):
        frame[f"qfq_{name}"] = frame[f"raw_{name}"]
    frame["qfq_factor"] = 1.0
    frame["tradable"] = frame["volume"] > 0
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    return PriceHistoryBundle(
        code=path.stem,
        prices=frame.reset_index(drop=True),
        source=f"local_real_price_cache:{path.resolve()}",
        diagnostics=[
            "beta benchmark uses unadjusted ETF close; cash dividends omitted"
        ],
    )


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _growth_bucket(value: float | None) -> str:
    """Classify the explicit five-year growth priced into the current price."""

    if value is None:
        return "unresolved"
    if value < 0.0:
        return "priced_decline"
    if value < 0.05:
        return "priced_growth_0_5pct"
    if value < 0.10:
        return "priced_growth_5_10pct"
    if value < 0.20:
        return "priced_growth_10_20pct"
    return "priced_growth_20pct_plus"


def _growth_comparison(
    implied: float | None, fundamental: float | None
) -> str:
    if implied is None:
        return "unresolved"
    if fundamental is None:
        return "fundamental_growth_unavailable"
    gap = implied - fundamental
    if gap < -0.02:
        return "market_below_fundamental_growth"
    if gap > 0.02:
        return "market_above_fundamental_growth"
    return "market_and_fundamental_growth_aligned"


def _load_rows(
    builder: PointInTimeValuationBuilder,
    config: IntrinsicValueConfig,
    benchmark_path: Path,
) -> tuple[date, list[dict[str, Any]]]:
    benchmark = _load_benchmark(benchmark_path)
    builder.benchmark_bundle = benchmark
    engine = IntrinsicValueEngine(config)
    anchor = builder.latest_date()
    rows: list[dict[str, Any]] = []
    for symbol in builder.available_symbols():
        snapshot = builder.snapshot(symbol, anchor, config)
        if snapshot is None:
            continue
        estimate = engine.estimate(snapshot, SubjectiveRiskAdjustment())
        dominant = max(estimate.gate, key=estimate.gate.get)
        reverse = estimate.reverse_dcf.get(dominant, {})
        capital = snapshot.capital_cost.to_dict() if snapshot.capital_cost else {}
        implied = _finite(reverse.get("market_implied_explicit_growth"))
        fundamental = _finite(reverse.get("fundamental_explicit_growth"))
        applied = estimate.required_return_policy.get("applied_required_return", {})
        market = estimate.required_return_policy.get("market_cost_of_equity", {})
        rows.append(
            {
                "symbol": symbol,
                "evaluation_date": estimate.evaluation_date.isoformat(),
                "market_date": estimate.market_date.isoformat(),
                "current_price": estimate.current_price,
                "dominant_expert": dominant,
                "expert_gate_weight": estimate.gate.get(dominant, 0.0),
                "implied_growth_5y": implied,
                "fundamental_growth": fundamental,
                "growth_gap": (
                    implied - fundamental
                    if implied is not None and fundamental is not None
                    else None
                ),
                "growth_bucket": _growth_bucket(implied),
                "growth_comparison": _growth_comparison(implied, fundamental),
                "reverse_dcf_interpretation": reverse.get("interpretation"),
                "cash_per_share": reverse.get("cash_per_share"),
                "terminal_growth": reverse.get("terminal_growth"),
                "beta": capital.get("adjusted_beta"),
                "raw_beta": capital.get("raw_beta"),
                "beta_method": capital.get("beta_method"),
                "beta_observations": capital.get("beta_observations"),
                "risk_free_rate": capital.get("risk_free_rate"),
                "equity_risk_premium": capital.get("market_risk_premium"),
                "capm_cost_of_equity": market.get("base"),
                "applied_required_return": applied.get("base"),
                "wacc_diagnostic": capital.get("wacc"),
                "wacc_low": capital.get("wacc_low"),
                "wacc_high": capital.get("wacc_high"),
                "fair_value": estimate.fair_value,
                "fair_value_gap": estimate.fair_value_gap,
                "buy_price": estimate.buy_price,
                "confidence": estimate.confidence,
                "financial_age_days": snapshot.financial_age_days,
                "fcf_per_share": snapshot.free_cash_flow_per_share,
                "eps": snapshot.earnings_per_share,
                "roe": snapshot.roe,
                "diagnostics": ";".join(estimate.diagnostics),
            }
        )
    return anchor, rows


def _summary(rows: list[dict[str, Any]], anchor: date, config: IntrinsicValueConfig) -> dict[str, Any]:
    frame = pd.DataFrame(rows)
    def count_non_null(name: str) -> int:
        return int(frame[name].notna().sum()) if name in frame else 0

    return {
        "contract": "reverse-dcf-cross-section-1",
        "evaluation_date": anchor.isoformat(),
        "input_snapshot_count": len(rows),
        "solved_implied_growth_count": count_non_null("implied_growth_5y"),
        "beta_count": count_non_null("beta"),
        "capm_cost_count": count_non_null("capm_cost_of_equity"),
        "wacc_diagnostic_count": count_non_null("wacc_diagnostic"),
        "growth_bucket_counts": (
            frame["growth_bucket"].value_counts(dropna=False).to_dict()
            if "growth_bucket" in frame
            else {}
        ),
        "growth_comparison_counts": (
            frame["growth_comparison"].value_counts(dropna=False).to_dict()
            if "growth_comparison" in frame
            else {}
        ),
        "configuration": asdict(config),
        "acceptance": {
            "point_in_time_fundamentals": True,
            "published_at_not_after_evaluation_date": True,
            "reverse_dcf_diagnostic_only": True,
            "implied_growth_not_used_as_forecast": True,
            "discount_basis": "equity_cash_flow_with_CAPM_cost_floor",
            "wacc_is_diagnostic_when_capital_structure_is_available": True,
        },
    }


def _write_html(output: Path, report: dict[str, Any], frame: pd.DataFrame) -> None:
    display = frame.copy()
    for name in ("implied_growth_5y", "fundamental_growth", "growth_gap", "beta", "capm_cost_of_equity", "wacc_diagnostic", "fair_value_gap"):
        if name in display:
            display[name] = pd.to_numeric(display[name], errors="coerce").map(
                lambda value: "" if pd.isna(value) else f"{value:.2%}"
                if "growth" in name or name in {"capm_cost_of_equity", "wacc_diagnostic", "fair_value_gap"}
                else f"{value:.3f}"
            )
    columns = [
        "symbol", "current_price", "implied_growth_5y", "growth_bucket",
        "growth_comparison", "fundamental_growth", "growth_gap", "beta",
        "capm_cost_of_equity", "wacc_diagnostic", "dominant_expert",
        "fair_value_gap",
    ]
    columns = [name for name in columns if name in display]
    table = display[columns].sort_values(
        ["growth_bucket", "implied_growth_5y"], na_position="last"
    ).to_html(index=False, escape=True)
    summary = html.escape(json.dumps(report, ensure_ascii=False, indent=2))
    content = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>批量反向DCF</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:1500px;margin:24px auto;padding:0 16px;color:#172b4d}}
table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #d0d5dd;padding:5px;text-align:right}}
th{{background:#f2f4f7}}th:first-child,td:first-child{{text-align:left}}
pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}</style>
<h1>600+标的批量反向 DCF</h1>
<p>估值日：{html.escape(report['evaluation_date'])}。Beta 使用历史价格与基准回归，CAPM 只用于股权成本诊断和必要回报下限；反向 DCF 的隐含增长率不回灌估值。</p>
<h2>汇总</h2><pre>{summary}</pre><h2>逐标的分类</h2>{table}</html>"""
    (output / "report.html").write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--terminal-growth", type=float, default=0.02)
    parser.add_argument("--required-return-low", type=float, default=0.07)
    parser.add_argument("--required-return", type=float, default=0.075)
    parser.add_argument("--required-return-high", type=float, default=0.08)
    args = parser.parse_args()
    config = IntrinsicValueConfig(
        terminal_growth=args.terminal_growth,
        investor_required_return_low=args.required_return_low,
        investor_required_return=args.required_return,
        investor_required_return_high=args.required_return_high,
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    builder = PointInTimeValuationBuilder(args.data_root, market=args.market)
    anchor, rows = _load_rows(builder, config, Path(args.benchmark_prices))
    report = _summary(rows, anchor, config)
    report["data_root"] = str(Path(args.data_root).resolve())
    report["benchmark"] = str(Path(args.benchmark_prices).resolve())
    frame = pd.DataFrame(rows)
    frame.to_csv(output / "reverse_dcf_all.csv", index=False, encoding="utf-8")
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not frame.empty and frame["implied_growth_5y"].notna().any():
        fig, ax = plt.subplots(figsize=(10, 5))
        values = frame["implied_growth_5y"].dropna().astype(float) * 100
        ax.hist(values, bins=30, color="#2563eb", alpha=0.85)
        ax.axvline(0, color="#444", linewidth=1)
        ax.set_xlabel("现价隐含五年显式增长率 (%)")
        ax.set_ylabel("标的数")
        ax.set_title("批量反向 DCF 隐含增长率分布")
        fig.tight_layout()
        fig.savefig(output / "implied_growth_distribution.png", dpi=160)
        plt.close(fig)
    _write_html(output, report, frame)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
