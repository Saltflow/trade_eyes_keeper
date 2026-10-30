"""Strict market-scoped optimizer configuration contract tests."""

from __future__ import annotations

from copy import deepcopy

import main
import pytest

from src.search.config import (
    get_market_optimizer_config,
    get_market_optimizer_configs,
    load_evaluation_horizon_policy,
    load_research_horizon,
)


def _config():
    return main.load_config()


def test_all_three_markets_are_required():
    config = _config()
    del config["optimizer"]["markets"]["hk"]
    with pytest.raises(ValueError, match="missing groups"):
        get_market_optimizer_configs(config)


@pytest.mark.parametrize("legacy_field", ["engine", "strategy_by_group"])
def test_legacy_global_strategy_fields_are_rejected(legacy_field):
    config = _config()
    config["optimizer"][legacy_field] = "percentile"
    with pytest.raises(ValueError, match="fallback"):
        get_market_optimizer_configs(config)


@pytest.mark.parametrize(
    "field",
    [
        "strategy",
        "solver_id",
        "gate_profile",
        "walk_forward_profile",
        "execution_profile",
        "benchmark_profile",
    ],
)
def test_each_market_contract_field_is_required(field):
    config = _config()
    config["optimizer"]["markets"]["us"].pop(field)
    with pytest.raises(ValueError, match="missing required"):
        get_market_optimizer_config("us", application_config=config)


def test_unknown_solver_and_missing_profile_fail_closed():
    config = _config()
    config["optimizer"]["markets"]["hk"]["solver_id"] = "does_not_exist"
    with pytest.raises(ValueError, match="unknown solver"):
        get_market_optimizer_config("hk", application_config=config)

    config = _config()
    config["optimizer"]["markets"]["hk"]["walk_forward_profile"] = "missing"
    with pytest.raises(ValueError, match="profile"):
        get_market_optimizer_config("hk", application_config=config)


def test_global_solver_and_gate_keys_are_not_accepted_as_market_defaults(tmp_path):
    config = _config()
    constraints_path = "config/optimizer_constraints.yaml"
    import yaml

    raw = yaml.safe_load(open(constraints_path, encoding="utf-8"))
    raw["search"]["solver_id"] = "random"
    solver_path = tmp_path / "solver.yaml"
    solver_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="global search"):
        get_market_optimizer_config(
            "a_share", application_config=config, constraints_path=solver_path
        )

    raw = yaml.safe_load(open(constraints_path, encoding="utf-8"))
    raw["search"]["gate_profile"] = "standard"
    temp = deepcopy(raw)
    gate_path = tmp_path / "gate.yaml"
    gate_path.write_text(yaml.safe_dump(temp), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="global search"):
            get_market_optimizer_config(
                "a_share", application_config=config, constraints_path=gate_path
            )
    finally:
        gate_path.unlink(missing_ok=True)


def test_market_solver_overrides_are_independent():
    config = _config()
    config["optimizer"]["markets"]["hk"]["solver_config"] = {"budget": 77}
    resolved = get_market_optimizer_configs(config)
    assert resolved["hk"].search.solver_config()["budget"] == 77
    assert resolved["a_share"].search.solver_config()["budget"] != 77
    resolved["a_share"].constraints.benchmark_codes.append("MUTATION")
    assert "MUTATION" not in resolved["hk"].constraints.benchmark_codes


def test_active_market_contracts_have_a_continuous_two_year_holdout():
    policy = load_evaluation_horizon_policy()
    assert policy.default_backtest_months >= policy.minimum_evaluation_months
    assert (
        policy.default_backtest_trading_days
        >= policy.minimum_evaluation_trading_days
    )
    for resolved in get_market_optimizer_configs(_config()).values():
        horizon = resolved.constraints.walk_forward
        assert horizon.test_months >= policy.minimum_evaluation_months
        assert horizon.held_out_window_count == 1
        assert (
            horizon.holdout_calendar_span_months
            >= policy.minimum_evaluation_months
        )
        assert horizon.independent_ranking_window_count >= 1


def test_short_market_or_research_horizon_fails_closed(tmp_path):
    import yaml

    raw = yaml.safe_load(open("config/optimizer_constraints.yaml", encoding="utf-8"))
    raw["walk_forward_profiles"]["a_share_84m"]["test_months"] = 23
    market_path = tmp_path / "short_market.yaml"
    market_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Walk-Forward profile"):
        get_market_optimizer_config(
            "a_share", application_config=_config(), constraints_path=market_path
        )

    raw["walk_forward_profiles"]["a_share_84m"]["test_months"] = 24
    raw["research_horizon_profiles"]["existing_data_66m_24m"]["test_months"] = 23
    research_path = tmp_path / "short_research.yaml"
    research_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="at least 24 months"):
        load_research_horizon(research_path)

    raw["research_horizon_profiles"]["existing_data_66m_24m"]["test_months"] = 24
    raw["research_horizon_profiles"]["existing_data_66m_24m"]["holdout_windows"] = 2
    raw["research_horizon_profiles"]["existing_data_66m_24m"]["ranking_windows"] = 1
    duplicate_holdout_path = tmp_path / "duplicate_holdout_research.yaml"
    duplicate_holdout_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one continuous"):
        load_research_horizon(duplicate_holdout_path)


def test_market_artifact_report_exposes_independent_contract():
    from src.search.reporting import render_optimizer_report

    html = render_optimizer_report(
        {
            "group": "hk",
            "strategy_id": "regime_pullback",
            "solver_id": "simulated_annealing",
            "gate_profile": "standard",
            "market_config_hash": "abc123",
            "parameter_schema": "regime-pullback/1",
            "timestamp": "2026-09-04T00:00:00",
            "market_contract": {
                "walk_forward_profile": "hk_84m",
                "execution_profile": "hk_hkd",
                "benchmark_profile": "hk",
            },
            "search": {"gate_profile_hash": "gate123"},
            "activation": {"eligible": False},
        }
    )
    for value in (
        "regime_pullback",
        "simulated_annealing",
        "hk_84m",
        "hk_hkd",
        "hk",
        "abc123",
    ):
        assert value in html
