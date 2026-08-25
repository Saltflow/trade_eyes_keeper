"""Point-in-time routing for economically incompatible valuation models.

The router is intentionally small and deterministic.  It does not rank shares
or read future prices: it selects a valuation *method* from an industry label
already published on the evaluation date and from disclosed annual cash-flow
history.  Financial companies do not route through ``OCF - CAPEX`` because
their operating cash flow contains balance-sheet funding flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import numpy as np


VALUATION_ROUTER_CONTRACT = "point-in-time-valuation-router-1"

# CSRC/CAPCO J is financial services.  The fine codes cover monetary banking,
# capital markets, insurance, and other financial services.
FINANCIAL_INDUSTRY_PREFIXES = ("J",)

# A deliberately narrow list of cyclical classification groups.  A company
# can additionally be routed by objectively unstable cash history, so this is
# not intended to be an exhaustive industry ontology.
DEFAULT_CYCLICAL_INDUSTRY_PREFIXES = (
    "B",  # mining
    "C25",  # petroleum, coal and other fuel processing
    "C26",  # chemicals and chemical products
    "C30",  # non-metallic mineral products
    "C31",  # ferrous metals
    "C32",  # non-ferrous metals
    "C33",  # fabricated metals
    "G55",  # water transport
)


class ValuationModel(str, Enum):
    """Models with distinct economic cash-flow definitions."""

    FCFE_DCF = "fcfe_dcf"
    NORMALIZED_FCFE_DCF = "normalized_fcfe_dcf"
    RESIDUAL_INCOME = "residual_income"


@dataclass(frozen=True)
class CashFlowHistorySummary:
    """Causal summary of already-screened annual FCFE-proxy observations."""

    values: tuple[float, ...]
    normalized_cash_per_share: float | None
    latest_to_normalized: float | None
    coefficient_of_variation: float | None

    @property
    def observation_count(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class ValuationRoute:
    """Auditable model selection, independent from model parameters."""

    model: ValuationModel
    reason: str
    industry_code: str | None
    industry_name: str | None


def summarize_cash_flow_history(
    values: Iterable[float],
    *,
    window: int = 7,
) -> CashFlowHistorySummary:
    """Normalize positive annual FCFE proxies with a trailing median."""

    usable = tuple(
        float(item)
        for item in values
        if np.isfinite(item) and float(item) > 0.0
    )
    sample = usable[-window:]
    normalized = float(np.median(sample)) if len(sample) >= 3 else None
    latest = usable[-1] if usable else None
    peak_ratio = (
        float(latest / normalized)
        if latest is not None and normalized is not None and normalized > 0.0
        else None
    )
    coefficient = None
    if len(sample) >= 3:
        mean = float(np.mean(sample))
        if mean > 0.0:
            coefficient = float(np.std(sample, ddof=0) / mean)
    return CashFlowHistorySummary(
        values=usable,
        normalized_cash_per_share=normalized,
        latest_to_normalized=peak_ratio,
        coefficient_of_variation=coefficient,
    )


def route_valuation_model(
    *,
    industry_code: str | None,
    industry_name: str | None,
    cash_flow_history: CashFlowHistorySummary,
    cyclical_industry_prefixes: Iterable[str] = DEFAULT_CYCLICAL_INDUSTRY_PREFIXES,
    peak_ratio_threshold: float = 1.75,
    cash_flow_cv_threshold: float = 0.60,
) -> ValuationRoute:
    """Select residual income for financials, normalized FCFE for cycles."""

    code = str(industry_code or "")
    if code.startswith(FINANCIAL_INDUSTRY_PREFIXES):
        return ValuationRoute(
            model=ValuationModel.RESIDUAL_INCOME,
            reason="financial_industry_ocf_not_fcfe",
            industry_code=industry_code,
            industry_name=industry_name,
        )
    if any(code.startswith(prefix) for prefix in cyclical_industry_prefixes):
        return ValuationRoute(
            model=ValuationModel.NORMALIZED_FCFE_DCF,
            reason="cyclical_industry_normalized_cash_flow",
            industry_code=industry_code,
            industry_name=industry_name,
        )
    if (
        cash_flow_history.latest_to_normalized is not None
        and cash_flow_history.latest_to_normalized >= peak_ratio_threshold
    ):
        return ValuationRoute(
            model=ValuationModel.NORMALIZED_FCFE_DCF,
            reason="cash_flow_peak_normalized",
            industry_code=industry_code,
            industry_name=industry_name,
        )
    if (
        cash_flow_history.coefficient_of_variation is not None
        and cash_flow_history.coefficient_of_variation >= cash_flow_cv_threshold
    ):
        return ValuationRoute(
            model=ValuationModel.NORMALIZED_FCFE_DCF,
            reason="cash_flow_volatility_normalized",
            industry_code=industry_code,
            industry_name=industry_name,
        )
    return ValuationRoute(
        model=ValuationModel.FCFE_DCF,
        reason="ordinary_company_fcfe_proxy",
        industry_code=industry_code,
        industry_name=industry_name,
    )


def residual_income_per_share(
    *,
    book_value_per_share: float,
    roe: float,
    payout_ratio: float,
    cost_of_equity: float,
    terminal_growth: float,
    projection_years: int,
    roe_fade: float,
    minimum_discount_spread: float,
) -> float | None:
    """Value equity as current book plus discounted residual income.

    ROE exponentially fades toward the CAPM cost of equity.  This is a
    conservative residual-income model: it accepts no operating cash-flow
    figure, and a negative terminal residual is retained rather than silently
    discarded.
    """

    if (
        not np.isfinite(book_value_per_share)
        or book_value_per_share <= 0.0
        or not np.isfinite(roe)
        or not np.isfinite(payout_ratio)
        or not np.isfinite(cost_of_equity)
        or cost_of_equity <= terminal_growth + minimum_discount_spread
        or projection_years < 1
        or not 0.0 <= payout_ratio <= 1.0
        or not 0.0 <= roe_fade <= 1.0
    ):
        return None
    value = float(book_value_per_share)
    book = float(book_value_per_share)
    current_roe = float(roe)
    for year in range(1, projection_years + 1):
        residual_income = (current_roe - cost_of_equity) * book
        value += residual_income / (1.0 + cost_of_equity) ** year
        earnings = current_roe * book
        book += earnings * (1.0 - payout_ratio)
        current_roe = cost_of_equity + (current_roe - cost_of_equity) * roe_fade
    terminal_residual = (current_roe - cost_of_equity) * book
    terminal_value = terminal_residual * (1.0 + terminal_growth) / (
        cost_of_equity - terminal_growth
    )
    value += terminal_value / (1.0 + cost_of_equity) ** projection_years
    return float(value) if np.isfinite(value) and value > 0.0 else None
