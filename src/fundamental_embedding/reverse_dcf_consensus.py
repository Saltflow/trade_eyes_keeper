"""Multi-expert reverse-DCF diagnostics at an observed CAPM equity cost.

Reverse DCF is a market-expectation diagnostic, not a fair-value forecast.
Different experts use different equity-cash-flow proxies, so this module keeps
their individual implied growth rates and only reports a robust consensus with
explicit count and dispersion. It never fabricates cash flow or lets market
implied growth feed the intrinsic-value engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .intrinsic_value import (
    ExpertValuation,
    IntrinsicValueConfig,
    IntrinsicValueEstimate,
    ValuationSnapshot,
    _discounted_growth_value,
    _finite,
)

REVERSE_DCF_CONSENSUS_CONTRACT = "market-cost-reverse-dcf-consensus-1"


@dataclass(frozen=True)
class ReverseDcfExpertCandidate:
    expert_id: str
    cash_flow_kind: str | None
    available: bool
    compatibility: float
    cash_per_share: float | None
    implied_growth: float | None
    discount_rate: float | None
    terminal_growth: float | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReverseDcfConsensus:
    status: str
    implied_growth: float | None
    lower_growth: float | None
    upper_growth: float | None
    dispersion: float | None
    candidate_count: int
    candidates: tuple[ReverseDcfExpertCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["candidates"] = [item.to_dict() for item in self.candidates]
        return result


def _market_discount_rate(estimate: IntrinsicValueEstimate) -> float | None:
    policy = estimate.required_return_policy.get("market_cost_of_equity", {})
    return _finite(policy.get("base"))


def _solve_market_implied_growth(
    cash_per_share: float,
    market_price: float,
    discount_rate: float,
    terminal_growth: float,
    years: int,
    growth_low: float,
    growth_high: float,
) -> float | None:
    if (
        cash_per_share <= 0
        or market_price <= 0
        or discount_rate <= terminal_growth + 0.0025
        or growth_low >= growth_high
    ):
        return None
    low_value = _discounted_growth_value(
        cash_per_share, growth_low, discount_rate, terminal_growth, years
    )
    high_value = _discounted_growth_value(
        cash_per_share, growth_high, discount_rate, terminal_growth, years
    )
    if (
        low_value is None
        or high_value is None
        or not low_value <= market_price <= high_value
    ):
        return None
    low, high = float(growth_low), float(growth_high)
    for _ in range(80):
        middle = (low + high) / 2.0
        value = _discounted_growth_value(
            cash_per_share, middle, discount_rate, terminal_growth, years
        )
        if value is None:
            return None
        if value < market_price:
            low = middle
        else:
            high = middle
    return float((low + high) / 2.0)


def _candidate(
    item: ExpertValuation,
    estimate: IntrinsicValueEstimate,
    snapshot: ValuationSnapshot,
    config: IntrinsicValueConfig,
    growth_low: float,
    growth_high: float,
) -> ReverseDcfExpertCandidate:
    cash = _finite(item.assumptions.get("cash_per_share"))
    discount_rate = _market_discount_rate(estimate)
    terminal = _finite(item.assumptions.get("terminal_growth"))
    kind = item.assumptions.get("cash_flow_kind")
    if cash is None or cash <= 0:
        return ReverseDcfExpertCandidate(
            item.expert_id,
            kind,
            item.available,
            item.compatibility,
            cash,
            None,
            discount_rate,
            terminal,
            "no_positive_equity_cash_flow",
        )
    if discount_rate is None or terminal is None:
        return ReverseDcfExpertCandidate(
            item.expert_id,
            kind,
            item.available,
            item.compatibility,
            cash,
            None,
            discount_rate,
            terminal,
            "missing_market_discount_inputs",
        )
    implied = _solve_market_implied_growth(
        cash,
        snapshot.current_price,
        discount_rate,
        terminal,
        config.projection_years,
        growth_low,
        growth_high,
    )
    if implied is None:
        status = "market_price_outside_solver_range"
    elif not item.available:
        status = "solved_but_expert_unavailable"
    else:
        status = "solved"
    return ReverseDcfExpertCandidate(
        item.expert_id,
        kind,
        item.available,
        item.compatibility,
        cash,
        implied,
        discount_rate,
        terminal,
        status,
    )


def build_reverse_dcf_consensus(
    estimate: IntrinsicValueEstimate,
    snapshot: ValuationSnapshot,
    config: IntrinsicValueConfig,
    *,
    minimum_candidates: int = 2,
    growth_low: float = -0.50,
    growth_high: float = 1.00,
) -> ReverseDcfConsensus:
    """Return per-expert CAPM diagnostics plus a weighted-median consensus.

    Only experts that are intrinsically available and have a positive equity
    cash-flow proxy enter the consensus. A single candidate is retained as a
    clearly labelled single-expert result; no candidate is silently imputed.
    The wider default range is diagnostic only and deliberately does not alter
    the intrinsic-value forecast bounds.
    """

    candidates = tuple(
        _candidate(
            item,
            estimate,
            snapshot,
            config,
            growth_low,
            growth_high,
        )
        for item in estimate.experts
    )
    usable = [
        item
        for item in candidates
        if item.status == "solved" and item.implied_growth is not None
    ]
    if not usable:
        return ReverseDcfConsensus(
            "unresolved", None, None, None, None, 0, candidates
        )
    values = np.asarray([item.implied_growth for item in usable], dtype=float)
    weights = np.asarray(
        [max(float(item.compatibility), 1e-6) for item in usable], dtype=float
    )
    order = np.argsort(values)
    ordered_values = values[order]
    ordered_weights = weights[order]
    midpoint = float(ordered_weights.sum() / 2.0)
    index = int(
        np.searchsorted(np.cumsum(ordered_weights), midpoint, side="left")
    )
    index = min(index, len(ordered_values) - 1)
    center = float(ordered_values[index])
    deviations = np.abs(values - center)
    dispersion = float(np.median(deviations)) if len(values) else None
    lower = float(np.min(values))
    upper = float(np.max(values))
    status = (
        "solved_consensus"
        if len(usable) >= max(2, int(minimum_candidates))
        else "solved_single_expert"
    )
    return ReverseDcfConsensus(
        status, center, lower, upper, dispersion, len(usable), candidates
    )


def consensus_rows(consensus: ReverseDcfConsensus) -> dict[str, Any]:
    """Flatten the consensus for CSV/report consumers."""

    return {
        "market_implied_growth_5y": consensus.implied_growth,
        "market_implied_growth_low_5y": consensus.lower_growth,
        "market_implied_growth_high_5y": consensus.upper_growth,
        "market_implied_growth_dispersion": consensus.dispersion,
        "market_implied_growth_expert_count": consensus.candidate_count,
        "market_implied_growth_status": consensus.status,
        "market_implied_growth_candidates": [
            item.to_dict() for item in consensus.candidates
        ],
        "reverse_dcf_contract": REVERSE_DCF_CONSENSUS_CONTRACT,
    }
