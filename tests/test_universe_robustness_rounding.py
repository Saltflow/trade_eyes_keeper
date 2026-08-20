from src.search.config import UniverseRobustnessConfig


def test_small_universe_can_explicitly_tolerate_one_failed_variant():
    config = UniverseRobustnessConfig(
        {
            "minimum_passing_ratio": 0.80,
            "small_universe_threshold": 5,
            "small_universe_allowed_failures": 1,
        }
    )

    assert config.required_positive_variants(4) == 3
    assert config.required_positive_variants(5) == 4
    assert config.required_positive_variants(9) == 8


def test_default_preserves_strict_ratio_rounding():
    config = UniverseRobustnessConfig({"minimum_passing_ratio": 0.80})

    assert config.required_positive_variants(4) == 4
