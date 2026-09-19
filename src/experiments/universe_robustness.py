"""Publication-free 84-month universe robustness diagnostics.

Every variant reuses the configured market's strategy, Solver, budget and
22/16/2/4 walk-forward contract.  It deliberately has no dependency on
optimizer artifact activation: this is evidence for a human decision only.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterable

from .strategy_benchmark import (
    BENCHMARK_GROUPS,
    prepare_benchmark_data,
    run_market_benchmark,
    summarize_prepared_data,
)


@dataclass(frozen=True)
class UniverseVariant:
    """A deterministic input variant; no monitoring configuration is mutated."""

    variant_id: str
    kind: str
    codes: tuple[str, ...]
    input_order: tuple[str, ...]
    excluded_codes: tuple[str, ...]


def _hashed_order(codes: Iterable[str]) -> list[str]:
    return sorted(
        {str(code) for code in codes},
        key=lambda code: (hashlib.sha256(code.encode()).hexdigest(), code),
    )


def build_universe_variants(codes: Iterable[str]) -> list[UniverseVariant]:
    """Build baseline, leave-one-out, proportional and order diagnostics."""

    baseline = tuple(sorted({str(code) for code in codes if str(code)}))
    if not baseline:
        return []
    variants = [
        UniverseVariant(
            "baseline",
            "baseline",
            baseline,
            baseline,
            (),
        )
    ]
    for code in baseline:
        kept = tuple(item for item in baseline if item != code)
        if kept:
            variants.append(
                UniverseVariant(
                    f"leave_one_out__{code}",
                    "leave_one_out",
                    kept,
                    kept,
                    (code,),
                )
            )

    ranked = _hashed_order(baseline)
    for fraction in (0.75, 0.50):
        count = max(2, int(len(baseline) * fraction))
        if count >= len(baseline):
            continue
        kept = tuple(sorted(ranked[:count]))
        variants.append(
            UniverseVariant(
                f"retain_{int(fraction * 100)}pct",
                "proportional_reduction",
                kept,
                kept,
                tuple(code for code in baseline if code not in kept),
            )
        )

    # The engine owns a lexical tie-break.  These two variants demonstrate
    # that cross-sectional input ordering cannot change a selected result.
    variants.extend(
        [
            UniverseVariant(
                "cross_sectional_order_ascending",
                "cross_sectional_order",
                baseline,
                baseline,
                (),
            ),
            UniverseVariant(
                "cross_sectional_order_descending",
                "cross_sectional_order",
                baseline,
                tuple(reversed(baseline)),
                (),
            ),
        ]
    )
    return variants


def _metric(summary: dict[str, Any], name: str) -> float | None:
    value = summary.get(name)
    return float(value) if value is not None else None


def _variant_metrics(result: dict[str, Any]) -> dict[str, float | int | None]:
    holdout = dict(result.get("holdout_summary", {}) or {})
    return {
        "return_pct": _metric(holdout, "mean_return_pct"),
        "excess_return_pct": _metric(holdout, "mean_excess_pct"),
        "max_drawdown_pct": _metric(holdout, "worst_drawdown_pct"),
        "sharpe_ratio": _metric(holdout, "mean_sharpe"),
        "trade_count": int(holdout.get("total_trades", 0) or 0),
        "window_count": int(holdout.get("window_count", 0) or 0),
    }


def _delta(
    value: float | int | None,
    baseline: float | int | None,
) -> float | None:
    if value is None or baseline is None:
        return None
    return round(float(value) - float(baseline), 8)


def _variant_snapshot(
    variant: UniverseVariant,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    selected = set(variant.codes)
    return {
        **prepared,
        "configured_codes": list(variant.input_order),
        "stocks_data": {
            code: frame
            for code, frame in prepared["stocks_data"].items()
            if code in selected
        },
        "market_bundles": {
            code: bundle
            for code, bundle in prepared.get("market_bundles", {}).items()
            if code in selected
        },
    }


def run_market_universe_robustness(
    *,
    config: dict,
    group: str,
    prepared_market: dict[str, Any],
    search_depth: int,
    evaluation_workers: int,
    runner: Callable[..., dict[str, Any]] = run_market_benchmark,
) -> dict[str, Any]:
    """Run the configured market strategy against deterministic pool variants."""

    from main import _optimizer_evaluation_budget, _strategy_context_enricher
    from src.search.config import get_market_optimizer_config
    from src.strategy import get_strategy

    if group not in BENCHMARK_GROUPS:
        raise ValueError(f"unknown market group: {group}")
    market_config = get_market_optimizer_config(group, config)
    strategy = get_strategy(market_config.strategy_name)
    if strategy is None:
        raise ValueError(f"unknown configured strategy: {market_config.strategy_name}")
    ready_codes = tuple(sorted(prepared_market.get("stocks_data", {})))
    base = {
        "market": group,
        "strategy_id": market_config.strategy_name,
        "solver_id": market_config.solver_id,
        "search_depth": int(search_depth),
        "market_contract": market_config.to_contract(),
        "configured_codes": list(prepared_market.get("configured_codes", [])),
        "ready_codes": list(ready_codes),
        "data_exclusions": {
            "missing_or_short_history_codes": list(
                prepared_market.get("missing_or_short_history_codes", [])
            ),
            "data_readiness_errors": list(
                prepared_market.get("data_readiness_errors", [])
            ),
        },
        "variants": [],
    }
    if len(ready_codes) < 2:
        base["status"] = "data_not_ready"
        base["reason"] = "fewer than two data-ready codes; no valid perturbation"
        return base

    solver_config = market_config.constraints.search.solver_config(
        market_config.solver_id
    )
    required_depth = _optimizer_evaluation_budget(market_config.constraints)
    if int(search_depth) != int(required_depth):
        raise ValueError(
            f"{group}: robustness budget {search_depth} must equal "
            f"configured optimizer budget {required_depth}"
        )
    baseline_metrics: dict[str, float | int | None] | None = None
    for variant in build_universe_variants(ready_codes):
        snapshot = _variant_snapshot(variant, prepared_market)
        row: dict[str, Any] = {
            "variant_id": variant.variant_id,
            "kind": variant.kind,
            "codes": list(variant.codes),
            "input_order": list(variant.input_order),
            "excluded_codes": list(variant.excluded_codes),
            "data_ready_count": len(snapshot["stocks_data"]),
            "data_exclusions": dict(base["data_exclusions"]),
        }
        if len(snapshot["stocks_data"]) != len(variant.codes):
            row.update(
                {
                    "status": "data_not_ready",
                    "reason": "variant lost a data-ready instrument",
                    "holdout": _variant_metrics({}),
                }
            )
            base["variants"].append(row)
            continue
        try:
            context_enricher = _strategy_context_enricher(
                strategy, config, group, list(variant.codes)
            )
            result = runner(
                config=config,
                strategy_name=market_config.strategy_name,
                group=group,
                search_depth=int(search_depth),
                evaluation_workers=max(1, int(evaluation_workers)),
                prepared_market=snapshot,
                context_enricher=context_enricher,
                solver_id=market_config.solver_id,
                solver_config=solver_config,
            )
            metrics = _variant_metrics(result)
            row.update(
                {
                    "status": "completed",
                    "holdout": metrics,
                    "full_window_counts": dict(result.get("full_window_counts", {})),
                    "window_roles": [
                        {
                            "global_index": item.get("global_index"),
                            "role": item.get("role"),
                            "period": item.get("period"),
                        }
                        for item in result.get("windows", [])
                    ],
                    "result": result,
                }
            )
            if variant.variant_id == "baseline":
                baseline_metrics = metrics
        except Exception as exc:  # noqa: BLE001 - diagnostic must remain complete.
            row.update(
                {
                    "status": "failed",
                    "reason": str(exc),
                    "holdout": _variant_metrics({}),
                }
            )
        base["variants"].append(row)

    for row in base["variants"]:
        metrics = row["holdout"]
        row["delta_vs_baseline"] = {
            key: _delta(metrics.get(key), (baseline_metrics or {}).get(key))
            for key in (
                "return_pct",
                "excess_return_pct",
                "max_drawdown_pct",
                "sharpe_ratio",
                "trade_count",
            )
        }
    base["status"] = (
        "completed"
        if baseline_metrics is not None
        and any(
            item["status"] == "completed"
            and item["variant_id"] != "baseline"
            for item in base["variants"]
        )
        else "incomplete"
    )
    return base


def _safe_number(value: object, suffix: str = "") -> str:
    try:
        return f"{float(value):+.2f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def render_universe_robustness_report(payload: dict[str, Any]) -> str:
    """Render one self-contained report; failed variants stay visible."""

    rows: list[str] = []
    for market in payload.get("markets", []):
        for item in market.get("variants", []):
            metrics = item.get("holdout", {})
            deltas = item.get("delta_vs_baseline", {})
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(market.get('market', '—')))}</td>"
                f"<td>{html.escape(str(item.get('variant_id', '—')))}</td>"
                f"<td>{html.escape(str(item.get('status', '—')))}</td>"
                f"<td>{html.escape(', '.join(item.get('codes', [])))}</td>"
                f"<td>{_safe_number(metrics.get('return_pct'), '%')}</td>"
                f"<td>{_safe_number(metrics.get('excess_return_pct'), '%')}</td>"
                f"<td>{_safe_number(metrics.get('max_drawdown_pct'), '%')}</td>"
                f"<td>{_safe_number(metrics.get('sharpe_ratio'))}</td>"
                f"<td>{html.escape(str(metrics.get('trade_count', '—')))}</td>"
                f"<td>{_safe_number(deltas.get('return_pct'), '%')}</td>"
                f"<td>{_safe_number(deltas.get('excess_return_pct'), '%')}</td>"
                f"<td>{html.escape(str(item.get('reason', '')))}</td>"
                "</tr>"
            )
    body = "".join(rows) or "<tr><td colspan='12'>没有可运行的市场变体。</td></tr>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>三市场标的池稳健性</title>
<style>
body{{margin:0;background:#f2f6fa;color:#21364b;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1440px;margin:auto;padding:16px 12px 28px}}section{{background:#fff;border:1px solid #dbe5ef;border-radius:10px;padding:14px;margin-bottom:12px}}
h1,h2{{margin:0 0 8px;color:#173b62}}h1{{font-size:22px}}h2{{font-size:17px}}.meta{{font-size:12px;color:#61758b}}.scroll{{overflow-x:auto}}table{{width:100%;min-width:1100px;border-collapse:collapse;font-size:12px}}th,td{{padding:8px;border-bottom:1px solid #e3ebf3;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:#f4f8fc;white-space:nowrap}}@media(max-width:640px){{main{{padding:10px 8px}}section{{padding:10px}}h1{{font-size:19px}}}}
</style></head><body><main>
<section><h1>三市场标的池稳健性 benchmark</h1><div class="meta">创建于 {html.escape(str(payload.get('created_at', '—')))} · 仅诊断，不会激活策略或修改生产指针。</div></section>
<section><h2>合同</h2><div class="meta">84 个月 · 22 个窗口（16 Ranking / 2 Purged / 4 Holdout）。所有变体复用各市场配置的策略、Solver、预算、raw 成交 / qfq 信号 / 公司行为合同；Holdout 只用于最终诊断。</div></section>
<section><h2>变体结果</h2><div class="scroll"><table><thead><tr><th>市场</th><th>变体</th><th>状态</th><th>入选标的</th><th>Holdout 收益</th><th>超额</th><th>最差回撤</th><th>Sharpe</th><th>交易</th><th>收益差</th><th>超额差</th><th>说明</th></tr></thead><tbody>{body}</tbody></table></div></section>
</main></body></html>"""


