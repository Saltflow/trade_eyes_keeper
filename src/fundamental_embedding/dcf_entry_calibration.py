"""No-lookahead calibration for a CAPM equity-cash-flow DCF entry price.

This module is deliberately separate from :mod:`intrinsic_value`.  The older
engine keeps its explicit investor hurdle and multi-expert interval report so
that historic reports remain reproducible.  Here the discount rate is only
the CAPM cost of equity::

    r_e = r_f + beta * ERP

There is no debt-cost or capital-structure term because the discounted cash
flow is an equity cash-flow proxy.  A terminal growth rate of 2% is fixed.
The five-year growth forecast is built only from statements published by the
valuation date.  Future prices are used exclusively as labels.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from math import sqrt
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from src.data.market_history import PriceHistoryBundle
from src.fundamental_embedding.capital_cost import estimate_robust_beta
from src.fundamental_embedding.industry_history import IndustryClassificationHistoryStore
from src.fundamental_embedding.valuation_router import (
    DEFAULT_CYCLICAL_INDUSTRY_PREFIXES,
    ValuationModel,
    residual_income_per_share,
    route_valuation_model,
    summarize_cash_flow_history,
)
from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    adjust_statement_shares,
)

DCF_ENTRY_CALIBRATION_CONTRACT = "capm-equity-dcf-entry-calibration-1"


@dataclass(frozen=True)
class GrowthWeights:
    """Weights for independently observable five-year growth components."""

    name: str
    revenue_cagr: float
    earnings_cagr: float
    fcf_cagr: float
    roe_reinvestment: float
    roe_trend: float


DEFAULT_GROWTH_PROFILES = (
    GrowthWeights("cash_focus", 0.10, 0.20, 0.45, 0.20, 0.05),
    GrowthWeights("balanced", 0.20, 0.25, 0.25, 0.20, 0.10),
    GrowthWeights("quality_reinvestment", 0.10, 0.20, 0.15, 0.40, 0.15),
    GrowthWeights("business_growth", 0.35, 0.35, 0.10, 0.10, 0.10),
)


@dataclass(frozen=True)
class CapmDcfEntryParameters:
    """One transparent DCF parameter set explored by the calibrator."""

    growth_weights: GrowthWeights
    growth_floor: float
    growth_cap: float
    entry_fair_value_fraction: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "CapmDcfEntryParameters":
        """Restore a frozen, auditable policy without re-running selection."""

        raw_weights = value.get("growth_weights")
        if not isinstance(raw_weights, Mapping):
            raise ValueError("frozen DCF policy is missing growth_weights")
        try:
            weights = GrowthWeights(
                name=str(raw_weights["name"]),
                revenue_cagr=float(raw_weights["revenue_cagr"]),
                earnings_cagr=float(raw_weights["earnings_cagr"]),
                fcf_cagr=float(raw_weights["fcf_cagr"]),
                roe_reinvestment=float(raw_weights["roe_reinvestment"]),
                roe_trend=float(raw_weights["roe_trend"]),
            )
            return cls(
                growth_weights=weights,
                growth_floor=float(value["growth_floor"]),
                growth_cap=float(value["growth_cap"]),
                entry_fair_value_fraction=float(value["entry_fair_value_fraction"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid frozen DCF policy parameters") from exc


@dataclass(frozen=True)
class CapmDcfEntryConfig:
    """Economic contract, parameter grid, and entry-label requirements.

    ``erp_growth_multiplier_*`` is a subjective *conservatism* overlay.  A
    risk premium cannot responsibly increase a DCF target price.  Therefore
    the model discounts the financial growth forecast by this multiplier:
    ``g_used = g_financial / multiplier``.  The corresponding unadjusted and
    adjusted values are both written to the audit rows.
    """

    projection_years: int = 5
    terminal_growth: float = 0.02
    erp_growth_threshold: float = 0.05
    erp_growth_multiplier_high: float = 1.25
    erp_growth_multiplier_low: float = 1.50
    minimum_discount_spread: float = 0.0025
    minimum_growth_components: int = 2
    minimum_plausible_fcf_years: int = 2
    minimum_fcf_to_net_income: float = 0.05
    maximum_fcf_to_net_income: float = 2.50
    maximum_fcf_to_revenue: float = 0.80
    normalized_cash_flow_years: int = 7
    minimum_normalized_fcf_years: int = 3
    cyclical_peak_ratio_threshold: float = 1.75
    cyclical_cash_flow_cv_threshold: float = 0.60
    cyclical_industry_prefixes: tuple[str, ...] = (
        DEFAULT_CYCLICAL_INDUSTRY_PREFIXES
    )
    residual_income_roe_fade: float = 0.65
    maximum_entry_to_current_price: float = 0.95
    max_financial_age_days: int = 550
    max_market_staleness_days: int = 10
    hit_horizon_trading_days: int = 252
    outcome_horizon_trading_days: int = 252
    success_above_entry_fraction: float = 0.50
    target_success_rate: float = 0.70
    # These are calibration-evidence requirements, not a claim that a broad
    # equity universe should receive a DCF limit order in 20% of report
    # periods.  A disciplined value model should be sparse.  The separate
    # route sample requirement below prevents a tiny route from being treated
    # as evidence merely because it happened to contain one fortunate hit.
    minimum_hit_rate: float = 0.04
    minimum_hit_count: int = 12
    minimum_success_wilson_lower_95: float = 0.50
    minimum_route_training_episodes: int = 100
    train_fraction: float = 0.70
    growth_floors: tuple[float, ...] = (-0.05, 0.00, 0.03)
    growth_caps: tuple[float, ...] = (0.05, 0.10, 0.15)
    entry_fair_value_fractions: tuple[float, ...] = (
        0.05,
        0.075,
        0.10,
        0.125,
        0.15,
        0.175,
        0.20,
        0.25,
        0.30,
        0.35,
        0.45,
        0.55,
        0.65,
        0.75,
        0.85,
        0.90,
    )
    growth_profiles: tuple[GrowthWeights, ...] = DEFAULT_GROWTH_PROFILES

    def __post_init__(self) -> None:
        if self.projection_years < 1:
            raise ValueError("projection_years must be positive")
        if not 0.0 <= self.terminal_growth < 1.0:
            raise ValueError("terminal_growth must be between zero and one")
        if not 0.0 < self.entry_fair_value_fractions[0] <= 1.0:
            raise ValueError("entry_fair_value_fractions must be positive")
        if any(not 0.0 < value <= 1.0 for value in self.entry_fair_value_fractions):
            raise ValueError("entry_fair_value_fractions must be in (0, 1]")
        if not 0.0 < self.train_fraction < 1.0:
            raise ValueError("train_fraction must be in (0, 1)")
        if self.minimum_growth_components < 1:
            raise ValueError("minimum_growth_components must be positive")
        if self.minimum_plausible_fcf_years < 2:
            raise ValueError("minimum_plausible_fcf_years must be at least two")
        if not 0.0 <= self.minimum_fcf_to_net_income <= 1.0:
            raise ValueError("minimum_fcf_to_net_income must be in [0, 1]")
        if self.maximum_fcf_to_net_income <= self.minimum_fcf_to_net_income:
            raise ValueError("maximum_fcf_to_net_income must exceed its minimum")
        if not 0.0 < self.maximum_fcf_to_revenue <= 1.0:
            raise ValueError("maximum_fcf_to_revenue must be in (0, 1]")
        if self.normalized_cash_flow_years < self.minimum_normalized_fcf_years:
            raise ValueError("normalized cash-flow window cannot be too short")
        if self.minimum_normalized_fcf_years < 3:
            raise ValueError("minimum_normalized_fcf_years must be at least three")
        if self.cyclical_peak_ratio_threshold <= 1.0:
            raise ValueError("cyclical_peak_ratio_threshold must exceed one")
        if self.cyclical_cash_flow_cv_threshold <= 0.0:
            raise ValueError("cyclical_cash_flow_cv_threshold must be positive")
        if not 0.0 <= self.residual_income_roe_fade <= 1.0:
            raise ValueError("residual_income_roe_fade must be in [0, 1]")
        if not 0.0 < self.maximum_entry_to_current_price <= 1.0:
            raise ValueError("maximum_entry_to_current_price must be in (0, 1]")
        if self.minimum_hit_count < 1:
            raise ValueError("minimum_hit_count must be positive")
        if not 0.0 <= self.minimum_hit_rate <= 1.0:
            raise ValueError("minimum_hit_rate must be in [0, 1]")
        if not 0.0 <= self.minimum_success_wilson_lower_95 <= 1.0:
            raise ValueError("minimum Wilson lower bound must be in [0, 1]")
        if self.minimum_route_training_episodes < 1:
            raise ValueError("minimum route training episodes must be positive")

    def parameter_grid(self) -> Iterable[CapmDcfEntryParameters]:
        for profile in self.growth_profiles:
            for floor in self.growth_floors:
                for cap in self.growth_caps:
                    if floor > cap:
                        continue
                    for fraction in self.entry_fair_value_fractions:
                        yield CapmDcfEntryParameters(
                            growth_weights=profile,
                            growth_floor=float(floor),
                            growth_cap=float(cap),
                            entry_fair_value_fraction=float(fraction),
                        )


@dataclass(frozen=True)
class GrowthInputs:
    """Point-in-time inputs used to forecast explicit five-year growth."""

    revenue_cagr: float | None
    earnings_cagr: float | None
    fcf_cagr: float | None
    roe_reinvestment: float | None
    roe_trend: float | None
    annual_observation_count: int
    fcf_observation_count: int

    def values(self) -> dict[str, float | None]:
        return {
            "revenue_cagr": self.revenue_cagr,
            "earnings_cagr": self.earnings_cagr,
            "fcf_cagr": self.fcf_cagr,
            "roe_reinvestment": self.roe_reinvestment,
            "roe_trend": self.roe_trend,
        }


@dataclass(frozen=True)
class FinancialEquityInputs:
    """Point-in-time inputs for a residual-income valuation."""

    book_value_per_share: float | None
    roe: float | None
    payout_ratio: float | None
    payout_source: str | None


@dataclass(frozen=True)
class CapmInputs:
    evaluation_date: date
    risk_free_rate: float
    equity_risk_premium: float
    beta: float
    beta_observations: int

    @property
    def cost_of_equity(self) -> float:
        return self.risk_free_rate + self.beta * self.equity_risk_premium


@dataclass(frozen=True)
class DcfEntryEpisode:
    """A valuation snapshot plus target-only future price paths.

    The low and close paths are in the valuation-date share basis.  They use
    raw prices plus only future split/share multipliers, deliberately not a
    final-date forward-adjusted series.  This prevents future dividends from
    masquerading as an executable lower price.
    """

    symbol: str
    evaluation_date: date
    market_date: date
    current_price: float
    cash_per_share: float | None
    growth_inputs: GrowthInputs
    risk_free_rate: float
    beta: float
    beta_observations: int
    future_lows: np.ndarray
    future_closes: np.ndarray
    financial_age_days: int
    action_adjusted: bool
    valuation_model: ValuationModel = ValuationModel.FCFE_DCF
    valuation_route_reason: str = "ordinary_company_fcfe_proxy"
    industry_code: str | None = None
    industry_name: str | None = None
    normalized_cash_per_share: float | None = None
    cash_flow_peak_ratio: float | None = None
    cash_flow_coefficient_of_variation: float | None = None
    book_value_per_share: float | None = None
    normalized_roe: float | None = None
    payout_ratio: float | None = None
    payout_source: str | None = None


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _per_share(statement: FinancialStatementSnapshot, field: str) -> float | None:
    value = _finite(getattr(statement, field, None))
    shares = _finite(
        statement.common_shares_outstanding
        or statement.total_shares
        or statement.diluted_average_shares
    )
    if value is None or shares is None or shares <= 0:
        return None
    return value / shares


def _cagr(history: list[tuple[date, float]]) -> float | None:
    usable = [(when, value) for when, value in history if value > 0]
    if len(usable) < 2:
        return None
    first, last = usable[max(0, len(usable) - 4)], usable[-1]
    years = (last[0] - first[0]).days / 365.25
    if years <= 0:
        return None
    return float(np.power(last[1] / first[1], 1.0 / years) - 1.0)


def _annual_statements(
    statements: Iterable[FinancialStatementSnapshot],
    evaluation_date: date,
) -> list[FinancialStatementSnapshot]:
    eligible = [
        item
        for item in statements
        if item.period_type in {"year", "annual", "12M"}
        and item.period_end <= evaluation_date
        and item.published_at is not None
        and item.published_at <= evaluation_date
    ]
    latest: dict[date, FinancialStatementSnapshot] = {}
    for item in eligible:
        current = latest.get(item.period_end)
        if current is None or (
            item.published_at or date.min,
            _statement_completeness(item),
        ) > (
            current.published_at or date.min,
            _statement_completeness(current),
        ):
            latest[item.period_end] = item
    return [latest[key] for key in sorted(latest)]


def _statement_completeness(item: FinancialStatementSnapshot) -> int:
    return sum(
        getattr(item, name, None) is not None
        for name in (
            "revenue",
            "net_income_parent",
            "free_cash_flow",
            "operating_cash_flow",
            "capital_expenditures",
            "reported_roe",
            "average_parent_equity",
        )
    )


def _plausible_fcf_history(
    annuals: Iterable[FinancialStatementSnapshot],
    *,
    minimum_fcf_to_net_income: float,
    maximum_fcf_to_net_income: float,
    maximum_fcf_to_revenue: float,
) -> list[tuple[date, float]]:
    """Return annual FCFE proxies that reconcile within their own filing."""

    fcf: list[tuple[date, float]] = []
    for item in annuals:
        free_cash_flow = _per_share(item, "free_cash_flow")
        net_income = _per_share(item, "net_income_parent")
        revenue_per_share = _per_share(item, "revenue")
        if (
            free_cash_flow is None
            or net_income is None
            or revenue_per_share is None
            or free_cash_flow <= 0
            or net_income <= 0
            or revenue_per_share <= 0
        ):
            continue
        cash_conversion = free_cash_flow / net_income
        cash_margin = free_cash_flow / revenue_per_share
        if (
            cash_conversion < minimum_fcf_to_net_income
            or cash_conversion > maximum_fcf_to_net_income
            or cash_margin > maximum_fcf_to_revenue
        ):
            continue
        fcf.append((item.period_end, float(free_cash_flow)))
    return fcf


def _reported_or_derived_roe(item: FinancialStatementSnapshot) -> float | None:
    roe = _finite(item.reported_roe)
    if roe is not None:
        roe = roe / 100.0 if abs(roe) > 1.0 else roe
    if roe is None:
        income = _finite(item.net_income_parent)
        equity = _finite(item.average_parent_equity)
        if income is not None and equity is not None and equity > 0:
            roe = income / equity
    return float(roe) if roe is not None and np.isfinite(roe) else None


def financial_equity_inputs_from_statements(
    statements: Iterable[FinancialStatementSnapshot],
    bundle: PriceHistoryBundle,
    evaluation_date: date,
    *,
    earnings_growth: float | None,
) -> FinancialEquityInputs:
    """Derive book, normalized ROE, and observed/derived payout causally."""

    annuals = _annual_statements(statements, evaluation_date)
    book_values = [
        value
        for item in annuals
        if (
            value := (
                _finite(item.book_value_per_share)
                or _per_share(item, "parent_equity")
            )
        ) is not None
        and value > 0.0
    ]
    roe_values = [
        value
        for item in annuals
        if (value := _reported_or_derived_roe(item)) is not None
        and -0.25 <= value <= 0.50
    ]
    book = book_values[-1] if book_values else None
    roe = float(np.median(roe_values[-3:])) if roe_values else None
    latest_earnings = next(
        (
            value
            for item in reversed(annuals)
            if (value := _per_share(item, "net_income_parent")) is not None
            and value > 0.0
        ),
        None,
    )
    trailing_start = evaluation_date - pd.Timedelta(days=365).to_pytimedelta()
    dividend = sum(
        float(action.cash_per_share)
        for action in bundle.actions
        if action.cash_per_share is not None
        and trailing_start < action.ex_date <= evaluation_date
        and (action.published_at is None or action.published_at <= evaluation_date)
    )
    if latest_earnings is not None and latest_earnings > 0.0 and dividend > 0.0:
        payout = float(np.clip(dividend / latest_earnings, 0.0, 1.0))
        source = "trailing_disclosed_cash_dividends"
    elif roe is not None and roe > 0.0 and earnings_growth is not None:
        retention = float(np.clip(max(earnings_growth, 0.0) / roe, 0.0, 0.85))
        payout = 1.0 - retention
        source = "earnings_growth_over_roe"
    else:
        payout = None
        source = None
    return FinancialEquityInputs(
        book_value_per_share=book,
        roe=roe,
        payout_ratio=payout,
        payout_source=source,
    )


def growth_inputs_from_statements(
    statements: Iterable[FinancialStatementSnapshot],
    evaluation_date: date,
    *,
    minimum_fcf_to_net_income: float = 0.05,
    maximum_fcf_to_net_income: float = 2.50,
    maximum_fcf_to_revenue: float = 0.80,
    minimum_plausible_fcf_years: int = 2,
) -> tuple[GrowthInputs, float | None, int | None]:
    """Derive growth inputs and a plausibility-screened FCF/share basis.

    A PDF extraction error can turn a cash-flow-table line item into operating
    cash flow.  Before an FCFE proxy is allowed to set an entry price, it must
    reconcile to the same annual report's positive earnings and revenue.  The
    screen rejects, rather than clips, suspect observations: DCF does not get
    to manufacture a lower or higher cash flow after the fact.
    """

    annuals = _annual_statements(statements, evaluation_date)
    revenue = [
        (item.period_end, value)
        for item in annuals
        if (value := _per_share(item, "revenue")) is not None
    ]
    earnings = [
        (item.period_end, value)
        for item in annuals
        if (value := _per_share(item, "net_income_parent")) is not None
    ]
    fcf = _plausible_fcf_history(
        annuals,
        minimum_fcf_to_net_income=minimum_fcf_to_net_income,
        maximum_fcf_to_net_income=maximum_fcf_to_net_income,
        maximum_fcf_to_revenue=maximum_fcf_to_revenue,
    )
    roe_values: list[tuple[date, float]] = []
    reinvestment: list[float] = []
    for item in annuals:
        roe = _reported_or_derived_roe(item)
        if roe is not None:
            roe_values.append((item.period_end, float(roe)))
        capex = _finite(item.capital_expenditures)
        ocf = _finite(item.operating_cash_flow)
        if (
            roe is not None
            and capex is not None
            and ocf is not None
            and capex >= 0
            and ocf > 0
        ):
            retention_proxy = float(np.clip(capex / ocf, 0.0, 0.85))
            reinvestment.append(float(np.clip(roe * retention_proxy, -0.10, 0.25)))
    roe_trend = None
    if len(roe_values) >= 2:
        first, last = roe_values[max(0, len(roe_values) - 4)], roe_values[-1]
        years = (last[0] - first[0]).days / 365.25
        if years > 0:
            roe_trend = float(np.clip((last[1] - first[1]) / years, -0.10, 0.10))
    latest_fcf_date = fcf[-1][0] if fcf else None
    recent_positive_fcf = [value for _, value in fcf[-3:]]
    cash_reference = (
        float(np.median(recent_positive_fcf))
        if len(recent_positive_fcf) >= minimum_plausible_fcf_years
        else None
    )
    age = (
        (evaluation_date - latest_fcf_date).days
        if latest_fcf_date is not None
        else None
    )
    return (
        GrowthInputs(
            revenue_cagr=_cagr(revenue),
            earnings_cagr=_cagr(earnings),
            fcf_cagr=_cagr(fcf),
            roe_reinvestment=(float(np.median(reinvestment)) if reinvestment else None),
            roe_trend=roe_trend,
            annual_observation_count=len(annuals),
            fcf_observation_count=len(fcf),
        ),
        cash_reference,
        age,
    )


def financial_growth(
    inputs: GrowthInputs,
    parameters: CapmDcfEntryParameters,
) -> tuple[float | None, dict[str, float]]:
    """Return a constrained financial-growth forecast and audit components."""

    weights = parameters.growth_weights
    values = inputs.values()
    weight_map = {
        "revenue_cagr": weights.revenue_cagr,
        "earnings_cagr": weights.earnings_cagr,
        "fcf_cagr": weights.fcf_cagr,
        "roe_reinvestment": weights.roe_reinvestment,
        "roe_trend": weights.roe_trend,
    }
    selected = {
        name: value
        for name, value in values.items()
        if value is not None and np.isfinite(value) and weight_map[name] > 0
    }
    if len(selected) < 2:
        return None, {}
    denominator = sum(weight_map[name] for name in selected)
    if denominator <= 0:
        return None, {}
    raw = sum(weight_map[name] * value for name, value in selected.items())
    raw /= denominator
    growth = float(np.clip(raw, parameters.growth_floor, parameters.growth_cap))
    detail = {name: float(value) for name, value in selected.items()}
    detail["weighted_raw_growth"] = float(raw)
    detail["financial_growth"] = growth
    return growth, detail


def risk_adjusted_growth(
    financial_growth_rate: float,
    equity_risk_premium: float,
    config: CapmDcfEntryConfig,
) -> tuple[float, float]:
    """Apply the user-specified ERP-dependent subjective growth haircut."""

    multiplier = (
        config.erp_growth_multiplier_high
        if equity_risk_premium > config.erp_growth_threshold
        else config.erp_growth_multiplier_low
    )
    if multiplier <= 0:
        raise ValueError("ERP growth multiplier must be positive")
    return float(financial_growth_rate / multiplier), float(multiplier)


def equity_dcf_per_share(
    cash_per_share: float,
    explicit_growth: float,
    cost_of_equity: float,
    terminal_growth: float,
    projection_years: int,
    minimum_discount_spread: float,
) -> float | None:
    """Discount an FCFE proxy with a fixed terminal growth rate."""

    if (
        not np.isfinite(cash_per_share)
        or cash_per_share <= 0
        or not np.isfinite(cost_of_equity)
        or cost_of_equity <= terminal_growth + minimum_discount_spread
    ):
        return None
    cash = float(cash_per_share)
    value = 0.0
    for year in range(1, projection_years + 1):
        cash *= 1.0 + explicit_growth
        value += cash / (1.0 + cost_of_equity) ** year
    terminal_cash = cash * (1.0 + terminal_growth)
    terminal_value = terminal_cash / (cost_of_equity - terminal_growth)
    value += terminal_value / (1.0 + cost_of_equity) ** projection_years
    return float(value) if np.isfinite(value) and value > 0 else None


def _normalized_future_path(
    bundle: PriceHistoryBundle,
    market_index: int,
    required_days: int,
) -> tuple[np.ndarray, np.ndarray, bool] | None:
    """Return future raw low/close paths normalized for later share actions."""

    prices = bundle.prices
    # ``required_days`` means executable trading sessions, not source rows.
    # A suspended security can have a carried-forward quote row marked
    # untradable.  Slicing first and filtering afterwards turns ten suspended
    # rows into a silent ten-session label shortage even when later valid
    # sessions exist.  Select all causally future rows, discard untradable
    # rows, then require exactly the requested number of real sessions.
    selected = prices.iloc[market_index + 1 :]
    selected = selected[selected["tradable"].astype(bool)]
    if len(selected) < required_days:
        return None
    selected = selected.iloc[:required_days]
    lows = pd.to_numeric(selected["raw_low"], errors="coerce").to_numpy(float)
    closes = pd.to_numeric(selected["raw_close"], errors="coerce").to_numpy(float)
    if (
        len(lows) != required_days
        or not np.isfinite(lows).all()
        or not np.isfinite(closes).all()
        or (lows <= 0).any()
        or (closes <= 0).any()
    ):
        return None
    market_date = pd.Timestamp(prices.iloc[market_index]["date"]).date()
    dates = pd.to_datetime(selected["date"]).dt.date.to_numpy()
    multiplier = np.ones(required_days, dtype=np.float64)
    action_adjusted = False
    for action in bundle.actions:
        if (
            action.share_multiplier is None
            or action.share_multiplier <= 0
            or action.ex_date <= market_date
        ):
            continue
        multiplier[dates >= action.ex_date] *= float(action.share_multiplier)
        action_adjusted = True
    return lows * multiplier, closes * multiplier, action_adjusted


def _market_index(bundle: PriceHistoryBundle, evaluation_date: date) -> int | None:
    dates = pd.to_datetime(bundle.prices["date"]).dt.date.to_numpy()
    index = int(np.searchsorted(dates, evaluation_date, side="right") - 1)
    if index < 0:
        return None
    market_date = dates[index]
    if (evaluation_date - market_date).days > 10:
        return None
    if not bool(bundle.prices.iloc[index]["tradable"]):
        return None
    return index


def _valuation_financial_age(
    valuation_model: ValuationModel,
    fcf_age: int | None,
    statement_age: int | None,
) -> int | None:
    """Return the age of the data actually consumed by a valuation route."""

    if valuation_model == ValuationModel.RESIDUAL_INCOME:
        # Residual income never consumes FCFE.  A stale or missing cash-flow
        # line must not reject fresh book value, ROE and payout disclosures.
        return statement_age
    return fcf_age if fcf_age is not None else statement_age


def _wilson_lower_bound(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = proportion + z * z / (2.0 * total)
    spread = z * sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
    )
    return float((center - spread) / denominator)


class CapmDcfEntryCalibrator:
    """Build annual point-in-time episodes and select a DCF entry profile."""

    def __init__(
        self,
        data_root: str | Path,
        benchmark_bundle: PriceHistoryBundle,
        risk_free_rates: Mapping[date, float],
        *,
        config: CapmDcfEntryConfig | None = None,
        market: str = "a_share",
        industry_history: IndustryClassificationHistoryStore | None = None,
    ):
        self.root = Path(data_root)
        self.market_store_root = self.root / "market"
        self.fundamental_store = PointInTimeFundamentalStore(self.root)
        self.benchmark_bundle = benchmark_bundle
        self.risk_free_rates = {
            key: float(value) for key, value in risk_free_rates.items()
        }
        self.config = config or CapmDcfEntryConfig()
        self.market = market
        self.industry_history = industry_history

    def available_symbols(self, symbols: Iterable[str] | None = None) -> list[str]:
        requested = {str(item) for item in symbols or ()}
        market_codes = {path.stem for path in self.market_store_root.glob("*.csv")}
        statement_codes = {
            path.name.split(".statements.json")[0]
            for path in (self.root / "fundamentals").glob("*.statements.json")
        }
        result = sorted(market_codes & statement_codes)
        return [item for item in result if not requested or item in requested]

    def evaluation_dates(self) -> list[date]:
        """Use one common quarter-end calendar, not report-date sampling.

        Individual announcements cluster on different days and would otherwise
        silently weight frequently revised companies more heavily.  The latest
        disclosure available at each common anchor is selected point in time.
        """

        dates = pd.to_datetime(self.benchmark_bundle.prices["date"])
        frame = pd.DataFrame({"date": dates.dropna()})
        frame["quarter"] = frame["date"].dt.to_period("Q")
        return [
            item.date() for item in frame.groupby("quarter", sort=True)["date"].max()
        ]

    def required_risk_free_dates(
        self, symbols: Iterable[str] | None = None
    ) -> list[date]:
        del symbols
        horizon = (
            self.config.hit_horizon_trading_days
            + self.config.outcome_horizon_trading_days
        )
        price_dates = pd.to_datetime(
            self.benchmark_bundle.prices["date"]
        ).dt.date.to_numpy()
        return [
            item
            for item in self.evaluation_dates()
            if int(np.searchsorted(price_dates, item, side="right") - 1) + horizon
            < len(price_dates)
        ]

    def _bundle(self, symbol: str) -> PriceHistoryBundle | None:
        from src.data.market_history import PointInTimeMarketStore

        return PointInTimeMarketStore(self.root).read(symbol)

    def _episode(
        self,
        symbol: str,
        bundle: PriceHistoryBundle,
        evaluation_date: date,
        industry_label: Any | None,
        *,
        require_future_labels: bool = True,
    ) -> tuple[DcfEntryEpisode | None, str | None]:
        risk_free_rate = self.risk_free_rates.get(evaluation_date)
        if risk_free_rate is None:
            return None, "risk_free_rate_missing"
        index = _market_index(bundle, evaluation_date)
        if index is None:
            return None, "market_row_missing_or_stale"
        market_date = pd.Timestamp(bundle.prices.iloc[index]["date"]).date()
        current_price = _finite(bundle.prices.iloc[index]["raw_close"])
        if current_price is None or current_price <= 0:
            return None, "current_market_price_missing"
        # A filing is first selected point-in-time, then its historical share
        # count is rolled forward through only actions disclosed by this date.
        # Without this denominator adjustment, a pre-split FCF/share is
        # compared with a post-split market price and can make a DCF entry
        # level spuriously expensive by the split multiplier.
        as_of_statements = self.fundamental_store.as_of(symbol, evaluation_date)
        adjusted_statements = adjust_statement_shares(
            as_of_statements, bundle.actions, evaluation_date
        )
        growth_inputs, cash_per_share, age = growth_inputs_from_statements(
            adjusted_statements,
            evaluation_date,
            minimum_fcf_to_net_income=self.config.minimum_fcf_to_net_income,
            maximum_fcf_to_net_income=self.config.maximum_fcf_to_net_income,
            maximum_fcf_to_revenue=self.config.maximum_fcf_to_revenue,
            minimum_plausible_fcf_years=self.config.minimum_plausible_fcf_years,
        )
        annuals = _annual_statements(adjusted_statements, evaluation_date)
        fcf_history = _plausible_fcf_history(
            annuals,
            minimum_fcf_to_net_income=self.config.minimum_fcf_to_net_income,
            maximum_fcf_to_net_income=self.config.maximum_fcf_to_net_income,
            maximum_fcf_to_revenue=self.config.maximum_fcf_to_revenue,
        )
        cash_history = summarize_cash_flow_history(
            (value for _, value in fcf_history),
            window=self.config.normalized_cash_flow_years,
        )
        industry_code = (
            str(industry_label.industry_code) if industry_label is not None else None
        )
        industry_name = (
            str(industry_label.industry_name) if industry_label is not None else None
        )
        route = route_valuation_model(
            industry_code=industry_code,
            industry_name=industry_name,
            cash_flow_history=cash_history,
            cyclical_industry_prefixes=self.config.cyclical_industry_prefixes,
            peak_ratio_threshold=self.config.cyclical_peak_ratio_threshold,
            cash_flow_cv_threshold=self.config.cyclical_cash_flow_cv_threshold,
        )
        financial_inputs = financial_equity_inputs_from_statements(
            adjusted_statements,
            bundle,
            evaluation_date,
            earnings_growth=growth_inputs.earnings_cagr,
        )
        statement_age = (
            (evaluation_date - annuals[-1].period_end).days if annuals else None
        )
        effective_age = _valuation_financial_age(
            route.model, age, statement_age
        )
        if route.model == ValuationModel.RESIDUAL_INCOME:
            if (
                financial_inputs.book_value_per_share is None
                or financial_inputs.roe is None
                or financial_inputs.payout_ratio is None
            ):
                return None, "residual_income_inputs_missing"
            if effective_age is None or effective_age > self.config.max_financial_age_days:
                return None, "residual_income_financial_data_stale"
        else:
            if cash_per_share is None or cash_per_share <= 0:
                return None, "plausible_positive_annual_fcf_per_share_missing"
            if effective_age is None or effective_age > self.config.max_financial_age_days:
                return None, "fcf_financial_data_stale"
            if (
                route.model == ValuationModel.NORMALIZED_FCFE_DCF
                and cash_history.normalized_cash_per_share is None
            ):
                return None, "normalized_cash_flow_history_insufficient"
        beta_estimate = estimate_robust_beta(bundle, self.benchmark_bundle, market_date)
        beta = beta_estimate.beta
        if beta is None or not np.isfinite(beta):
            return None, "point_in_time_beta_unavailable"
        if require_future_labels:
            required_days = (
                self.config.hit_horizon_trading_days
                + self.config.outcome_horizon_trading_days
            )
            path = _normalized_future_path(bundle, index, required_days)
            if path is None:
                return None, "future_price_label_incomplete"
            lows, closes, action_adjusted = path
        else:
            # A live/frozen policy evaluation must use precisely the same
            # point-in-time valuation inputs but has no legitimate future
            # label.  Empty arrays preserve the DcfEntryEpisode contract while
            # making any accidental label inspection evaluate to no hit.
            lows = np.asarray([], dtype=np.float64)
            closes = np.asarray([], dtype=np.float64)
            action_adjusted = False
        return (
            DcfEntryEpisode(
                symbol=symbol,
                evaluation_date=evaluation_date,
                market_date=market_date,
                current_price=current_price,
                cash_per_share=(
                    float(cash_per_share) if cash_per_share is not None else None
                ),
                growth_inputs=growth_inputs,
                risk_free_rate=float(risk_free_rate),
                beta=float(beta),
                beta_observations=sum(
                    item.observations for item in beta_estimate.components
                ),
                future_lows=lows,
                future_closes=closes,
                financial_age_days=int(effective_age),
                action_adjusted=action_adjusted,
                valuation_model=route.model,
                valuation_route_reason=route.reason,
                industry_code=route.industry_code,
                industry_name=route.industry_name,
                normalized_cash_per_share=cash_history.normalized_cash_per_share,
                cash_flow_peak_ratio=cash_history.latest_to_normalized,
                cash_flow_coefficient_of_variation=(
                    cash_history.coefficient_of_variation
                ),
                book_value_per_share=financial_inputs.book_value_per_share,
                normalized_roe=financial_inputs.roe,
                payout_ratio=financial_inputs.payout_ratio,
                payout_source=financial_inputs.payout_source,
            ),
            None,
        )

    def build_episodes(
        self,
        symbols: Iterable[str] | None = None,
    ) -> tuple[list[DcfEntryEpisode], dict[str, int]]:
        episodes: list[DcfEntryEpisode] = []
        skipped: dict[str, int] = {}
        evaluation_dates = self.evaluation_dates()
        labels_by_date = {
            evaluation_date: (
                self.industry_history.labels_as_of(evaluation_date)
                if self.industry_history is not None
                else {}
            )
            for evaluation_date in evaluation_dates
        }
        for symbol in self.available_symbols(symbols):
            bundle = self._bundle(symbol)
            if bundle is None:
                skipped["market_bundle_missing"] = (
                    skipped.get("market_bundle_missing", 0) + 1
                )
                continue
            for evaluation_date in evaluation_dates:
                episode, reason = self._episode(
                    symbol,
                    bundle,
                    evaluation_date,
                    labels_by_date[evaluation_date].get(symbol),
                    require_future_labels=True,
                )
                if episode is None:
                    if reason is not None:
                        skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                episodes.append(episode)
        return sorted(
            episodes, key=lambda item: (item.evaluation_date, item.symbol)
        ), skipped

    def build_valuation_snapshots(
        self,
        symbols: Iterable[str] | None = None,
    ) -> tuple[list[DcfEntryEpisode], dict[str, int]]:
        """Build causal valuation snapshots without future-price labels.

        This is deliberately separate from :meth:`build_episodes`: calibration
        needs completed one-year price paths, whereas a live value strategy
        must be able to value the latest available report without looking into
        its future.  Both paths share all accounting, split adjustment, route,
        risk-free-rate, and point-in-time beta logic.
        """

        snapshots: list[DcfEntryEpisode] = []
        skipped: dict[str, int] = {}
        evaluation_dates = self.evaluation_dates()
        labels_by_date = {
            evaluation_date: (
                self.industry_history.labels_as_of(evaluation_date)
                if self.industry_history is not None
                else {}
            )
            for evaluation_date in evaluation_dates
        }
        for symbol in self.available_symbols(symbols):
            bundle = self._bundle(symbol)
            if bundle is None:
                skipped["market_bundle_missing"] = (
                    skipped.get("market_bundle_missing", 0) + 1
                )
                continue
            for evaluation_date in evaluation_dates:
                episode, reason = self._episode(
                    symbol,
                    bundle,
                    evaluation_date,
                    labels_by_date[evaluation_date].get(symbol),
                    require_future_labels=False,
                )
                if episode is None:
                    if reason is not None:
                        skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                snapshots.append(episode)
        return sorted(
            snapshots, key=lambda item: (item.evaluation_date, item.symbol)
        ), skipped

    @staticmethod
    def _training_beta_reference(
        episodes: Iterable[DcfEntryEpisode],
    ) -> dict[str, object]:
        """Return an auditable broad-universe beta reference from train data.

        The reference is a descriptive, equal-episode mean that is available
        before the holdout begins.  It is used only to choose a portfolio-level
        safety margin, never to reprice an individual company's CAPM cost of
        equity (which already uses its own beta).
        """

        values = np.asarray(
            [item.beta for item in episodes if np.isfinite(item.beta) and item.beta > 0],
            dtype=np.float64,
        )
        if not len(values):
            raise ValueError("training episodes have no finite positive beta")
        return {
            "method": "equal_episode_mean_point_in_time_beta",
            "value": float(np.mean(values)),
            "median": float(np.median(values)),
            "count": int(len(values)),
        }

    def _evaluate_episode(
        self,
        episode: DcfEntryEpisode,
        parameters: CapmDcfEntryParameters
        | Mapping[str, CapmDcfEntryParameters],
        equity_risk_premium: float,
    ) -> dict[str, Any]:
        if isinstance(parameters, CapmDcfEntryParameters):
            selected_parameters = parameters
        else:
            selected_parameters = parameters.get(episode.valuation_model.value)
            if selected_parameters is None:
                # A route with insufficient pre-holdout evidence must produce
                # no trade, rather than borrow a different model's parameters
                # or cause the whole calibration to silently skip it.
                return {
                    "symbol": episode.symbol,
                    "evaluation_date": episode.evaluation_date.isoformat(),
                    "market_date": episode.market_date.isoformat(),
                    "growth_profile": None,
                    "risk_free_rate": episode.risk_free_rate,
                    "equity_risk_premium": equity_risk_premium,
                    "beta": episode.beta,
                    "beta_observations": episode.beta_observations,
                    "cash_per_share": episode.cash_per_share,
                    "normalized_cash_per_share": episode.normalized_cash_per_share,
                    "cash_flow_peak_ratio": episode.cash_flow_peak_ratio,
                    "cash_flow_coefficient_of_variation": (
                        episode.cash_flow_coefficient_of_variation
                    ),
                    "valuation_model": episode.valuation_model.value,
                    "valuation_route_reason": episode.valuation_route_reason,
                    "industry_code": episode.industry_code,
                    "industry_name": episode.industry_name,
                    "book_value_per_share": episode.book_value_per_share,
                    "normalized_roe": episode.normalized_roe,
                    "payout_ratio": episode.payout_ratio,
                    "payout_source": episode.payout_source,
                    "current_price": episode.current_price,
                    "financial_age_days": episode.financial_age_days,
                    "action_adjusted_price_label": episode.action_adjusted,
                    "eligible": False,
                    "reason": "valuation_route_not_supported_by_training_gate",
                    "valuation_route_supported": False,
                }
        base = {
            "symbol": episode.symbol,
            "evaluation_date": episode.evaluation_date.isoformat(),
            "market_date": episode.market_date.isoformat(),
            "growth_profile": selected_parameters.growth_weights.name,
            "risk_free_rate": episode.risk_free_rate,
            "equity_risk_premium": equity_risk_premium,
            "beta": episode.beta,
            "beta_observations": episode.beta_observations,
            "cash_per_share": episode.cash_per_share,
            "normalized_cash_per_share": episode.normalized_cash_per_share,
            "cash_flow_peak_ratio": episode.cash_flow_peak_ratio,
            "cash_flow_coefficient_of_variation": (
                episode.cash_flow_coefficient_of_variation
            ),
            "valuation_model": episode.valuation_model.value,
            "valuation_route_reason": episode.valuation_route_reason,
            "industry_code": episode.industry_code,
            "industry_name": episode.industry_name,
            "book_value_per_share": episode.book_value_per_share,
            "normalized_roe": episode.normalized_roe,
            "payout_ratio": episode.payout_ratio,
            "payout_source": episode.payout_source,
            "current_price": episode.current_price,
            "financial_age_days": episode.financial_age_days,
            "action_adjusted_price_label": episode.action_adjusted,
            "valuation_route_supported": True,
        }
        capm = CapmInputs(
            evaluation_date=episode.evaluation_date,
            risk_free_rate=episode.risk_free_rate,
            equity_risk_premium=equity_risk_premium,
            beta=episode.beta,
            beta_observations=episode.beta_observations,
        )
        if episode.valuation_model == ValuationModel.RESIDUAL_INCOME:
            fair_value = residual_income_per_share(
                book_value_per_share=episode.book_value_per_share or float("nan"),
                roe=episode.normalized_roe or float("nan"),
                payout_ratio=episode.payout_ratio or float("nan"),
                cost_of_equity=capm.cost_of_equity,
                terminal_growth=self.config.terminal_growth,
                projection_years=self.config.projection_years,
                roe_fade=self.config.residual_income_roe_fade,
                minimum_discount_spread=self.config.minimum_discount_spread,
            )
            growth = None
            adjusted_growth = None
            multiplier = None
            detail: dict[str, Any] = {
                "method": "residual_income",
                "roe_fade": self.config.residual_income_roe_fade,
            }
            cash_basis = "book_value_and_normalized_roe"
        else:
            # A normalized FCFE model uses a through-cycle cash base.  Its
            # volatile FCF CAGR is deliberately excluded from the five-year
            # growth mix; otherwise a single cyclical peak re-enters through
            # the growth term after being removed from the cash denominator.
            growth_inputs = episode.growth_inputs
            if episode.valuation_model == ValuationModel.NORMALIZED_FCFE_DCF:
                growth_inputs = replace(growth_inputs, fcf_cagr=None)
            growth, detail = financial_growth(growth_inputs, selected_parameters)
            if growth is None:
                return {
                    **base,
                    "eligible": False,
                    "reason": "growth_inputs_missing",
                    "growth_components": detail,
                }
            adjusted_growth, multiplier = risk_adjusted_growth(
                growth, equity_risk_premium, self.config
            )
            cash_per_share = (
                episode.normalized_cash_per_share
                if episode.valuation_model == ValuationModel.NORMALIZED_FCFE_DCF
                else episode.cash_per_share
            )
            fair_value = equity_dcf_per_share(
                cash_per_share or float("nan"),
                adjusted_growth,
                capm.cost_of_equity,
                self.config.terminal_growth,
                self.config.projection_years,
                self.config.minimum_discount_spread,
            )
            cash_basis = (
                "trailing_median_plausible_fcfe"
                if episode.valuation_model == ValuationModel.NORMALIZED_FCFE_DCF
                else "recent_plausible_fcfe"
            )
        if fair_value is None:
            return {
                **base,
                "financial_growth": growth,
                "risk_adjusted_growth": adjusted_growth,
                "growth_risk_multiplier": multiplier,
                "cost_of_equity": capm.cost_of_equity,
                "cash_flow_basis": cash_basis,
                "growth_components": detail,
                "eligible": False,
                "reason": "valuation_inputs_or_discount_rate_invalid",
            }
        buy_price = fair_value * selected_parameters.entry_fair_value_fraction
        entry_price_gate_pass = bool(
            buy_price / episode.current_price
            <= self.config.maximum_entry_to_current_price
        )
        hits = (
            np.flatnonzero(
                episode.future_lows[: self.config.hit_horizon_trading_days]
                <= buy_price
            )
            if entry_price_gate_pass
            else np.asarray([], dtype=int)
        )
        hit_index = int(hits[0]) if len(hits) else None
        after_hit = (
            episode.future_closes[
                hit_index + 1 : hit_index + 1 + self.config.outcome_horizon_trading_days
            ]
            if hit_index is not None
            else np.asarray([], dtype=np.float64)
        )
        post_entry_above_rate = (
            float(np.mean(after_hit > buy_price)) if len(after_hit) else None
        )
        success = (
            post_entry_above_rate is not None
            and post_entry_above_rate >= self.config.success_above_entry_fraction
        )
        return {
            **base,
            "financial_growth": growth,
            "risk_adjusted_growth": adjusted_growth,
            "growth_risk_multiplier": multiplier,
            "cost_of_equity": capm.cost_of_equity,
            "cash_flow_basis": cash_basis,
            "growth_components": detail,
            "fair_value": fair_value,
            "buy_price": buy_price,
            "buy_price_to_current_price": buy_price / episode.current_price,
            "entry_price_gate_limit": self.config.maximum_entry_to_current_price,
            "entry_price_gate_pass": entry_price_gate_pass,
            "entry_fair_value_fraction": (
                selected_parameters.entry_fair_value_fraction
            ),
            "eligible": True,
            "hit_within_one_year": hit_index is not None,
            "entry_trading_day": hit_index + 1 if hit_index is not None else None,
            "post_entry_above_rate": post_entry_above_rate,
            "success": bool(success) if hit_index is not None else None,
        }

    def evaluate(
        self,
        episodes: Iterable[DcfEntryEpisode],
        parameters: CapmDcfEntryParameters
        | Mapping[str, CapmDcfEntryParameters],
        equity_risk_premium: float,
        *,
        include_rows: bool = False,
    ) -> dict[str, Any]:
        rows = [
            self._evaluate_episode(item, parameters, equity_risk_premium)
            for item in episodes
        ]
        eligible = [item for item in rows if item["eligible"]]
        hits = [item for item in eligible if item["hit_within_one_year"]]
        successful = [item for item in hits if item["success"]]
        hit_count = len(hits)
        success_count = len(successful)
        models = sorted({item["valuation_model"] for item in rows})
        by_valuation_model: dict[str, dict[str, int | float | None]] = {}
        for model in models:
            model_rows = [item for item in rows if item["valuation_model"] == model]
            model_eligible = [item for item in model_rows if item["eligible"]]
            model_hits = [
                item for item in model_eligible if item["hit_within_one_year"]
            ]
            model_successful = [item for item in model_hits if item["success"]]
            by_valuation_model[model] = {
                "episode_count": len(model_rows),
                "eligible_count": len(model_eligible),
                "entry_price_gate_rejected_count": sum(
                    item.get("entry_price_gate_pass") is False
                    for item in model_eligible
                ),
                "route_not_supported_count": sum(
                    item.get("reason") == "valuation_route_not_supported_by_training_gate"
                    for item in model_rows
                ),
                "hit_count": len(model_hits),
                "hit_rate": (
                    len(model_hits) / len(model_eligible)
                    if model_eligible
                    else None
                ),
                "success_count": len(model_successful),
                "post_entry_success_rate": (
                    len(model_successful) / len(model_hits)
                    if model_hits
                    else None
                ),
            }
        result: dict[str, Any] = {
            "scenario_erp": equity_risk_premium,
            "eligible_count": len(eligible),
            "hit_count": hit_count,
            "hit_rate": len(hits) / len(eligible) if eligible else None,
            "success_count": success_count,
            "post_entry_success_rate": (
                success_count / hit_count if hit_count else None
            ),
            "post_entry_success_wilson_lower_95": _wilson_lower_bound(
                success_count, hit_count
            ),
            "mean_post_entry_above_rate": (
                float(np.mean([item["post_entry_above_rate"] for item in hits]))
                if hits
                else None
            ),
            "unhit_count": len(eligible) - len(hits),
            "ineligible_count": len(rows) - len(eligible),
            "by_valuation_model": by_valuation_model,
        }
        if include_rows:
            result["rows"] = rows
        return result

    def _metrics_key(
        self,
        metrics_by_erp: Mapping[float, dict[str, Any]],
    ) -> tuple[float, float, float, float, int]:
        metrics = list(metrics_by_erp.values())
        feasible = all(
            item["hit_count"] >= self.config.minimum_hit_count
            and item["hit_rate"] is not None
            and item["hit_rate"] >= self.config.minimum_hit_rate
            and item["post_entry_success_rate"] is not None
            and item["post_entry_success_rate"] >= self.config.target_success_rate
            and item["post_entry_success_wilson_lower_95"] is not None
            and item["post_entry_success_wilson_lower_95"]
            >= self.config.minimum_success_wilson_lower_95
            for item in metrics
        )
        # Every declared ERP scenario is a stress case.  Treating a no-hit
        # scenario as absent used to let a 15%-of-fair-value order outrank a
        # usable order because only the surviving scenario contributed a high
        # Wilson score.  Missing evidence is a failure, not a neutral value.
        lower_bounds = [
            (
                float(item["post_entry_success_wilson_lower_95"])
                if item["post_entry_success_wilson_lower_95"] is not None
                else -1.0
            )
            for item in metrics
        ]
        success_rates = [
            (
                float(item["post_entry_success_rate"])
                if item["post_entry_success_rate"] is not None
                else -1.0
            )
            for item in metrics
        ]
        hit_rates = [
            float(item["hit_rate"]) if item["hit_rate"] is not None else -1.0
            for item in metrics
        ]
        hits = [item["hit_count"] for item in metrics]
        if feasible:
            return (
                1.0,
                min(lower_bounds),
                min(success_rates),
                min(hit_rates),
                min(hits) if hits else 0,
            )
        # A failed gate must never turn into a coverage-only objective.  This
        # fallback makes the closest-to-safe candidate visible for diagnosis,
        # while the explicit pass bit still prevents deployment.
        return (
            0.0,
            min(lower_bounds),
            min(success_rates),
            min(hit_rates),
            min(hits) if hits else 0,
        )

    @staticmethod
    def _split_episodes(
        episodes: list[DcfEntryEpisode], train_fraction: float
    ) -> tuple[list[DcfEntryEpisode], list[DcfEntryEpisode], str | None]:
        dates = sorted({item.evaluation_date for item in episodes})
        if len(dates) < 2:
            return episodes, [], None
        train_count = max(1, int(np.floor(len(dates) * train_fraction)))
        train_count = min(train_count, len(dates) - 1)
        train_dates = set(dates[:train_count])
        test_dates = set(dates[train_count:])
        return (
            [item for item in episodes if item.evaluation_date in train_dates],
            [item for item in episodes if item.evaluation_date in test_dates],
            min(test_dates).isoformat(),
        )

    def select(
        self,
        train_episodes: list[DcfEntryEpisode],
        erp_scenarios: Iterable[float],
    ) -> tuple[
        CapmDcfEntryParameters,
        dict[float, dict[str, Any]],
        list[dict[str, Any]],
    ]:
        scenarios = tuple(float(item) for item in erp_scenarios)
        if not scenarios:
            raise ValueError("at least one ERP scenario is required")
        selected_parameters = None
        selected_metrics = None
        selected_key = None
        leaderboard: list[dict[str, Any]] = []
        for parameters in self.config.parameter_grid():
            metrics = {
                erp: self.evaluate(train_episodes, parameters, erp) for erp in scenarios
            }
            key = self._metrics_key(metrics)
            leaderboard.append(
                {
                    "parameters": parameters.to_dict(),
                    "selection_key": list(key),
                    "gate_passes_all_erp_scenarios": bool(key[0]),
                    "metrics": metrics,
                }
            )
            if selected_key is None or key > selected_key:
                selected_parameters = parameters
                selected_metrics = metrics
                selected_key = key
        if selected_parameters is None or selected_metrics is None:
            raise ValueError("no DCF parameter candidates were generated")
        leaderboard.sort(key=lambda item: tuple(item["selection_key"]), reverse=True)
        # The entire finite grid is intentionally retained: a rejected
        # calibration must be auditable, not hidden behind only its top rows.
        return selected_parameters, selected_metrics, leaderboard

    def select_by_valuation_model(
        self,
        train_episodes: list[DcfEntryEpisode],
        erp_scenarios: Iterable[float],
    ) -> tuple[
        dict[str, CapmDcfEntryParameters],
        dict[float, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        """Select a causal policy per economically distinct valuation model.

        A financial residual-income entry fraction is not a parameter of a
        cyclical FCFE model.  Choosing a single fraction across the two would
        make the model with the smallest number of fortunate historical
        entries dominate a fallback ranking.  Each policy sees only its own
        training episodes; the merged policy is then evaluated on the entire
        training/validation set under exactly the same aggregate safety gate.
        """

        policy: dict[str, CapmDcfEntryParameters] = {}
        details: dict[str, dict[str, Any]] = {}
        for model in ValuationModel:
            model_episodes = [
                item for item in train_episodes if item.valuation_model == model
            ]
            if not model_episodes:
                continue
            parameters, metrics, leaderboard = self.select(
                model_episodes, erp_scenarios
            )
            route_key = self._metrics_key(metrics)
            route_supported = bool(route_key[0]) and (
                len(model_episodes) >= self.config.minimum_route_training_episodes
            )
            if route_supported:
                policy[model.value] = parameters
            if len(model_episodes) < self.config.minimum_route_training_episodes:
                route_support_reason = "insufficient_training_episodes"
            elif not route_key[0]:
                route_support_reason = "no_candidate_passes_all_erp_stress_cases"
            else:
                route_support_reason = None
            details[model.value] = {
                "episode_count": len(model_episodes),
                "parameters": parameters.to_dict(),
                "training_metrics": metrics,
                "route_supported": route_supported,
                "route_support_reason": route_support_reason,
                "candidate_count": len(leaderboard),
                "feasible_candidate_count": sum(
                    1
                    for item in leaderboard
                    if item["gate_passes_all_erp_scenarios"]
                ),
                "training_leaderboard": leaderboard,
            }
        merged_metrics = {
            float(erp): self.evaluate(train_episodes, policy, float(erp))
            for erp in erp_scenarios
        }
        return policy, merged_metrics, details

    def run(
        self,
        *,
        erp_scenarios: Iterable[float],
        symbols: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        scenarios = tuple(sorted({float(item) for item in erp_scenarios}))
        if any(not 0.0 < item < 0.20 for item in scenarios):
            raise ValueError("ERP scenarios must be between zero and 20%")
        episodes, skipped = self.build_episodes(symbols)
        train, validation, validation_start = self._split_episodes(
            episodes, self.config.train_fraction
        )
        if not train or not validation:
            raise ValueError("at least two evaluation dates are required")
        policy, train_metrics, selections = self.select_by_valuation_model(
            train, scenarios
        )
        validation_metrics = {
            erp: self.evaluate(validation, policy, erp, include_rows=True)
            for erp in scenarios
        }
        validation_key = self._metrics_key(validation_metrics)
        validation_passes = bool(validation_key[0])
        return {
            "contract": DCF_ENTRY_CALIBRATION_CONTRACT,
            "economic_contract": {
                "discount_rate": "cost_of_equity = risk_free_rate + beta * ERP",
                "debt_cost_ignored": True,
                "valuation_routing": {
                    "financial_industry": "residual income from book value, normalized ROE, payout and CAPM cost of equity; OCF-CAPEX is prohibited",
                    "cyclical_company": "through-cycle median plausible FCFE with volatile FCF CAGR excluded",
                    "ordinary_company": "recent plausible FCFE proxy",
                },
                "cash_flow_basis": "all FCFE proxies are OCF minus capex and must reconcile to positive net income and revenue; financial companies do not use this proxy",
                "terminal_growth": self.config.terminal_growth,
                "risk_growth_overlay": "financial_growth / ERP-dependent multiplier",
                "entry_price_gate": "buy_price/current_price must not exceed maximum_entry_to_current_price",
                "future_price_used_only_as_label": True,
            },
            "config": asdict(self.config),
            "erp_scenarios": list(scenarios),
            "dataset": {
                "root": str(self.root.resolve()),
                "market": self.market,
                "episode_count": len(episodes),
                "train_episode_count": len(train),
                "validation_episode_count": len(validation),
                "train_dates": sorted(
                    {item.evaluation_date.isoformat() for item in train}
                ),
                "validation_dates": sorted(
                    {item.evaluation_date.isoformat() for item in validation}
                ),
                "validation_start": validation_start,
                "skipped": skipped,
                "risk_free_dates_provided": len(self.risk_free_rates),
            },
            "selection": {
                "parameters": {
                    model: parameters.to_dict()
                    for model, parameters in policy.items()
                },
                "policy_beta_reference": self._training_beta_reference(train),
                "training_metrics": train_metrics,
                "training_selection_by_valuation_model": selections,
                "candidate_count": sum(
                    item["candidate_count"] for item in selections.values()
                ),
                "training_feasible_candidate_count": sum(
                    item["feasible_candidate_count"]
                    for item in selections.values()
                ),
                "training_aggregate_gate_passes_all_erp_scenarios": bool(
                    self._metrics_key(train_metrics)[0]
                ),
                "training_all_models_individually_feasible": all(
                    item["feasible_candidate_count"] > 0
                    for item in selections.values()
                ),
            },
            "validation": {
                "metrics": validation_metrics,
                "passes_all_erp_scenarios": validation_passes,
                "selection_never_read_validation_labels": True,
            },
            "acceptance": {
                "no_lookahead": True,
                "buy_price_reached_within_next_trading_year": True,
                "post_entry_observation_year": True,
                # This research tool never changes runtime state by itself.
                # A passed calibration is eligible for a separate, manually
                # activated unified-strategy experiment only after the actual
                # configured pool has its own causal coverage and return Gate.
                "candidate_eligible_for_manual_strategy_experiment": validation_passes,
                "requires_configured_pool_unified_backtest": True,
                "production_ready": False,
                "validation_passes_entry_quality_gate": validation_passes,
            },
        }

    def run_frozen_policy(
        self,
        *,
        policy: Mapping[str, CapmDcfEntryParameters],
        policy_available_from: date,
        erp_scenarios: Iterable[float],
        symbols: Iterable[str] | None = None,
        policy_provenance: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Apply a broad-universe policy to a smaller pool without retraining.

        ``policy_available_from`` is part of the policy contract: application
        rows before that date are excluded because the policy had not yet been
        selected.  The receiving pool's size is descriptive only; it cannot
        retrain, reject, or alter the frozen policy.
        """

        scenarios = tuple(sorted({float(item) for item in erp_scenarios}))
        if not policy:
            raise ValueError("frozen DCF policy must contain at least one route")
        if any(not 0.0 < item < 0.20 for item in scenarios):
            raise ValueError("ERP scenarios must be between zero and 20%")
        episodes, skipped = self.build_episodes(symbols)
        application = [
            item for item in episodes if item.evaluation_date >= policy_available_from
        ]
        metrics = {
            erp: self.evaluate(application, policy, erp, include_rows=True)
            for erp in scenarios
        }
        enabled_routes = sorted(policy)
        unsupported_routes = sorted(
            {item.valuation_model.value for item in application} - set(policy)
        )
        return {
            "contract": DCF_ENTRY_CALIBRATION_CONTRACT,
            "economic_contract": {
                "discount_rate": "cost_of_equity = risk_free_rate + beta * ERP",
                "debt_cost_ignored": True,
                "terminal_growth": self.config.terminal_growth,
                "future_price_used_only_as_label": True,
            },
            "config": asdict(self.config),
            "erp_scenarios": list(scenarios),
            "dataset": {
                "root": str(self.root.resolve()),
                "market": self.market,
                "episode_count": len(episodes),
                "application_episode_count": len(application),
                "application_start": policy_available_from.isoformat(),
                "skipped": skipped,
                "risk_free_dates_provided": len(self.risk_free_rates),
            },
            "selection": {
                "parameters": {
                    model: parameters.to_dict() for model, parameters in policy.items()
                },
                "training_metrics": {},
                "training_selection_by_valuation_model": {},
                "candidate_count": 0,
                "training_feasible_candidate_count": None,
                "frozen_policy": True,
                "policy_provenance": dict(policy_provenance or {}),
                "enabled_routes": enabled_routes,
                "unsupported_application_routes": unsupported_routes,
            },
            "validation": {
                "metrics": metrics,
                # This is a policy-transfer backtest, not an attempt to make a
                # statistically-powered re-certification from a small pool.
                "passes_all_erp_scenarios": None,
                "policy_never_read_application_labels": True,
                # Retain the generic renderer's audit key; unlike a local
                # selection, it means the frozen policy never read labels from
                # this receiving pool.
                "selection_never_read_validation_labels": True,
            },
            "acceptance": {
                "no_lookahead": True,
                "frozen_policy_not_retrained_on_application_pool": True,
                "requires_configured_pool_unified_backtest": True,
                "production_ready": False,
            },
        }
