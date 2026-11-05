"""The extension points must be discoverable without reading implementations."""

from src.strategy import TradingStrategy, list_strategy_ids


def test_public_strategy_api_and_automatic_discovery():
    assert TradingStrategy.__abstractmethods__ == {
        "make_signals",
        "param_space",
        "to_human_readable",
    }
    assert list_strategy_ids() == (
        "capm_dcf_value",
        "causal_trend_allocation",
        "justified_pb_value",
        "ke_threshold_allocation",
        "ma60_band",
        "percentile",
        "regime_pullback",
        "simplified",
        "technical_ensemble",
        "valuation_aware_ensemble",
    )