def write_universe_robustness_artifacts(
    payload: dict[str, Any],
    output_dir: Path | str,
) -> dict[str, str]:
    """Persist machine-readable and human-readable diagnostics."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "universe_robustness.json"
    html_path = root / "universe_robustness.html"
    csv_path = root / "universe_robustness.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_universe_robustness_report(payload), encoding="utf-8")
    rows = [
        {
            "market": market.get("market"),
            "variant_id": variant.get("variant_id"),
            "kind": variant.get("kind"),
            "status": variant.get("status"),
            "code_count": len(variant.get("codes", [])),
            "codes": ",".join(variant.get("codes", [])),
            **dict(variant.get("holdout", {})),
            **{
                f"delta_{key}": value
                for key, value in dict(
                    variant.get("delta_vs_baseline", {})
                ).items()
            },
            "reason": variant.get("reason", ""),
        }
        for market in payload.get("markets", [])
        for variant in market.get("variants", [])
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return {
        "json": str(json_path.resolve()),
        "html": str(html_path.resolve()),
        "csv": str(csv_path.resolve()),
    }


def run_universe_robustness(
    config: dict,
    *,
    groups: Iterable[str] = BENCHMARK_GROUPS,
    search_depth: int | None = None,
    evaluation_workers: int = 1,
    output_dir: Path | str | None = None,
    runner: Callable[..., dict[str, Any]] = run_market_benchmark,
) -> dict[str, Any]:
    """Run isolated market diagnostics and persist no optimizer activation."""

    from main import _optimizer_evaluation_budget
    from src.search.config import get_market_optimizer_config

    requested = tuple(dict.fromkeys(str(group) for group in groups))
    unknown = sorted(set(requested) - set(BENCHMARK_GROUPS))
    if unknown:
        raise ValueError(f"unknown market groups: {unknown}")
    started = monotonic()
    prepared = prepare_benchmark_data(config, requested)
    markets = []
    for group in requested:
        market_config = get_market_optimizer_config(group, config)
        required_depth = _optimizer_evaluation_budget(market_config.constraints)
        depth = required_depth if search_depth is None else int(search_depth)
        markets.append(
            run_market_universe_robustness(
                config=config,
                group=group,
                prepared_market=prepared[group],
                search_depth=depth,
                evaluation_workers=evaluation_workers,
                runner=runner,
            )
        )
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "universe_robustness_v1",
        "activation": {"attempted": False, "changed": False},
        "contract": {
            "total_months": 84,
            "window_partition": {"total": 22, "ranking": 16, "purged": 2, "holdout": 4},
            "holdout_used_for_selection": False,
            "execution": "raw-prices-qfq-signals-explicit-corporate-actions",
        },
        "data_readiness": summarize_prepared_data(prepared),
        "markets": markets,
        "elapsed_seconds": monotonic() - started,
    }
    target = output_dir or (
        Path("data/optimizer/diagnostics")
        / f"universe-robustness-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    )
    payload["artifacts"] = write_universe_robustness_artifacts(payload, target)
    return payload
