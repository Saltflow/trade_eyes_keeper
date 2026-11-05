"""Shared market execution and manual walk-forward profile contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.search.config import (
    get_market_optimizer_config,
    get_market_optimizer_configs,
)


def _config():
    return yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))


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








def test_active_market_contracts_preserve_production_nine_month_geometry():
    for resolved in get_market_optimizer_configs(_config()).values():
        horizon = resolved.constraints.walk_forward
        assert horizon.state_lookback_months == 12
        assert horizon.test_months == 9
        assert horizon.step_months == 3
        assert horizon.num_windows == 22
        assert horizon.independent_ranking_window_count == 16
        assert horizon.purge_overlap_window_count == 2
        assert horizon.held_out_window_count == 4
        assert horizon.holdout_calendar_span_months == 18
        assert horizon.total_months_needed == 84


@pytest.mark.parametrize(("field", "value"), [("test_months", 0), ("step_months", 0), ("validation_windows", -1)])
def test_invalid_production_window_lengths_fail_closed(tmp_path, field, value):
    import yaml

    with open("config/optimizer_constraints.yaml", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    raw["walk_forward_profiles"]["a_share_84m"][field] = value
    path = tmp_path / "invalid_window.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Walk-Forward profile"):
        get_market_optimizer_config(
            "a_share", application_config=_config(), constraints_path=path
        )




def test_legacy_solver_metadata_is_optional_and_never_loads_a_solver():
    config = _config()
    config["optimizer"]["markets"]["hk"]["solver_id"] = "retired_unknown_solver"
    config["optimizer"]["markets"]["hk"]["gate_profile"] = "retired_unknown_gate"
    resolved = get_market_optimizer_configs(config)
    assert resolved["hk"].solver_id == "retired_unknown_solver"
    assert not hasattr(resolved["hk"].constraints, "search")
    config["optimizer"]["markets"]["hk"].pop("solver_id")
    config["optimizer"]["markets"]["hk"].pop("gate_profile")
    assert get_market_optimizer_config("hk", application_config=config).solver_id == ""


def test_missing_walk_forward_profile_fails_closed():
    config = _config()
    config["optimizer"]["markets"]["hk"]["walk_forward_profile"] = "missing"
    with pytest.raises(ValueError, match="profile"):
        get_market_optimizer_config("hk", application_config=config)


def test_market_benchmark_state_remains_independent():
    resolved = get_market_optimizer_configs(_config())
    resolved["a_share"].constraints.benchmark_codes.append("MUTATION")
    assert "MUTATION" not in resolved["hk"].constraints.benchmark_codes
