"""Auditable terminal reports, including runs with no admissible candidate."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from html import escape
from pathlib import Path

import yaml

from .artifacts import MARKET_GROUPS, OptimizerRunSummary, as_yaml_primitives


def summarize_ranking_archive(path: Path, group: str) -> dict:
    """Count actual evaluations and hard-gate rejections without loading a run."""
    counts = Counter()
    failures = Counter()
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    counts["invalid_record_count"] += 1
                    continue
                if not isinstance(record, dict) or record.get("market") != group:
                    counts["invalid_record_count"] += 1
                    continue
                gates = record.get("gate_results", [])
                if not isinstance(gates, list) or any(
                    not isinstance(gate, dict) or not gate.get("rule_id")
                    for gate in gates
                ):
                    counts["invalid_record_count"] += 1
                    continue
                counts["evaluated_count"] += 1
                if record.get("feasible") is True:
                    counts["ranking_feasible_count"] += 1
                for gate in gates:
                    if gate.get("mode") == "hard" and gate.get("passed") is False:
                        failures[str(gate["rule_id"])] += 1
    return {
        key: counts[key]
        for key in ("evaluated_count", "ranking_feasible_count", "invalid_record_count")
    } | {"hard_gate_failure_counts": dict(failures.most_common())}


def persist_run_summary(report: OptimizerRunSummary, run_dir: Path) -> None:
    """Persist a single-market receipt, never an activation manifest."""
    if (
        run_dir.name != report.run_id
        or len(report.groups) != 1
        or not set(report.groups).issubset(MARKET_GROUPS)
    ):
        raise ValueError("terminal optimizer report must belong to one matching run")
    group, summary = next(iter(report.groups.items()))
    if summary.group != group or summary.run_id != report.run_id:
        raise ValueError("terminal optimizer report market/run identity mismatch")
    payload = as_yaml_primitives({"schema_version": 1, **asdict(report)})
    serialized = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_summary.yaml").write_text(serialized, encoding="utf-8")
    # Plain escaped structured details are intentional: failure reports do not
    # invent return/Sharpe/holdout values when no candidate survived selection.
    title = f"{group} 搜参运行记录 — {summary.status}"
    content = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>body{font:16px system-ui;max-width:1100px;margin:24px auto;"
        "padding:0 16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
        "background:#f4f6f8;padding:16px}</style>"
        f"<h1>{escape(title)}</h1>"
        "<p>独立市场运行记录。搜索完成不代表通过 Gate，也不代表已激活。</p>"
        f"<pre>{escape(serialized)}</pre></html>"
    )
    (run_dir / f"{group}_run_status.html").write_text(content, encoding="utf-8")
