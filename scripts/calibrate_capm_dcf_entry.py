#!/usr/bin/env python3
"""Calibrate a CAPM equity-DCF limit price against completed history.

The script fetches only dated ChinaBond 10-year yields when explicitly asked.
It never uses a current rate as a substitute for a missing historical date.
ERP is evaluated as declared scenarios because an ex-post market return is not
an expected equity-risk premium.  The selected parameters must pass the entry
quality gate in every scenario on the held-out chronological suffix.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.market_history import PriceHistoryBundle
from src.fundamental_embedding.capital_market_data import (
    OfficialCapitalMarketDataProvider,
)
from src.fundamental_embedding.dcf_entry_calibration import (
    CapmDcfEntryCalibrator,
    CapmDcfEntryConfig,
    CapmDcfEntryParameters,
    DCF_ENTRY_CALIBRATION_CONTRACT,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)


def _load_benchmark(path: Path) -> PriceHistoryBundle:
    """Load the locally cached real 510300 history for point-in-time beta."""

    raw = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"benchmark is missing columns: {missing}")
    volume = raw["volume"] if "volume" in raw.columns else 1.0
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
    for field in ("open", "high", "low", "close"):
        frame[f"qfq_{field}"] = frame[f"raw_{field}"]
    frame["qfq_factor"] = 1.0
    frame["tradable"] = frame["volume"] > 0
    return PriceHistoryBundle(
        code=path.stem,
        prices=frame.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True),
        source=f"local_real_price_cache:{path.resolve()}",
        diagnostics=["beta benchmark cache excludes dividend adjustment"],
    ).validate()


def _parse_erp(values: str) -> tuple[float, ...]:
    result = tuple(sorted({float(item.strip()) for item in values.split(",")}))
    if not result or any(not 0.0 < item < 0.20 for item in result):
        raise ValueError("ERP scenarios must be comma-separated decimal rates")
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load_frozen_policy(
    path: Path,
) -> tuple[dict[str, CapmDcfEntryParameters], date, dict[str, object]]:
    """Load a broad-universe policy with its causal availability boundary."""

    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("contract") != DCF_ENTRY_CALIBRATION_CONTRACT:
        raise ValueError("frozen policy report has an unknown DCF contract")
    acceptance = report.get("acceptance", {})
    if not acceptance.get("candidate_eligible_for_manual_strategy_experiment"):
        raise ValueError("frozen policy report did not pass its own holdout gate")
    expected_config = CapmDcfEntryConfig()
    from dataclasses import asdict

    if _canonical_json(report.get("config")) != _canonical_json(asdict(expected_config)):
        raise ValueError(
            "frozen policy uses a different DCF economic/config contract; "
            "do not apply it silently"
        )
    raw_policy = report.get("selection", {}).get("parameters", {})
    if not isinstance(raw_policy, dict) or not raw_policy:
        raise ValueError("frozen policy report has no supported valuation routes")
    policy = {
        str(model): CapmDcfEntryParameters.from_dict(value)
        for model, value in raw_policy.items()
        if isinstance(value, dict)
    }
    available_from = report.get("dataset", {}).get("validation_start")
    if not available_from:
        raise ValueError("frozen policy report is missing validation_start")
    return (
        policy,
        date.fromisoformat(str(available_from)),
        {
            "source_report": str(path.resolve()),
            "source_contract": report["contract"],
            "source_validation_start": str(available_from),
            "source_candidate_gate_passed": True,
        },
    )


def _load_rates(path: Path) -> dict[date, float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("risk_free_rates", payload)
    if not isinstance(values, dict):
        raise TypeError("risk-free JSON must contain a date-to-rate mapping")
    result = {
        date.fromisoformat(str(key)): float(value) for key, value in values.items()
    }
    if any(not 0.0 < value < 0.20 for value in result.values()):
        raise ValueError("risk-free rates must be decimal rates between zero and 20%")
    return result


def _fetch_rates(
    dates: list[date], timeout: int
) -> tuple[dict[date, float], dict[str, Any]]:
    provider = OfficialCapitalMarketDataProvider(timeout=timeout)
    values: dict[date, float] = {}
    audit: dict[str, Any] = {}
    for requested in dates:
        print(
            json.dumps(
                {
                    "stage": "fetch_risk_free",
                    "requested_date": requested.isoformat(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        errors = []
        for offset in range(8):
            attempted = requested - timedelta(days=offset)
            try:
                rate = provider.fetch_chinabond_ten_year_yield(attempted)
            except (
                requests.RequestException,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                errors.append(f"{attempted.isoformat()}:{type(exc).__name__}")
                continue
            values[requested] = float(rate)
            audit[requested.isoformat()] = {
                "requested_date": requested.isoformat(),
                "source_date": attempted.isoformat(),
                "risk_free_rate": float(rate),
                "source": "ChinaBond 10Y government curve",
            }
            break
        if requested not in values:
            audit[requested.isoformat()] = {
                "requested_date": requested.isoformat(),
                "status": "missing",
                "attempts": errors,
            }
    return values, audit


def _metric_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def append_rows(split: str, by_erp: dict[str, Any]) -> None:
        for erp, metrics in by_erp.items():
            aggregate = {
                key: value
                for key, value in metrics.items()
                if key not in {"rows", "by_valuation_model"}
            }
            rows.append(
                {
                    "split": split,
                    "valuation_model": "aggregate",
                    "erp": float(erp),
                    **aggregate,
                }
            )
            for model, model_metrics in metrics.get(
                "by_valuation_model", {}
            ).items():
                rows.append(
                    {
                        "split": split,
                        "valuation_model": model,
                        "erp": float(erp),
                        **model_metrics,
                    }
                )

    append_rows("train", report["selection"]["training_metrics"])
    append_rows("validation", report["validation"]["metrics"])
    return rows


def _write_html(output: Path, report: dict[str, Any]) -> None:
    metrics = pd.DataFrame(_metric_rows(report))
    numeric = metrics.copy()
    for column in (
        "erp",
        "scenario_erp",
        "hit_rate",
        "post_entry_success_rate",
        "post_entry_success_wilson_lower_95",
        "mean_post_entry_above_rate",
    ):
        if column in numeric:
            numeric[column] = pd.to_numeric(numeric[column], errors="coerce").map(
                lambda value: "" if pd.isna(value) else f"{value:.2%}"
            )
    parameters = html.escape(
        json.dumps(report["selection"]["parameters"], ensure_ascii=False, indent=2)
    )
    summary = html.escape(
        json.dumps(
            {
                "economic_contract": report["economic_contract"],
                "dataset": report["dataset"],
                "validation": {
                    "passes_all_erp_scenarios": report["validation"][
                        "passes_all_erp_scenarios"
                    ],
                    "selection_never_read_validation_labels": report["validation"][
                        "selection_never_read_validation_labels"
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    table = (
        numeric.to_html(index=False, escape=True) if not numeric.empty else "<p>无</p>"
    )
    output.write_text(
        f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>CAPM 股权 DCF 买入价校准</title>
<style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:1500px;margin:24px auto;padding:0 16px;color:#172b4d}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #d0d5dd;padding:6px;text-align:right}}th{{background:#f2f4f7}}th:first-child,td:first-child{{text-align:left}}pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}
</style>
<h1>CAPM 股权 DCF 买入价：历史校准</h1>
<p>买入限价须在估值日后的 252 个有效交易日内以当日最低价触发；
触发后以买入限价为成本，接下来 252 个有效交易日的收盘价至少有 50% 高于该成本才记为成功。
未来价格仅作标签，不能进入增长、Beta、CAPM 或参数选择的训练输入。</p>
<p><strong>不以从不成交换取高胜率：</strong>每个 ERP 情景都要满足最小成交覆盖和最小成交笔数；
参数只在时间上更早的训练报告日选择，最后的报告日后缀保持留出。</p>
<p><strong>训练可行候选数：</strong>{report["selection"]["training_feasible_candidate_count"]} /
{report["selection"]["candidate_count"]}；
<strong>可用于生产：</strong>{report["acceptance"]["production_ready"]}。</p>
<h2>按估值模型选择的参数</h2><pre>{parameters}</pre>
<h2>训练与留出结果（aggregate 为合并 Gate；其余行是模型分解）</h2>{table}
<h2>合同与数据审计</h2><pre>{summary}</pre>
</html>""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument(
        "--industry-history",
        required=True,
        help="official point-in-time industry classification history JSON",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--erp-scenarios", default="0.04,0.05,0.06")
    parser.add_argument("--risk-free-rates-json")
    parser.add_argument("--fetch-risk-free", action="store_true")
    parser.add_argument("--risk-free-timeout", type=int, default=8)
    parser.add_argument(
        "--risk-free-start",
        default="2021-01-01",
        help="earliest common quarterly anchor to calibrate (YYYY-MM-DD)",
    )
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument(
        "--frozen-policy-report",
        help=(
            "passed broad-universe report; applies its frozen policy only from "
            "that report's holdout start and never retrains on --symbols"
        ),
    )
    args = parser.parse_args()
    if bool(args.risk_free_rates_json) == bool(args.fetch_risk_free):
        raise ValueError(
            "provide exactly one of --risk-free-rates-json or --fetch-risk-free"
        )

    benchmark = _load_benchmark(Path(args.benchmark_prices))
    industry_history = IndustryClassificationHistoryStore(args.industry_history)
    provisional = CapmDcfEntryCalibrator(
        args.data_root,
        benchmark,
        {},
        config=CapmDcfEntryConfig(),
        industry_history=industry_history,
    )
    risk_free_start = date.fromisoformat(args.risk_free_start)
    required_dates = [
        item
        for item in provisional.required_risk_free_dates(args.symbols)
        if item >= risk_free_start
    ]
    rate_audit: dict[str, Any]
    if args.fetch_risk_free:
        rates, rate_audit = _fetch_rates(required_dates, args.risk_free_timeout)
    else:
        rates = _load_rates(Path(args.risk_free_rates_json))
        rate_audit = {
            key.isoformat(): {
                "requested_date": key.isoformat(),
                "source_date": key.isoformat(),
                "risk_free_rate": value,
                "source": str(Path(args.risk_free_rates_json).resolve()),
            }
            for key, value in rates.items()
        }
    missing = [item.isoformat() for item in required_dates if item not in rates]
    if missing:
        raise ValueError(
            "historical risk-free rates are missing for evaluation dates: "
            + ", ".join(missing[:20])
        )

    calibrator = CapmDcfEntryCalibrator(
        args.data_root,
        benchmark,
        rates,
        config=CapmDcfEntryConfig(),
        industry_history=industry_history,
    )
    scenarios = _parse_erp(args.erp_scenarios)
    if args.frozen_policy_report:
        policy, available_from, provenance = _load_frozen_policy(
            Path(args.frozen_policy_report)
        )
        report = calibrator.run_frozen_policy(
            policy=policy,
            policy_available_from=available_from,
            erp_scenarios=scenarios,
            symbols=args.symbols,
            policy_provenance=provenance,
        )
    else:
        report = calibrator.run(erp_scenarios=scenarios, symbols=args.symbols)
    report["risk_free_rate_audit"] = rate_audit
    report["benchmark"] = {
        "path": str(Path(args.benchmark_prices).resolve()),
        "source": benchmark.source,
        "diagnostics": benchmark.diagnostics,
    }
    report["industry_history"] = {
        "path": str(Path(args.industry_history).resolve()),
        "contract": "industry-classification-history-1",
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "risk_free_rates.json").write_text(
        json.dumps(
            {
                "contract": "official-chinabond-10y-history-1",
                "risk_free_rates": {
                    item.isoformat(): value for item, value in sorted(rates.items())
                },
                "audit": rate_audit,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    rows = []
    for erp, metrics in report["validation"]["metrics"].items():
        for row in metrics.get("rows", []):
            flattened = dict(row)
            flattened["scenario_erp"] = float(erp)
            flattened["growth_components"] = json.dumps(
                flattened.get("growth_components", {}), ensure_ascii=False
            )
            rows.append(flattened)
    pd.DataFrame(rows).to_csv(
        output / "validation_rows.csv", index=False, encoding="utf-8"
    )
    _write_html(output / "report.html", report)
    console_summary = {
        "passes_all_erp_scenarios": report["validation"][
            "passes_all_erp_scenarios"
        ],
        "metrics": {
            erp: {
                key: metrics.get(key)
                for key in (
                    "eligible_count",
                    "hit_count",
                    "hit_rate",
                    "success_count",
                    "post_entry_success_rate",
                    "post_entry_success_wilson_lower_95",
                    "by_valuation_model",
                )
            }
            for erp, metrics in report["validation"]["metrics"].items()
        },
    }
    print(json.dumps(console_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
