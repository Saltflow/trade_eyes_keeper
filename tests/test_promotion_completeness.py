from src.search.promotion import PromotionPolicy, compare_with_incumbent


def _report(value, benchmarks=None):
    return {
        "total_return": value,
        "max_drawdown": -5.0,
        "trade_count": 2,
        "benchmark_returns": dict(
            benchmarks
            or {"one": 1.0, "two": 2.0, "three": 3.0}
        ),
    }


def test_two_matching_markets_cannot_promote_three_market_strategy():
    candidate = {"a_share": _report(3.0), "hk": _report(3.0)}
    incumbent = {"a_share": _report(0.0), "hk": _report(0.0)}

    decision = compare_with_incumbent(candidate, incumbent, PromotionPolicy())

    assert not decision.passed
    assert decision.reasons == ("incomplete_group_comparison",)


def test_framework_benchmark_in_addition_to_three_controls_is_complete():
    benchmarks = {
        "risk_free": 1.0,
        "broad": 2.0,
        "dividend": 3.0,
        "universe_equal_weight": 4.0,
    }
    candidate = {
        group: _report(3.0, benchmarks)
        for group in ("a_share", "hk", "us")
    }
    incumbent = {
        group: _report(0.0, benchmarks)
        for group in ("a_share", "hk", "us")
    }

    decision = compare_with_incumbent(candidate, incumbent, PromotionPolicy())

    assert decision.passed


def test_fewer_than_configured_minimum_benchmarks_is_incomplete():
    benchmarks = {"risk_free": 1.0, "broad": 2.0}
    candidate = {
        group: _report(3.0, benchmarks)
        for group in ("a_share", "hk", "us")
    }
    incumbent = {
        group: _report(0.0, benchmarks)
        for group in ("a_share", "hk", "us")
    }

    decision = compare_with_incumbent(
        candidate,
        incumbent,
        PromotionPolicy(minimum_benchmark_count=3),
    )

    assert not decision.passed
    assert decision.reasons == (
        "a_share:incomplete_benchmarks",
        "hk:incomplete_benchmarks",
        "us:incomplete_benchmarks",
    )
