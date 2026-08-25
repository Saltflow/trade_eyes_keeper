#!/usr/bin/env python3
"""Evaluate stable company exposures and signed market factor pricing."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fundamental_embedding import (  # noqa: E402
    FACTOR_NAMES,
    QuarterlyPricingDatasetBuilder,
    SplitPricingConfig,
    SplitPricingEvaluator,
)


def _dataset_hash(dataset) -> str:
    digest = hashlib.sha256()
    digest.update("|".join(dataset.symbols).encode("utf-8"))
    digest.update(
        "|".join(item.isoformat() for item in dataset.feature_dates).encode()
    )
    digest.update(np.nan_to_num(dataset.values, nan=0.0).tobytes())
    digest.update(dataset.availability_mask.tobytes())
    digest.update(dataset.excess_returns.tobytes())
    return digest.hexdigest()


def _write_factor_chart(report: dict, path: Path) -> None:
    model_id = report["candidate_model_id"]
    states = [
        row
        for row in report["factor_price_states"]
        if row["model_id"] == model_id
    ]
    if not states:
        return
    figure, axis = plt.subplots(figsize=(10.5, 5.0))
    dates = pd.to_datetime([row["as_of"] for row in states])
    for factor_index, factor_name in enumerate(FACTOR_NAMES):
        axis.plot(
            dates,
            [row["factor_prices"][factor_index] for row in states],
            marker="o",
            linewidth=1.4,
            label=factor_name,
        )
    axis.axhline(0.0, color="#555", linewidth=0.8)
    axis.set_title("Signed market prices of stable company factors")
    axis.set_ylabel("forecast factor price")
    axis.grid(alpha=0.25)
    axis.legend(ncol=2)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _format_number(value, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _write_html(report: dict, path: Path, chart_name: str) -> None:
    metrics = report["metrics"]
    paired = report["paired_comparison"]
    baseline_rows = "".join(
        "<tr>"
        f"<td>{html.escape(model_id)}</td>"
        f"<td>{_format_number(item['mean_quarterly_rank_ic'])}</td>"
        f"<td>{_format_number(item['quarterly_rank_ic_std'])}</td>"
        f"<td>{_format_number(item['positive_ic_quarter_rate'])}</td>"
        f"<td>{_format_number(item['selection_score'])}</td>"
        "</tr>"
        for model_id, item in sorted(report["baselines"].items())
    )
    strongest = paired.get("against_strongest") or {}
    summary = {
        "candidate": report["candidate_model_id"],
        "candidate_metrics": metrics,
        "strongest_baseline": paired.get("strongest_baseline"),
        "against_strongest": strongest,
        "stability": report["stability"],
        "acceptance": report["acceptance"],
    }
    candidate_ic = _format_number(metrics["mean_quarterly_rank_ic"])
    strongest_name = html.escape(str(paired.get("strongest_baseline")))
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公司画像与市场定价拆分实验</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;max-width:1120px;
margin:28px auto;color:#202124;line-height:1.5}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.card{{background:#f5f7fa;border-radius:9px;padding:14px}}
table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}
th,td{{border:1px solid #ddd;padding:7px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
img{{max-width:100%}}pre{{white-space:pre-wrap;background:#f5f7fa;padding:14px}}
</style></head><body>
<h1>公司画像与市场定价拆分实验</h1>
<p>公司画像使用固定经济含义，不用未来收益训练；市场定价状态单独学习，
评价目标为逐季度横截面排名，所有模型使用相同样本行并强制保留基线。</p>
<div class="cards">
<div class="card">测试季度<br><b>{report['dataset']['test_quarter_count']}</b></div>
<div class="card">测试公司<br><b>{report['dataset']['test_symbol_count']}</b></div>
<div class="card">候选 Rank IC<br><b>{candidate_ic}</b></div>
<div class="card">最强基线<br><b>{strongest_name}</b></div>
</div>
<h2>有符号市场因子价格</h2>
<img src="{html.escape(chart_name)}" alt="factor prices">
<h2>强制基线</h2>
<table><tr><th>模型</th><th>平均季度 Rank IC</th><th>IC 标准差</th>
<th>正 IC 季度率</th><th>稳定性惩罚后得分</th></tr>{baseline_rows}</table>
<h2>验收摘要</h2>
<pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>
</body></html>"""
    path.write_text(body, encoding="utf-8")


