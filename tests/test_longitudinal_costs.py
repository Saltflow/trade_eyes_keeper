from src.fundamental_quant.comparison_transport import measure_request_tokens
from src.fundamental_quant.longitudinal_costs import (
    FLASH_RATES,
    estimate_cost,
    usage_cost,
)


def test_cache_rates_and_unknown_usage():
    result = estimate_cost(1000000, 1000000, 1000000)
    assert result["off_peak"] == "4.02"
    assert result["peak"] == "8.04"
    assert (
        usage_cost(
            {"input_tokens": 100, "output_tokens": 20, "unknown_usage_calls": 1},
            FLASH_RATES,
        )["status"]
        == "known_usage_only_total_charge_unknown"
    )
    assert usage_cost({})["status"] == "rate_profile_not_bound_to_campaign"


def test_image_bytes_are_not_text_tokens_and_original_preserved():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + "x" * 50000},
                }
            ],
        }
    ]
    actual = measure_request_tokens(messages, len, 1024)
    assert 1024 < actual < 1500
    assert len(messages[0]["content"][0]["image_url"]["url"]) > 50000
    assert measure_request_tokens(messages, len, None) > 50000
