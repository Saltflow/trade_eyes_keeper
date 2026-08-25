#!/usr/bin/env python3
"""Train and evaluate the causal fundamental-pricing MOE embedding."""

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

from src.fundamental_embedding import (
    MoEConfig,
    QuarterlyPricingDatasetBuilder,
    WalkForwardMoEEvaluator,
)


def _dataset_hash(dataset) -> str:
    digest = hashlib.sha256()
    digest.update("|".join(dataset.symbols).encode("utf-8"))
    digest.update("|".join(item.isoformat() for item in dataset.feature_dates).encode())
    digest.update(np.nan_to_num(dataset.values, nan=0.0).tobytes())
    digest.update(dataset.availability_mask.tobytes())
    digest.update(dataset.excess_returns.tobytes())
    return digest.hexdigest()


def _load_current_memberships(
    manifest_path: str | None,
) -> dict[str, tuple[str, ...]]:
    if not manifest_path:
        return {}
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if payload.get("contract") != "index-reference-universe-1":
        raise ValueError("unsupported reference-universe manifest contract")
    return {
        str(item["code"]): tuple(str(value) for value in item["memberships"])
        for item in payload.get("companies", [])
    }


def _write_gate_chart(report: dict, path: Path) -> None:
    rows = {}
    for item in report["predictions"]:
        rows[item["feature_date"]] = item["gate_weights"]
    if not rows:
        return
    dates = sorted(rows)
    names = list(next(iter(rows.values())))
    figure, axis = plt.subplots(figsize=(10, 4.8))
    for name in names:
        axis.plot(
            pd.to_datetime(dates),
            [rows[item][name] for item in dates],
            marker="o",
            linewidth=1.5,
            label=name,
        )
    axis.set_ylim(0.0, 1.0)
    axis.set_ylabel("Causal gate weight")
    axis.set_title("Market pricing style mixture (walk-forward only)")
    axis.grid(alpha=0.25)
    axis.legend(ncol=2)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _write_html(report: dict, path: Path, chart_name: str) -> None:
    metrics = report["metrics"]
    stability = report["stability"]
    diagnostics = report["expert_diagnostics"]
    coverage_rows = "".join(
        "<tr>"
        f"<td>{html.escape(name)}</td>"
        f"<td>{item['filled']}/{item['total']}</td>"
        f"<td>{item['fill_rate']:.1%}</td>"
        "</tr>"
        for name, item in report["feature_coverage"].items()
    )
    gate_rows = "".join(
        "<tr>"
        f"<td>{html.escape(name)}</td>"
        f"<td>{diagnostics.get('mean_gate_weights', {}).get(name, 0):.1%}</td>"
        f"<td>{diagnostics.get('latest_gate_weights', {}).get(name, 0):.1%}</td>"
        "</tr>"
        for name in diagnostics.get("expert_names", [])
    )
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>基本面MOE表征</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;max-width:1100px;margin:30px auto;color:#222}}
table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}th,td{{border:1px solid #ddd;padding:7px;text-align:right}}th:first-child,td:first-child{{text-align:left}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#f6f8fa;border-radius:8px;padding:14px}}img{{max-width:100%}}code{{background:#eee;padding:2px 4px}}
</style></head><body>
<h1>因果基本面定价 MOE 表征</h1>
<p>所有训练标签必须在预测季度前完整实现；当前小样本只验收框架和稳定性，不据此激活投资策略。</p>
<div class="cards">
<div class="card">测试季度<br><b>{report['dataset']['test_quarter_count']}</b></div>
<div class="card">测试标的<br><b>{report['dataset']['test_symbol_count']}</b></div>
<div class="card">平均Rank IC<br><b>{metrics.get('mean_quarterly_rank_ic')}</b></div>
<div class="card">稳定余弦<br><b>{stability.get('stable_median_cosine')}</b></div>
</div>
<h2>市场定价侧重</h2><img src="{html.escape(chart_name)}" alt="gate weights">
<table><tr><th>专家</th><th>历史平均权重</th><th>最新权重</th></tr>{gate_rows}</table>
<h2>评估</h2><pre>{html.escape(json.dumps({
    'metrics': metrics,
    'baselines': report['baselines'],
    'stability': stability,
    'acceptance': report['acceptance'],
}, ensure_ascii=False, indent=2))}</pre>
<h2>特征覆盖率</h2><table><tr><th>特征</th><th>填充</th><th>填充率</th></tr>{coverage_rows}</table>
</body></html>"""
    path.write_text(body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build quarterly point-in-time fundamentals and walk-forward evaluate "
            "a causal value/cash/quality/growth mixture of experts"
        )
    )
    parser.add_argument("--data-root", default="data/point_in_time")
    parser.add_argument("--output-dir", default="data/analysis/fundamental_embedding")
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument(
        "--universe-manifest",
        help=(
            "optional current index-membership manifest used only for "
            "510300/510500/510880 cohort diagnostics"
        ),
    )
    parser.add_argument("--forward-trading-days", type=int, default=63)
    parser.add_argument("--minimum-train-rows", type=int, default=48)
    parser.add_argument("--minimum-train-dates", type=int, default=8)
    parser.add_argument("--embedding-smoothing-alpha", type=float, default=0.35)
    args = parser.parse_args()
    builder = QuarterlyPricingDatasetBuilder(
        args.data_root,
        forward_trading_days=args.forward_trading_days,
        market=args.market,
    )
    dataset = builder.build(symbols=args.symbols)
    current_snapshot = builder.build_latest(symbols=args.symbols)
    current_memberships = _load_current_memberships(
        args.universe_manifest
    )
    config = MoEConfig(
        minimum_train_rows=args.minimum_train_rows,
        minimum_train_dates=args.minimum_train_dates,
        embedding_smoothing_alpha=args.embedding_smoothing_alpha,
    )
    evaluation = WalkForwardMoEEvaluator(config).run(
        dataset,
        inference_snapshot=current_snapshot,
        current_memberships=current_memberships,
    )
    report = evaluation.to_dict()
    report["generated_at"] = datetime.now().astimezone().isoformat()
    report["dataset_hash"] = _dataset_hash(dataset)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    predictions = pd.DataFrame(report["predictions"])
    predictions.to_csv(output_dir / "predictions.csv", index=False, encoding="utf-8")
    latest_rows = []
    for item in report["latest_embeddings"]:
        row = {
            "feature_date": item["feature_date"],
            "symbol": item["symbol"],
        }
        row.update(dict(zip(item["embedding_names"], item["embedding"])))
        row.update({f"gate:{name}": value for name, value in item["gate_weights"].items()})
        latest_rows.append(row)
    pd.DataFrame(latest_rows).to_csv(
        output_dir / "latest_embeddings.csv", index=False, encoding="utf-8"
    )
    chart_path = output_dir / "gate_weights.png"
    _write_gate_chart(report, chart_path)
    _write_html(report, output_dir / "report.html", chart_path.name)
    print(json.dumps({
        "report": str(report_path.resolve()),
        "dataset": report["dataset"],
        "metrics": report["metrics"],
        "stability": report["stability"],
        "acceptance": report["acceptance"],
        "latest_gate_weights": report["expert_diagnostics"].get(
            "latest_gate_weights"
        ),
    }, ensure_ascii=False, indent=2))
    return 0 if report["acceptance"]["framework_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