def _write_tables(report: dict, output_dir: Path) -> None:
    prediction_rows = []
    for row in report["predictions"]:
        flat = {
            "feature_date": row["feature_date"],
            "label_end_date": row["label_end_date"],
            "symbol": row["symbol"],
            "actual_return": row["actual_return"],
            "actual_excess_return": row["actual_excess_return"],
        }
        flat.update({
            f"score:{name}": value for name, value in row["scores"].items()
        })
        prediction_rows.append(flat)
    pd.DataFrame(prediction_rows).to_csv(
        output_dir / "ranking_predictions.csv",
        index=False,
        encoding="utf-8",
    )

    exposure_rows = []
    for row in report["latest_company_exposures"]:
        flat = {
            "feature_date": row["feature_date"],
            "symbol": row["symbol"],
            "ranking_score": row["ranking_score"],
            "market_pricing_model": row["market_pricing_model"],
        }
        for field in (
            "raw_company_exposure",
            "stable_company_exposure",
            "ranking_company_exposure",
            "availability_confidence",
        ):
            flat.update({
                f"{field}:{name}": value
                for name, value in zip(FACTOR_NAMES, row[field])
            })
        exposure_rows.append(flat)
    pd.DataFrame(exposure_rows).to_csv(
        output_dir / "company_exposures.csv",
        index=False,
        encoding="utf-8",
    )

    state_rows = []
    for state in report["factor_price_states"]:
        row = {
            "as_of": state["as_of"],
            "realized_through": state["realized_through"],
            "model_id": state["model_id"],
        }
        row.update({
            f"price:{name}": value
            for name, value in zip(FACTOR_NAMES, state["factor_prices"])
        })
        row.update({
            f"uncertainty:{name}": value
            for name, value in zip(FACTOR_NAMES, state["uncertainty"])
        })
        state_rows.append(row)
    pd.DataFrame(state_rows).to_csv(
        output_dir / "market_pricing_states.csv",
        index=False,
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Walk-forward evaluate stable company exposures, signed market "
            "factor pricing, and mandatory cross-sectional ranking baselines"
        )
    )
    parser.add_argument("--data-root", default="data/point_in_time")
    parser.add_argument(
        "--output-dir", default="data/analysis/split_fundamental_pricing"
    )
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--forward-trading-days", type=int, default=63)
    parser.add_argument("--minimum-train-rows", type=int, default=48)
    parser.add_argument("--minimum-train-dates", type=int, default=8)
    parser.add_argument("--exposure-smoothing-alpha", type=float, default=0.35)
    parser.add_argument(
        "--candidate-model-id", default="kalman_factor_price"
    )
    args = parser.parse_args()

    builder = QuarterlyPricingDatasetBuilder(
        args.data_root,
        forward_trading_days=args.forward_trading_days,
        market=args.market,
    )
    dataset = builder.build(symbols=args.symbols)
    snapshot = builder.build_latest(symbols=args.symbols)
    config = SplitPricingConfig(
        minimum_train_rows=args.minimum_train_rows,
        minimum_train_dates=args.minimum_train_dates,
        exposure_smoothing_alpha=args.exposure_smoothing_alpha,
        candidate_model_id=args.candidate_model_id,
    )
    report = SplitPricingEvaluator(config).run(
        dataset, inference_snapshot=snapshot
    ).to_dict()
    report["generated_at"] = datetime.now().astimezone().isoformat()
    report["dataset_hash"] = _dataset_hash(dataset)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "baseline_comparison.json").write_text(
        json.dumps(
            report["paired_comparison"], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    _write_tables(report, output_dir)
    chart_path = output_dir / "factor_prices.png"
    _write_factor_chart(report, chart_path)
    _write_html(report, output_dir / "report.html", chart_path.name)

    print(json.dumps({
        "report": str(report_path.resolve()),
        "dataset": report["dataset"],
        "candidate": report["candidate_model_id"],
        "candidate_metrics": report["metrics"],
        "strongest_baseline": report["paired_comparison"][
            "strongest_baseline"
        ],
        "against_strongest": report["paired_comparison"][
            "against_strongest"
        ],
        "stability": report["stability"],
        "acceptance": report["acceptance"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["acceptance"]["framework_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
