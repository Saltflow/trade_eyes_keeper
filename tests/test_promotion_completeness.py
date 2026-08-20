from src.search.promotion import PromotionPolicy, compare_with_incumbent


def _report(value):
    return {
        "total_return": value,
        "max_drawdown": -5.0,
        "trade_count": 2,
        "benchmark_returns": {"one": 1.0, "two": 2.0, "three": 3.0},
    }


def test_two_matching_markets_cannot_promote_three_market_strategy():
    candidate = {"a_share": _report(3.0), "hk": _report(3.0)}
    incumbent = {"a_share": _report(0.0), "hk": _report(0.0)}

    decision = compare_with_incumbent(candidate, incumbent, PromotionPolicy())

    assert not decision.passed
    assert decision.reasons == ("incomplete_group_comparison",)
