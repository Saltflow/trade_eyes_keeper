"""Cash valuation uses the same configured fixed FX as portfolio execution.

Original declared amounts remain evidence. Converted cash is a simulation
valuation, never a claim about the issuer's actual settlement currency.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy

CORPORATE_ACTION_CURRENCY_CONTRACT = "declared-cash+configured-fixed-fx/1"


def native_currency(code: str) -> str:
    if re.fullmatch(r"\d{5}", str(code)):
        return "HKD"
    if re.fullmatch(r"\d{6}", str(code)):
        return "CNY"
    return "USD"


def _rate(rates: dict, key: str) -> float:
    value = rates[key]
    if isinstance(value, bool):
        raise TypeError("fixed FX rate must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("fixed FX rate must be finite and positive")
    return value


def _digest(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def validate_cash_currency(action, expected: str | None = None) -> None:
    if getattr(action, "cash_per_share", None) is None:
        return
    expected = expected or native_currency(action.code)
    currency = getattr(action, "currency", None)
    if currency is None and expected != "HKD":
        return
    if currency != expected:
        raise ValueError(
            f"cash currency mismatch: {action.code} {currency} != {expected}"
        )
    declared = getattr(action, "declared_currency", None)
    if not declared or declared == currency:
        return
    proof = getattr(action, "cash_conversion", None) or {}
    try:
        record = proof["source_record"]
        rates = record["fx_rates"]
        rate = _rate(rates, "us") / _rate(rates, "hk")
        original = float(action.declared_cash_per_share)
        valid = (
            declared == "USD"
            and currency == "HKD"
            and proof.get("from_currency") == declared
            and proof.get("to_currency") == currency
            and proof.get("method") == CORPORATE_ACTION_CURRENCY_CONTRACT
            and record.get("source") == "optimizer_execution_profile"
            and bool(record.get("execution_profile"))
            and proof["source_sha256"] == _digest(record)
            and math.isclose(float(proof["rate"]), rate, rel_tol=1e-12)
            and math.isfinite(original)
            and original > 0
            and math.isclose(
                float(action.cash_per_share), original * rate, rel_tol=1e-12
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(
            f"invalid foreign cash valuation evidence: {action.code} {action.ex_date}"
        )


class FixedCashValuation:
    """Resolve the HK execution profile lazily; no FX network client exists."""

    def __init__(self, config=None, *, constraints_path=None):
        self.config = config
        self.constraints_path = constraints_path
        self._proof = None

    def quote(self) -> dict:
        if self._proof is None:
            from ..search.config import load_market_optimizer_config

            resolved = load_market_optimizer_config(
                "hk", self.config, self.constraints_path
            )
            rates = resolved.constraints.execution.fx_rates
            record = {
                "source": "optimizer_execution_profile",
                "execution_profile": resolved.execution_profile,
                "fx_rates": {"us": _rate(rates, "us"), "hk": _rate(rates, "hk")},
                "rate_unit": "CNY_per_native_currency_unit",
            }
            self._proof = {
                "from_currency": "USD",
                "to_currency": "HKD",
                "rate": record["fx_rates"]["us"] / record["fx_rates"]["hk"],
                "method": CORPORATE_ACTION_CURRENCY_CONTRACT,
                "basis": "configured_fixed_HKD_valuation_not_actual_HKD_receipt",
                "source_record": record,
                "source_sha256": _digest(record),
            }
        return deepcopy(self._proof)

    def convert(self, action):
        declared = getattr(action, "declared_currency", None) or action.currency
        if declared == "HKD" and action.currency == "HKD":
            validate_cash_currency(action, "HKD")
            return action
        if declared != "USD" or action.published_at is None:
            raise ValueError(
                f"unsupported or unverified foreign cash currency: {action.code} {declared}"
            )
        # Revalue an old conversion from its retained original, never multiply
        # already converted HKD again. Missing original evidence is an error.
        original = (
            action.declared_cash_per_share
            if action.declared_currency
            else action.cash_per_share
        )
        if original is None:
            raise ValueError("foreign cash conversion is missing original amount")
        result = deepcopy(action)
        result.declared_cash_per_share = original
        result.declared_currency = declared
        result.cash_conversion = self.quote()
        result.cash_per_share = original * result.cash_conversion["rate"]
        result.currency = "HKD"
        marker = "cash_is_configured_fixed_HKD_valuation_not_issuer_HKD_payment"
        result.diagnostics = [
            x
            for x in result.diagnostics
            if x != "cash_is_HKD_ex_date_valuation_not_issuer_HKD_payment"
        ]
        if marker not in result.diagnostics:
            result.diagnostics.append(marker)
        validate_cash_currency(result, "HKD")
        return result


def revalue_cached_bundle(bundle, config=None):
    """Rebuild cash and qfq in memory from cached originals under current FX.

    Preserve the disk evidence and immutable optimizer runs. Do not combine
    freshly converted cash with factors calculated under a previous rate.
    """
    foreign = [
        a
        for a in bundle.actions
        if a.cash_per_share is not None
        and (a.declared_currency or a.currency) == "USD"
        and native_currency(a.code) == "HKD"
    ]
    if not foreign:
        return bundle
    converter = FixedCashValuation(config)
    revised = [converter.convert(a) if a in foreign else a for a in bundle.actions]
    if all(a.dict() == b.dict() for a, b in zip(bundle.actions, revised)):
        return bundle
    evidence = next(
        (
            x.split(":", 1)[1]
            for x in bundle.diagnostics
            if x.startswith("action_coverage:")
        ),
        None,
    )
    if not evidence:
        raise ValueError("cached foreign cash lacks complete action coverage evidence")
    from .price_adjustments import apply_disclosed_adjustments

    return apply_disclosed_adjustments(bundle, revised, evidence=evidence)
