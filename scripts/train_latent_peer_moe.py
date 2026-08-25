#!/usr/bin/env python3
"""Run the company-conditional latent peer MOE experiment."""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.dataset import (  # noqa: E402
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.latent_peer_moe import (  # noqa: E402
    LatentPeerMoEConfig,
    LatentPeerWalkForwardEvaluator,
)


def _reference_comparison(report: dict, reference_path: str | None) -> dict:
    if not reference_path:
        return {}
    reference = json.loads(
        Path(reference_path).read_text(encoding="utf-8")
    )
    scores = {
        (row["feature_date"], row["symbol"]): row["scores"]
        for row in reference["predictions"]
    }
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in report["predictions"]:
        key = (row["feature_date"], row["symbol"])
        if key not in scores:
            continue
        by_date[row["feature_date"]].append({
            "actual": row["actual_excess_return"],
            "latent": row["latent_moe"],
            **scores[key],
        })
    result = {}
    model_ids = (
        "kalman_factor_price",
        "ewma_factor_price",
        "single_return_ridge",
        "legacy_recent_mse_gate",
    )
    for model_id in model_ids:
        latent_ic = []
        baseline_ic = []
        deltas = []
        for rows in by_date.values():
            actual = [row["actual"] for row in rows]
            first = pd.Series(
                [row["latent"] for row in rows]
            ).corr(pd.Series(actual), method="spearman")
            second = pd.Series(
                [row[model_id] for row in rows]
            ).corr(pd.Series(actual), method="spearman")
            if np.isfinite(first) and np.isfinite(second):
                latent_ic.append(float(first))
                baseline_ic.append(float(second))
                deltas.append(float(first - second))
        result[model_id] = {
            "paired_quarters": len(deltas),
            "latent_mean_rank_ic": (
                float(np.mean(latent_ic)) if latent_ic else None
            ),
            "baseline_mean_rank_ic": (
                float(np.mean(baseline_ic)) if baseline_ic else None
            ),
            "mean_delta_rank_ic": (
                float(np.mean(deltas)) if deltas else None
            ),
            "latent_win_rate": (
                float(np.mean(np.asarray(deltas) > 0.0))
                if deltas
                else None
            ),
        }
    return result


def _write_chart(report: dict, path: Path) -> None:
    by_date: dict[str, list[list[float]]] = defaultdict(list)
    for row in report["predictions"]:
        by_date[row["feature_date"]].append(row["gate"])
    dates = sorted(by_date)
    mean_gate = np.asarray([
        np.mean(by_date[feature_date], axis=0) for feature_date in dates
    ])
    figure, axis = plt.subplots(figsize=(10.5, 4.8))
    for expert in range(mean_gate.shape[1]):
        axis.plot(
            pd.to_datetime(dates),
            mean_gate[:, expert],
            marker="o",
            label=f"latent expert {expert}",
        )
    axis.set_ylim(0.0, 1.0)
    axis.set_ylabel("mean company gate weight")
    axis.set_title("Company-conditional latent expert routing")
    axis.grid(alpha=0.25)
    axis.legend(ncol=2)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _write_html(report: dict, path: Path, chart_name: str) -> None:
    comparisons = "".join(
        "<tr>"
        f"<td>{html.escape(name)}</td>"
        f"<td>{item['baseline_mean_rank_ic']:.4f}</td>"
        f"<td>{item['mean_delta_rank_ic']:+.4f}</td>"
        f"<td>{item['latent_win_rate']:.1%}</td>"
        "</tr>"
        for name, item in report["external_comparison"].items()
    )
    peers = "".join(
        "<tr>"
        f"<td>{row['symbol']}</td>"
        f"<td>{html.escape(', '.join(item['symbol'] for item in row['peers']))}</td>"
        f"<td>{html.escape(str([round(value, 3) for value in row['gate']]))}</td>"
        "</tr>"
        for row in report["latest_latent_peers"][:30]
    )
    summary = {
        "metrics": report["metrics"],
        "valuation_metrics": report["valuation_metrics"],
        "valuation_baselines": report["valuation_baselines"],
        "gate_diagnostics": report["gate_diagnostics"],
        "acceptance": report["acceptance"],
    }
    content = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>潜在同行双头MOE实验</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;max-width:1120px;
margin:28px auto;color:#202124}} table{{border-collapse:collapse;width:100%;
margin:12px 0 24px}} th,td{{border:1px solid #ddd;padding:7px}}
img{{max-width:100%}} pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}
</style></head><body>
<h1>潜在同行双头 MOE 实验</h1>
<p>不使用同行标签或股票代码。公司条件 Gate 与估值解释头、未来收益排序头
联合学习；最近邻由 Gate 分布事后产生。</p>
<img src="{html.escape(chart_name)}" alt="latent expert gates">
<h2>与冻结基线的同季度比较</h2>
<table><tr><th>基线</th><th>基线 Rank IC</th><th>MOE 增量</th>
<th>MOE 胜率</th></tr>{comparisons}</table>
<h2>当前软同行示例</h2>
<table><tr><th>代码</th><th>潜在同行</th><th>Gate</th></tr>{peers}</table>
<h2>完整诊断摘要</h2>
<pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>
</body></html>"""
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reference-report")
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--expert-count", type=int, default=4)
    parser.add_argument("--restarts", type=int, default=4)
    parser.add_argument("--em-iterations", type=int, default=35)
    args = parser.parse_args()

    builder = QuarterlyPricingDatasetBuilder(
        args.data_root, forward_trading_days=63, market=args.market
    )
    dataset = builder.build()
    snapshot = builder.build_latest()
    config = LatentPeerMoEConfig(
        expert_count=args.expert_count,
        restarts=args.restarts,
        em_iterations=args.em_iterations,
    )
    report = LatentPeerWalkForwardEvaluator(config).run(
        dataset, snapshot
    )
    report["generated_at"] = datetime.now().astimezone().isoformat()
    report["external_comparison"] = _reference_comparison(
        report, args.reference_report
    )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(report["predictions"]).to_csv(
        output / "predictions.csv", index=False, encoding="utf-8"
    )
    peer_rows = []
    for row in report["latest_latent_peers"]:
        peer_rows.append({
            "symbol": row["symbol"],
            **{
                f"gate_{index}": value
                for index, value in enumerate(row["gate"])
            },
            "peers": ",".join(item["symbol"] for item in row["peers"]),
        })
    pd.DataFrame(peer_rows).to_csv(
        output / "latest_latent_peers.csv",
        index=False,
        encoding="utf-8",
    )
    chart = output / "latent_gate_history.png"
    _write_chart(report, chart)
    _write_html(report, output / "report.html", chart.name)
    print(json.dumps({
        "report": str((output / "report.json").resolve()),
        "metrics": report["metrics"],
        "valuation_metrics": report["valuation_metrics"],
        "valuation_baselines": report["valuation_baselines"],
        "gate_diagnostics": report["gate_diagnostics"],
        "external_comparison": report["external_comparison"],
        "acceptance": report["acceptance"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["acceptance"]["framework_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
