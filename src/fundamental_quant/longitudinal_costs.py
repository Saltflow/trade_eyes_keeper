"""Versioned rate estimates; never represent a tariff estimate as a bill."""

from decimal import Decimal

FLASH_RATES = {
    "model": "deepseek-flash",
    "currency": "CNY",
    "per_tokens": 1000000,
    "verified_at": "2026-10-10",
    "effective_as_of": "2026-10-10",
    "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
    "off_peak": {"input_hit": "0.02", "input_miss": "1", "output": "4"},
    "peak": {"input_hit": "0.04", "input_miss": "2", "output": "8"},
    "note": "Published tariff checked 2026-10-10; no output-cache discount assumed; verify again for later campaigns",
}
FLASH_CAPABILITIES = {
    "context_tokens": 1000000,
    "max_images": 600,
    "image_token_upper_bound": 1024,
    "vision_source": "https://api-docs.deepseek.com/guides/vision/",
    "max_output_tokens": 393216,
    "verified_source": "https://api-docs.deepseek.com/quick_start/pricing/",
    "verified_at": "2026-10-10",
    "max_request_bytes": 48 * 1024 * 1024,
    "request_byte_limit_source": "local_conservative_transport_limit",
}


def estimate_cost(input_tokens, output_tokens, hit_tokens=0, rates=None):
    rates = rates or FLASH_RATES
    if (
        any(
            type(x) is not int or x < 0
            for x in (input_tokens, output_tokens, hit_tokens)
        )
        or hit_tokens > input_tokens
    ):
        raise ValueError("invalid_usage")
    result = {
        "status": "tariff_estimate_not_invoice",
        "currency": rates["currency"],
        "rate_profile": rates,
    }
    for period in ("off_peak", "peak"):
        price = rates[period]
        result[period] = str(
            (
                Decimal(hit_tokens) * Decimal(price["input_hit"])
                + Decimal(input_tokens - hit_tokens) * Decimal(price["input_miss"])
                + Decimal(output_tokens) * Decimal(price["output"])
            )
            / Decimal(rates["per_tokens"])
        )
    return result


def usage_cost(usage, rates=None):
    if rates is None:
        return {"status": "rate_profile_not_bound_to_campaign"}
    cache = usage.get("provider_cache_usage", {})
    hit = cache.get("prompt_cache_hit_tokens", 0)
    result = estimate_cost(
        usage.get("input_tokens", 0), usage.get("output_tokens", 0), hit, rates
    )
    result["cache_measurement"] = (
        "provider"
        if "prompt_cache_hit_tokens" in cache
        else "unknown_assumed_all_miss_for_estimate"
    )
    if usage.get("unknown_usage_calls") or usage.get("reservation_uncertain_calls"):
        result["status"] = "known_usage_only_total_charge_unknown"
    return result
