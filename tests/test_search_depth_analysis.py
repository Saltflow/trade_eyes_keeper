import pytest

from src.analysis.search_depth_analysis import (
    aggregate_market_records,
    build_depth_checkpoints,
    choose_balance_depth,
    marginal_effect_improvement,
)


def test_build_depth_checkpoints_includes_six_nested_points():
    assert build_depth_checkpoints(1000, 10000, 6) == [
        1000,
        2800,
        4600,
        6400,
        8200,
        10000,
    ]


def test_build_depth_checkpoints_rejects_duplicate_integer_points():
    with pytest.raises(ValueError):
        build_depth_checkpoints(1, 2, 3)


def test_marginal_effect_uses_positive_score_index_for_negative_scores():
    assert marginal_effect_improvement(-10.0, -5.0) == pytest.approx(
        5.0 / 90.0 * 100.0
    )


def test_balance_depth_requires_all_later_steps_to_remain_below_threshold():
    depths = [1000, 2800, 4600, 6400]
    improvements = [None, 3.0, 7.0, 2.0]
    assert choose_balance_depth(
        depths,
        improvements,
        [False, False, False, False],
    ) == 4600


def test_later_ranking_eligible_candidate_prevents_premature_balance():
    depths = [1000, 2800, 4600]
    improvements = [None, 2.0, 2.0]
    assert choose_balance_depth(
        depths,
        improvements,
        [False, False, True],
    ) == 4600


def test_aggregate_market_records_uses_all_three_markets():
    depths = [1000, 2800]
    market_records = {}
    for group, score in (("a_share", -10.0), ("hk", 0.0), ("us", 10.0)):
        market_records[group] = [
            {
                "best_raw_wf_score": score,
                "eligible_candidate_count": 0,
                "elapsed_seconds": 1.0,
            },
            {
                "best_raw_wf_score": score + 1.0,
                "eligible_candidate_count": 1,
                "elapsed_seconds": 2.0,
            },
        ]
    aggregate, balance = aggregate_market_records(depths, market_records)
    assert aggregate[0]["mean_best_raw_wf_score"] == pytest.approx(0.0)
    assert aggregate[1]["eligible_market_count"] == 3
    assert aggregate[1]["all_markets_ranking_eligible"] is True
    assert aggregate[1]["cumulative_seconds"] == pytest.approx(6.0)
    assert balance == 2800
