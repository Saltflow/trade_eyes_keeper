from datetime import date

import numpy as np
import pytest

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.valuation_consensus_context import (
    ValuationConsensusQualityConfig,
    attach_consensus_valuation_features,
)


def _raw():
    return FundamentalPricingDataset(
        feature_dates=(date(2025, 3, 31), date(2025, 3, 31)),
        label_end_dates=(date(2025, 6, 30), date(2025, 6, 30)),
        symbols=("000001", "000002"),
        feature_names=("earnings_yield",),
        values=np.ones((2, 1), dtype=float),
        availability_mask=np.ones((2, 1), dtype=bool),
        forward_returns=np.asarray([0.1, -0.1]),
        excess_returns=np.asarray([0.1, -0.1]),
    )


def _row(symbol, *, dispersion=0.05, status="solved_consensus"):
    return {
        "symbol": symbol,
        "feature_date": "2025-03-31",
        "status": "solved",
        "market_implied_growth_status": status,
        "market_implied_growth_expert_count": 2,
        "market_implied_growth_dispersion": dispersion,
        "beta": 1.0,
        "capm_cost_of_equity": 0.08,
        "market_implied_growth_5y": 0.10,
        "market_vs_fundamental_growth": 0.02,
        "fundamental_growth": 0.08,
    }


def test_quality_gate_masks_high_dispersion_without_imputation():
    result = attach_consensus_valuation_features(
        _raw(), [_row("1"), _row("2", dispersion=0.11)]
    )
    assert result.metadata["contract"] == (
        "historical-valuation-consensus-source-1"
    )
    assert result.metadata["gated_valuation_rows"] == 1
    assert result.availability_mask[0, -1]
    assert not result.availability_mask[1, -1]
    assert result.values[1, -1] == 0.0


def test_quality_gate_requires_consensus_and_expert_count():
    with pytest.raises(ValueError, match="duplicate"):
        attach_consensus_valuation_features(_raw(), [_row("1"), _row("000001")])
    result = attach_consensus_valuation_features(
        _raw(),
        [_row("1", status="solved_single_expert"), _row("2")],
        ValuationConsensusQualityConfig(minimum_expert_count=2),
    )
    assert not result.availability_mask[0, -1]
    assert result.availability_mask[1, -1]


def test_quality_config_rejects_invalid_threshold():
    with pytest.raises(ValueError, match="non-negative"):
        ValuationConsensusQualityConfig(maximum_dispersion=-0.1).validate()
