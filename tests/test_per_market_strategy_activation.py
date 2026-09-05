"""Three market optimizer configuration and isolation regressions."""

from __future__ import annotations

import main
from src.search.config import get_market_optimizer_configs


GROUPS = ("a_share", "hk", "us")


def test_optimizer_resolves_distinct_strategy_solver_and_profiles():
    configs = get_market_optimizer_configs(main.load_config())
    assert set(configs) == set(GROUPS)
    assert [configs[group].strategy_name for group in GROUPS] == [
        "technical_ensemble",
        "regime_pullback",
        "percentile",
    ]
    assert [configs[group].solver_id for group in GROUPS] == [
        "local_genetic",
        "simulated_annealing",
        "random",
    ]
    assert len({configs[group].config_hash for group in GROUPS}) == 3
    assert configs["a_share"].constraints is not configs["hk"].constraints
    assert configs["hk"].constraints is not configs["us"].constraints
    assert configs["a_share"].constraints.benchmark_codes != configs["hk"].constraints.benchmark_codes


def test_global_strategy_and_solver_fallbacks_are_rejected():
    config = main.load_config()
    config["optimizer"] = {
        "strategy_by_group": {group: "percentile" for group in GROUPS}
    }
    try:
        get_market_optimizer_configs(config)
    except ValueError as exc:
        assert "fallback" in str(exc)
    else:
        raise AssertionError("legacy strategy_by_group must be rejected")


def test_optimizer_runs_one_independent_job_per_market(monkeypatch):
    config = main.load_config()
    calls = []

    def fake_market_run(config, group, market_config, **_kwargs):
        calls.append(
            (
                market_config.strategy_name,
                group,
                market_config.solver_id,
                market_config.constraints.market_group,
            )
        )
        return {group: 1}

    monkeypatch.setattr(main, "_run_optimization_group", fake_market_run)

    completed = main.run_optimization(config, target_groups=GROUPS)

    assert completed == {"a_share": 1, "hk": 1, "us": 1}
    assert calls == [
        ("technical_ensemble", "a_share", "local_genetic", "a_share"),
        ("regime_pullback", "hk", "simulated_annealing", "hk"),
        ("percentile", "us", "random", "us"),
    ]


def test_cli_limits_activation_to_one_explicit_market():
    parser = main._build_argument_parser()
    args = parser.parse_args(["--activate-run", "candidate"])
    assert args.market_group is None
