import json

import main

from src.experiments.universe_robustness import (
    build_universe_variants,
    render_universe_robustness_report,
    run_market_universe_robustness,
    write_universe_robustness_artifacts,
)
from src.search.config import get_market_optimizer_config


def test_build_variants_is_deterministic_and_keeps_order_diagnostics():
    variants = build_universe_variants(["CCC", "AAA", "BBB"])

    assert variants[0].variant_id == "baseline"
    assert variants[0].codes == ("AAA", "BBB", "CCC")
    assert {item.variant_id for item in variants} >= {
        "leave_one_out__AAA",
        "leave_one_out__BBB",
        "leave_one_out__CCC",
        "retain_75pct",
        "cross_sectional_order_ascending",
        "cross_sectional_order_descending",
    }
    descending = next(
        item
        for item in variants
        if item.variant_id == "cross_sectional_order_descending"
    )
    assert descending.codes == ("AAA", "BBB", "CCC")
    assert descending.input_order == ("CCC", "BBB", "AAA")


def test_cli_exposes_non_activating_universe_robustness_mode():
    args = main._build_argument_parser().parse_args(
        ["--universe-robustness", "--group", "hk"]
    )

    assert args.universe_robustness
    assert args.market_group == "hk"


def test_market_robustness_reuses_market_budget_and_never_activates(monkeypatch):
    config = main.load_config()
    market_config = get_market_optimizer_config("a_share", config)
    required_budget = main._optimizer_evaluation_budget(market_config.constraints)
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        count = len(kwargs["prepared_market"]["stocks_data"])
        return {
            "holdout_summary": {
                "mean_return_pct": float(count),
                "mean_excess_pct": float(count) / 2,
                "worst_drawdown_pct": -float(count),
                "mean_sharpe": 0.5,
                "total_trades": count * 2,
                "window_count": 4,
            },
            "full_window_counts": {
                "total": 22,
                "ranking": 16,
                "purged": 2,
                "holdout": 4,
            },
            "windows": [
                {"global_index": 19, "role": "holdout", "period": {}},
                {"global_index": 20, "role": "holdout", "period": {}},
                {"global_index": 21, "role": "holdout", "period": {}},
                {"global_index": 22, "role": "holdout", "period": {}},
            ],
        }

    prepared = {
        "configured_codes": ["AAA", "BBB", "CCC"],
        "stocks_data": {"AAA": object(), "BBB": object(), "CCC": object()},
        "market_bundles": {},
        "benchmark_bundles": {},
        "benchmarks": {},
        "missing_or_short_history_codes": ["MISSING"],
        "data_readiness_errors": ["MISSING: no strict bundle"],
    }
    result = run_market_universe_robustness(
        config=config,
        group="a_share",
        prepared_market=prepared,
        search_depth=required_budget,
        evaluation_workers=1,
        runner=fake_runner,
    )

    baseline = result["variants"][0]
    leave_one_out = next(
        item
        for item in result["variants"]
        if item["variant_id"] == "leave_one_out__AAA"
    )
    assert result["status"] == "completed"
    assert baseline["holdout"]["window_count"] == 4
    assert leave_one_out["delta_vs_baseline"]["return_pct"] == -1.0
    assert all(call["search_depth"] == required_budget for call in calls)
    assert all(call["solver_id"] == market_config.solver_id for call in calls)
    assert all(
        call["prepared_market"]["configured_codes"]
        for call in calls
    )
    assert result["data_exclusions"]["missing_or_short_history_codes"] == ["MISSING"]


def test_market_robustness_fails_closed_without_a_usable_perturbation():
    result = run_market_universe_robustness(
        config=main.load_config(),
        group="a_share",
        prepared_market={
            "configured_codes": ["AAA"],
            "stocks_data": {"AAA": object()},
            "missing_or_short_history_codes": [],
            "data_readiness_errors": [],
        },
        search_depth=1,
        evaluation_workers=1,
    )

    assert result["status"] == "data_not_ready"
    assert result["variants"] == []


def test_robustness_artifacts_escape_codes_and_preserve_non_activation(tmp_path):
    payload = {
        "created_at": "2026-09-19T00:00:00Z",
        "activation": {"attempted": False, "changed": False},
        "markets": [
            {
                "market": "hk",
                "variants": [
                    {
                        "variant_id": "baseline",
                        "kind": "baseline",
                        "status": "completed",
                        "codes": ["<unsafe>"],
                        "holdout": {
                            "return_pct": 1.0,
                            "excess_return_pct": 0.5,
                            "max_drawdown_pct": -2.0,
                            "sharpe_ratio": 0.8,
                            "trade_count": 4,
                        },
                        "delta_vs_baseline": {
                            "return_pct": 0.0,
                            "excess_return_pct": 0.0,
                        },
                    }
                ],
            }
        ],
    }
    html = render_universe_robustness_report(payload)
    paths = write_universe_robustness_artifacts(payload, tmp_path)

    assert "&lt;unsafe&gt;" in html
    assert "<unsafe>" not in html
    assert "不会激活策略" in html
    saved = json.loads((tmp_path / "universe_robustness.json").read_text("utf-8"))
    assert saved["activation"] == {"attempted": False, "changed": False}
    assert all(path for path in paths.values())
