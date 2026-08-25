"""Point-in-time capital-cost estimates for intrinsic valuation.

The estimator deliberately separates observed inputs from assumptions.  A
reported WACC is emitted only when market beta, dated capital-market inputs,
capital structure, debt cost, and tax data are all available.  Otherwise the
result is explicitly labelled as a cost-of-equity fallback.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from src.data.market_history import PriceHistoryBundle
from src.instruments.models import FinancialStatementSnapshot


@dataclass(frozen=True)
class CapitalMarketAssumptions:
    """Dated market inputs; rates are decimals, not percentages."""

    as_of: date
    risk_free_rate: float
    market_risk_premium: float
    risk_free_source: str
    market_risk_premium_source: str
    market_risk_premium_method: str = "unspecified"
    market_risk_premium_low: float | None = None
    market_risk_premium_high: float | None = None
    market_risk_premium_inputs: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["as_of"] = self.as_of.isoformat()
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CapitalMarketAssumptions":
        return cls(
            as_of=date.fromisoformat(str(payload["as_of"])),
            risk_free_rate=float(payload["risk_free_rate"]),
            market_risk_premium=float(payload["market_risk_premium"]),
            risk_free_source=str(payload["risk_free_source"]),
            market_risk_premium_source=str(
                payload["market_risk_premium_source"]
            ),
            market_risk_premium_method=str(
                payload.get("market_risk_premium_method", "legacy_unspecified")
            ),
            market_risk_premium_low=_number(
                payload.get("market_risk_premium_low")
            ),
            market_risk_premium_high=_number(
                payload.get("market_risk_premium_high")
            ),
            market_risk_premium_inputs={
                str(key): float(value)
                for key, value in dict(
                    payload.get("market_risk_premium_inputs", {})
                ).items()
            },
        )


class CapitalMarketAssumptionStore:
    """Auditable dated market inputs with strict point-in-time lookup."""

    CONTRACT = "capital-market-assumptions-2"
    READABLE_CONTRACTS = {"capital-market-assumptions-1", CONTRACT}

    def __init__(self, root: str | Path):
        self.path = Path(root) / "capital_market" / "assumptions.json"

    def read_all(self) -> list[CapitalMarketAssumptions]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("contract") not in self.READABLE_CONTRACTS:
            raise ValueError("unsupported capital-market assumption contract")
        return sorted(
            (
                CapitalMarketAssumptions.from_dict(item)
                for item in payload.get("assumptions", [])
            ),
            key=lambda item: item.as_of,
        )

    def as_of(
        self,
        evaluation_date: date,
        *,
        maximum_age_days: int | None = None,
    ) -> CapitalMarketAssumptions | None:
        eligible = [
            item for item in self.read_all() if item.as_of <= evaluation_date
        ]
        if not eligible:
            return None
        selected = eligible[-1]
        if (
            maximum_age_days is not None
            and (evaluation_date - selected.as_of).days > maximum_age_days
        ):
            return None
        return selected

    def upsert(self, assumption: CapitalMarketAssumptions) -> Path:
        rows = {item.as_of: item for item in self.read_all()}
        rows[assumption.as_of] = assumption
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "contract": self.CONTRACT,
                    "assumptions": [
                        rows[key].to_dict() for key in sorted(rows)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return self.path


@dataclass(frozen=True)
class CapitalCostConfig:
    beta_lookback_days: int = 365 * 5
    # A two-calendar-year A-share history has about 99-103 aligned weekly
    # returns after holidays and the first percentage-change row. Requiring
    # 104 silently dropped the designed two-year weekly estimator.
    minimum_beta_weeks: int = 78
    minimum_beta_daily_observations: int = 252
    beta_horizons_years: tuple[int, ...] = (2, 3, 5)
    # Retained only for explicit legacy experiments. Production estimation
    # uses estimate_robust_beta() and never applies fixed Blume shrinkage.
    beta_shrinkage_to_one: float = 0.0
    beta_floor: float = -3.0
    beta_cap: float = 3.0
    debt_cost_floor: float = 0.005
    debt_cost_cap: float = 0.12
    tax_rate_floor: float = 0.0
    tax_rate_cap: float = 0.35
    rate_sensitivity: float = 0.01


@dataclass(frozen=True)
class CapitalCostEstimate:
    evaluation_date: date
    assumptions_as_of: date
    risk_free_rate: float
    market_risk_premium: float
    raw_beta: float | None
    adjusted_beta: float | None
    beta_observations: int
    cost_of_equity: float
    pre_tax_cost_of_debt: float | None
    effective_tax_rate: float | None
    market_equity: float | None
    interest_bearing_debt: float | None
    available_cash: float | None
    net_debt: float | None
    equity_weight: float | None
    debt_weight: float | None
    wacc: float | None
    discount_rate_kind: str
    sources: dict[str, str] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    beta_method: str = "legacy_weekly"
    beta_low: float | None = None
    beta_high: float | None = None
    beta_components: tuple[dict[str, object], ...] = ()
    market_risk_premium_method: str = "unspecified"
    market_risk_premium_low: float | None = None
    market_risk_premium_high: float | None = None
    market_risk_premium_inputs: dict[str, float] = field(default_factory=dict)
    cost_of_equity_low: float | None = None
    cost_of_equity_high: float | None = None
    wacc_low: float | None = None
    wacc_high: float | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["evaluation_date"] = self.evaluation_date.isoformat()
        result["assumptions_as_of"] = self.assumptions_as_of.isoformat()
        return result


DEBT_FIELDS = (
    "short_term_borrowings",
    "current_portion_noncurrent_debt",
    "long_term_borrowings",
    "bonds_payable",
    "lease_liabilities",
)


@dataclass(frozen=True)
class BetaComponent:
    """One auditable OLS beta estimate for a horizon/frequency pair."""

    horizon_years: int
    frequency: str
    beta: float
    observations: int
    standard_error: float | None
    confidence_low: float | None
    confidence_high: float | None
    r_squared: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BetaEstimate:
    """Robust beta center plus the complete set of observable components."""

    evaluation_date: date
    beta: float | None
    lower_quartile: float | None
    upper_quartile: float | None
    components: tuple[BetaComponent, ...]
    method: str = "median_2y_3y_5y_daily_weekly_ols"

    @property
    def long_horizon_weekly(self) -> BetaComponent | None:
        weekly = [item for item in self.components if item.frequency == "weekly"]
        return max(weekly, key=lambda item: item.horizon_years) if weekly else None


def _price_returns(
    bundle: PriceHistoryBundle,
    *,
    start: date,
    end: date,
    frequency: str,
) -> pd.Series:
    frame = bundle.prices[["date", "qfq_close"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["qfq_close"] = pd.to_numeric(frame["qfq_close"], errors="coerce")
    selected = frame[
        (frame["date"].dt.date >= start)
        & (frame["date"].dt.date <= end)
    ].dropna()
    prices = selected.set_index("date")["qfq_close"].sort_index()
    prices = prices[~prices.index.duplicated(keep="last")]
    if frequency == "weekly":
        prices = prices.resample("W-FRI").last()
    elif frequency != "daily":
        raise ValueError(f"unsupported beta frequency: {frequency}")
    return prices.pct_change(fill_method=None)


def _ols_beta_component(
    company_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    horizon_years: int,
    frequency: str,
    minimum_observations: int,
) -> BetaComponent | None:
    aligned = pd.concat([company_returns, benchmark_returns], axis=1).dropna()
    observations = len(aligned)
    if observations < minimum_observations:
        return None
    aligned.columns = ["company", "benchmark"]
    x = aligned["benchmark"].to_numpy(dtype=float)
    y = aligned["company"].to_numpy(dtype=float)
    centered_x = x - x.mean()
    denominator = float(centered_x @ centered_x)
    if not np.isfinite(denominator) or denominator <= 1e-12:
        return None
    beta = float(centered_x @ (y - y.mean()) / denominator)
    intercept = float(y.mean() - beta * x.mean())
    residual = y - (intercept + beta * x)
    standard_error = None
    confidence_low = None
    confidence_high = None
    if observations > 2:
        residual_variance = float(residual @ residual / (observations - 2))
        if np.isfinite(residual_variance) and residual_variance >= 0:
            standard_error = float(np.sqrt(residual_variance / denominator))
            confidence_low = beta - 1.96 * standard_error
            confidence_high = beta + 1.96 * standard_error
    total_variance = float((y - y.mean()) @ (y - y.mean()))
    r_squared = None
    if total_variance > 1e-12:
        r_squared = float(1.0 - (residual @ residual) / total_variance)
    return BetaComponent(
        horizon_years=horizon_years,
        frequency=frequency,
        beta=beta,
        observations=observations,
        standard_error=standard_error,
        confidence_low=confidence_low,
        confidence_high=confidence_high,
        r_squared=r_squared,
    )


def estimate_robust_beta(
    company: PriceHistoryBundle,
    benchmark: PriceHistoryBundle,
    evaluation_date: date,
    config: CapitalCostConfig | None = None,
) -> BetaEstimate:
    """Estimate beta from multiple horizons without fixed shrinkage.

    The median across available 2/3/5-year daily and weekly OLS estimates is
    robust to a single horizon or sampling frequency. The IQR is retained as
    valuation sensitivity rather than hidden by forcing beta toward one.
    """

    settings = config or CapitalCostConfig()
    components: list[BetaComponent] = []
    for horizon in settings.beta_horizons_years:
        start = evaluation_date - timedelta(days=round(horizon * 365.25))
        for frequency in ("daily", "weekly"):
            minimum = (
                settings.minimum_beta_daily_observations
                if frequency == "daily"
                else settings.minimum_beta_weeks
            )
            component = _ols_beta_component(
                _price_returns(
                    company,
                    start=start,
                    end=evaluation_date,
                    frequency=frequency,
                ),
                _price_returns(
                    benchmark,
                    start=start,
                    end=evaluation_date,
                    frequency=frequency,
                ),
                horizon_years=horizon,
                frequency=frequency,
                minimum_observations=minimum,
            )
            if component is not None:
                components.append(component)
    if not components:
        return BetaEstimate(
            evaluation_date=evaluation_date,
            beta=None,
            lower_quartile=None,
            upper_quartile=None,
            components=(),
        )
    values = np.asarray([item.beta for item in components], dtype=float)
    return BetaEstimate(
        evaluation_date=evaluation_date,
        beta=float(np.median(values)),
        lower_quartile=float(np.quantile(values, 0.25)),
        upper_quartile=float(np.quantile(values, 0.75)),
        components=tuple(components),
    )


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def statement_debt(statement: FinancialStatementSnapshot) -> float | None:
    values = [_number(getattr(statement, name, None)) for name in DEBT_FIELDS]
    observed = [max(value, 0.0) for value in values if value is not None]
    return float(sum(observed)) if observed else None


def estimate_weekly_beta(
    company: PriceHistoryBundle,
    benchmark: PriceHistoryBundle,
    evaluation_date: date,
    config: CapitalCostConfig | None = None,
) -> tuple[float | None, float | None, int]:
    """Estimate trailing weekly beta without using prices after the date."""

    settings = config or CapitalCostConfig()
    start = evaluation_date - timedelta(days=settings.beta_lookback_days)

    def returns(bundle: PriceHistoryBundle) -> pd.Series:
        frame = bundle.prices[["date", "qfq_close"]].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["qfq_close"] = pd.to_numeric(
            frame["qfq_close"], errors="coerce"
        )
        selected = frame[
            (frame["date"].dt.date >= start)
            & (frame["date"].dt.date <= evaluation_date)
        ].dropna()
        weekly = selected.set_index("date")["qfq_close"].resample(
            "W-FRI"
        ).last()
        return weekly.pct_change(fill_method=None)

    aligned = pd.concat(
        [returns(company), returns(benchmark)], axis=1
    ).dropna()
    observations = len(aligned)
    if observations < settings.minimum_beta_weeks:
        return None, None, observations
    aligned.columns = ["company", "benchmark"]
    market_variance = float(aligned["benchmark"].var(ddof=1))
    if not np.isfinite(market_variance) or market_variance <= 1e-12:
        return None, None, observations
    raw = float(
        aligned[["company", "benchmark"]].cov().iloc[0, 1]
        / market_variance
    )
    shrink = float(np.clip(settings.beta_shrinkage_to_one, 0.0, 1.0))
    adjusted = (1.0 - shrink) * raw + shrink
    adjusted = float(
        np.clip(adjusted, settings.beta_floor, settings.beta_cap)
    )
    return raw, adjusted, observations


def _latest_annuals(
    statements: Iterable[FinancialStatementSnapshot],
) -> list[FinancialStatementSnapshot]:
    annual = [
        item
        for item in statements
        if item.period_type in {"year", "annual", "12M"}
    ]
    return sorted(
        annual,
        key=lambda item: (item.period_end, item.published_at or date.min),
        reverse=True,
    )


def estimate_capital_cost(
    *,
    evaluation_date: date,
    assumptions: CapitalMarketAssumptions,
    statements: Iterable[FinancialStatementSnapshot],
    shares: float | None,
    market_price: float | None,
    raw_beta: float | None,
    adjusted_beta: float | None,
    beta_observations: int,
    beta_method: str = "legacy_weekly",
    beta_low: float | None = None,
    beta_high: float | None = None,
    beta_components: tuple[dict[str, object], ...] = (),
    config: CapitalCostConfig | None = None,
) -> CapitalCostEstimate:
    """Calculate CAPM cost of equity and WACC from disclosed capital data."""

    settings = config or CapitalCostConfig()
    diagnostics: list[str] = []
    beta = adjusted_beta
    if beta is None:
        beta = 1.0
        diagnostics.append("beta_unavailable_used_explicit_one_fallback")
    cost_of_equity = (
        assumptions.risk_free_rate
        + beta * assumptions.market_risk_premium
    )
    cost_of_equity_low = None
    cost_of_equity_high = None
    if (
        beta_low is not None
        and beta_high is not None
        and assumptions.market_risk_premium_low is not None
        and assumptions.market_risk_premium_high is not None
    ):
        equity_premiums = [
            candidate_beta * candidate_premium
            for candidate_beta in (beta_low, beta_high)
            for candidate_premium in (
                assumptions.market_risk_premium_low,
                assumptions.market_risk_premium_high,
            )
        ]
        cost_of_equity_low = assumptions.risk_free_rate + min(
            equity_premiums
        )
        cost_of_equity_high = assumptions.risk_free_rate + max(
            equity_premiums
        )

    annual = _latest_annuals(statements)
    latest = annual[0] if annual else None
    prior = annual[1] if len(annual) > 1 else None
    debt = statement_debt(latest) if latest is not None else None
    prior_debt = statement_debt(prior) if prior is not None else None
    interest = _number(getattr(latest, "interest_expense", None)) if latest else None
    if interest is not None:
        interest = abs(interest)
    average_debt = None
    if debt is not None and prior_debt is not None:
        average_debt = (debt + prior_debt) / 2.0
    elif debt is not None:
        average_debt = debt
        diagnostics.append("debt_cost_uses_ending_debt_no_prior_annual")
    debt_cost = None
    if interest is not None and average_debt is not None and average_debt > 0:
        debt_cost = float(
            np.clip(
                interest / average_debt,
                settings.debt_cost_floor,
                settings.debt_cost_cap,
            )
        )
    else:
        diagnostics.append("pre_tax_cost_of_debt_unavailable")

    tax_expense = (
        _number(getattr(latest, "income_tax_expense", None))
        if latest
        else None
    )
    profit_before_tax = (
        _number(getattr(latest, "profit_before_tax", None))
        if latest
        else None
    )
    tax_rate = None
    if (
        tax_expense is not None
        and profit_before_tax is not None
        and profit_before_tax > 0
    ):
        tax_rate = float(
            np.clip(
                tax_expense / profit_before_tax,
                settings.tax_rate_floor,
                settings.tax_rate_cap,
            )
        )
    else:
        diagnostics.append("effective_tax_rate_unavailable")

    equity = None
    if (
        shares is not None
        and shares > 0
        and market_price is not None
        and market_price > 0
    ):
        equity = float(shares * market_price)
    else:
        diagnostics.append("market_equity_unavailable")

    cash = (
        _number(getattr(latest, "cash_and_cash_equivalents", None))
        if latest
        else None
    )
    available_cash = max(0.0, cash) if cash is not None else None
    net_debt = (
        debt - available_cash
        if debt is not None and available_cash is not None
        else None
    )

    wacc = None
    wacc_low = None
    wacc_high = None
    equity_weight = None
    debt_weight = None
    market_inputs_observed = not (
        assumptions.risk_free_source == "configured_fallback"
        or assumptions.market_risk_premium_source == "configured_fallback"
    )
    if (
        equity is not None
        and debt is not None
        and debt_cost is not None
        and tax_rate is not None
        and equity + debt > 0
    ):
        equity_weight = equity / (equity + debt)
        debt_weight = debt / (equity + debt)
        wacc = (
            equity_weight * cost_of_equity
            + debt_weight * debt_cost * (1.0 - tax_rate)
        )
        if cost_of_equity_low is not None and cost_of_equity_high is not None:
            debt_component = debt_weight * debt_cost * (1.0 - tax_rate)
            wacc_low = equity_weight * cost_of_equity_low + debt_component
            wacc_high = equity_weight * cost_of_equity_high + debt_component
        kind = (
            "point_in_time_wacc"
            if market_inputs_observed
            else "configured_market_inputs_wacc_fallback"
        )
    else:
        kind = (
            "point_in_time_cost_of_equity_fallback"
            if market_inputs_observed
            else "configured_cost_of_equity_fallback"
        )

    return CapitalCostEstimate(
        evaluation_date=evaluation_date,
        assumptions_as_of=assumptions.as_of,
        risk_free_rate=assumptions.risk_free_rate,
        market_risk_premium=assumptions.market_risk_premium,
        raw_beta=raw_beta,
        adjusted_beta=adjusted_beta,
        beta_observations=beta_observations,
        cost_of_equity=float(cost_of_equity),
        pre_tax_cost_of_debt=debt_cost,
        effective_tax_rate=tax_rate,
        market_equity=equity,
        interest_bearing_debt=debt,
        available_cash=available_cash,
        net_debt=net_debt,
        equity_weight=equity_weight,
        debt_weight=debt_weight,
        wacc=float(wacc) if wacc is not None else None,
        discount_rate_kind=kind,
        sources={
            "risk_free_rate": assumptions.risk_free_source,
            "market_risk_premium": assumptions.market_risk_premium_source,
            "capital_structure": getattr(latest, "source", "") if latest else "",
        },
        diagnostics=tuple(diagnostics),
        beta_method=beta_method,
        beta_low=beta_low,
        beta_high=beta_high,
        beta_components=beta_components,
        market_risk_premium_method=assumptions.market_risk_premium_method,
        market_risk_premium_low=assumptions.market_risk_premium_low,
        market_risk_premium_high=assumptions.market_risk_premium_high,
        market_risk_premium_inputs=assumptions.market_risk_premium_inputs,
        cost_of_equity_low=(
            float(cost_of_equity_low)
            if cost_of_equity_low is not None
            else None
        ),
        cost_of_equity_high=(
            float(cost_of_equity_high)
            if cost_of_equity_high is not None
            else None
        ),
        wacc_low=float(wacc_low) if wacc_low is not None else None,
        wacc_high=float(wacc_high) if wacc_high is not None else None,
    )
