"""Point-in-time intrinsic-value experts for a conservative value strategy.

Market price never determines forecast cash flow or growth. CAPM remains a
market-pricing diagnostic, while equity cash flows are valued at the
investor's explicit required return. Reverse DCF uses market price only to
report the growth embedded in that price; it never feeds that growth back into
intrinsic value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle
from src.fundamental_embedding.capital_cost import (
    CapitalCostEstimate,
    CapitalMarketAssumptionStore,
    CapitalMarketAssumptions,
    estimate_capital_cost,
    estimate_robust_beta,
)
from src.instruments.calculations import derive_company_fundamentals
from src.instruments.classifier import detect_market
from src.instruments.models import MetricStatus, MetricValue
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    adjust_statement_shares,
)


EXPERT_NAMES = (
    "cash_flow_dcf",
    "earnings_power_dcf",
    "dividend_discount",
    "residual_income",
)


@dataclass(frozen=True)
class IntrinsicValueConfig:
    """Conservative economic assumptions, independent of market price."""

    projection_years: int = 5
    risk_free_rate: float = 0.02
    equity_risk_premium: float = 0.06
    beta_assumption: float = 1.0
    investor_required_return_low: float = 0.07
    investor_required_return: float = 0.075
    investor_required_return_high: float = 0.08
    market_cost_of_equity_floor: bool = True
    default_subjective_haircut: float = 0.15
    rate_sensitivity: float = 0.01
    terminal_growth: float = 0.02
    maximum_growth: float = 0.12
    minimum_growth: float = -0.08
    conservative_quantile: float = 0.25
    minimum_expert_weight: float = 0.02
    gate_temperature: float = 0.15
    active_expert_count: int = 2
    financial_stale_days: int = 550
    capital_market_stale_days: int = 120
    equity_cash_flow_only: bool = True


@dataclass(frozen=True)
class SubjectiveRiskAdjustment:
    """Auditable human overlay; never fitted from future returns."""

    price_haircut: float = 0.0
    adverse_event_probability: float = 0.0
    adverse_event_loss: float = 0.0
    uncertainty_multiplier: float = 1.0
    reason: str = ""
    effective_from: date | None = None
    expires_at: date | None = None

    def active_on(self, evaluation_date: date) -> bool:
        starts_in_time = (
            self.effective_from is None
            or self.effective_from <= evaluation_date
        )
        has_not_expired = (
            self.expires_at is None or evaluation_date <= self.expires_at
        )
        return starts_in_time and has_not_expired

    def expected_price_haircut(self) -> float:
        """Expected loss overlay, separate from the discount rate."""

        probability = float(np.clip(self.adverse_event_probability, 0.0, 1.0))
        event_loss = float(np.clip(self.adverse_event_loss, 0.0, 1.0))
        return float(
            np.clip(
                max(0.0, self.price_haircut) + probability * event_loss,
                0.0,
                0.80,
            )
        )

    def event_uncertainty(self) -> float:
        """Bernoulli event-loss standard deviation in price-return units."""

        probability = float(np.clip(self.adverse_event_probability, 0.0, 1.0))
        event_loss = float(np.clip(self.adverse_event_loss, 0.0, 1.0))
        multiplier = float(max(0.0, self.uncertainty_multiplier))
        return float(
            event_loss
            * np.sqrt(probability * (1.0 - probability))
            * multiplier
        )


@dataclass(frozen=True)
class ValuationSnapshot:
    symbol: str
    evaluation_date: date
    market_date: date
    current_price: float
    shares: float | None
    revenue_per_share: float | None
    earnings_per_share: float | None
    free_cash_flow_per_share: float | None
    book_value_per_share: float | None
    dividend_per_share: float | None
    roe: float | None
    growth: float
    payout_ratio: float | None
    cash_conversion: float | None
    earnings_stability: float
    dividend_stability: float
    fcf_history_count: int
    dividend_history_count: int
    financial_age_days: int | None
    capital_cost: CapitalCostEstimate | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpertValuation:
    expert_id: str
    available: bool
    compatibility: float
    low: float | None = None
    base: float | None = None
    high: float | None = None
    safety_margin: float = 0.0
    assumptions: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntrinsicValueEstimate:
    symbol: str
    evaluation_date: date
    market_date: date
    current_price: float
    fair_value_low: float | None
    fair_value: float | None
    fair_value_high: float | None
    buy_price: float | None
    margin_of_safety: float | None
    fair_value_gap: float | None
    confidence: float
    gate: dict[str, float]
    market_implied_growth: dict[str, float | None]
    experts: tuple[ExpertValuation, ...]
    risk: SubjectiveRiskAdjustment
    required_return_policy: dict[str, Any] = field(default_factory=dict)
    reverse_dcf: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evaluation_date"] = self.evaluation_date.isoformat()
        result["market_date"] = self.market_date.isoformat()
        if self.risk.effective_from is not None:
            result["risk"]["effective_from"] = (
                self.risk.effective_from.isoformat()
            )
        if self.risk.expires_at is not None:
            result["risk"]["expires_at"] = self.risk.expires_at.isoformat()
        return result


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _metric_value(metric: Any) -> float | None:
    return _finite(getattr(metric, "value", None))


def _robust_center(values: Iterable[float]) -> float | None:
    selected = np.asarray(
        [value for item in values if (value := _finite(item)) is not None],
        dtype=np.float64,
    )
    if not len(selected):
        return None
    return float(np.median(selected))


def _stability(values: Iterable[float]) -> float:
    selected = np.asarray(
        [value for item in values if (value := _finite(item)) is not None],
        dtype=np.float64,
    )
    if len(selected) < 2:
        return 0.25
    center = float(np.median(selected))
    scale = max(abs(center), float(np.median(np.abs(selected))), 1e-9)
    dispersion = float(np.median(np.abs(selected - center))) / scale
    return float(np.clip(1.0 / (1.0 + 3.0 * dispersion), 0.0, 1.0))


def _cagr(first: float, last: float, years: float) -> float | None:
    if first <= 0 or last <= 0 or years <= 0:
        return None
    return float((last / first) ** (1.0 / years) - 1.0)


def _discounted_growth_value(
    cash_per_share: float,
    growth: float,
    discount_rate: float,
    terminal_growth: float,
    years: int,
) -> float | None:
    if cash_per_share <= 0 or discount_rate <= terminal_growth + 0.0025:
        return None
    cash = float(cash_per_share)
    present = 0.0
    for year in range(1, years + 1):
        cash *= 1.0 + growth
        present += cash / (1.0 + discount_rate) ** year
    terminal_cash = cash * (1.0 + terminal_growth)
    terminal = terminal_cash / (discount_rate - terminal_growth)
    result = present + terminal / (1.0 + discount_rate) ** years
    return float(result) if np.isfinite(result) and result > 0 else None


def _weighted_quantile(
    values: list[float], weights: list[float], quantile: float
) -> float:
    order = np.argsort(values)
    ordered_values = np.asarray(values, dtype=np.float64)[order]
    ordered_weights = np.asarray(weights, dtype=np.float64)[order]
    cumulative = np.cumsum(ordered_weights)
    threshold = float(np.clip(quantile, 0.0, 1.0)) * cumulative[-1]
    index = int(np.searchsorted(cumulative, threshold, side="left"))
    return float(ordered_values[min(index, len(ordered_values) - 1)])


def _market_implied_growth(
    cash_per_share: float | None,
    market_price: float,
    discount_rate: float | None,
    terminal_growth: float | None,
    years: int,
) -> float | None:
    """Solve the explicit growth rate implied by price for diagnostics only."""

    if (
        cash_per_share is None
        or cash_per_share <= 0
        or market_price <= 0
        or discount_rate is None
        or terminal_growth is None
    ):
        return None
    low, high = -0.30, 0.30
    low_value = _discounted_growth_value(
        cash_per_share, low, discount_rate, terminal_growth, years
    )
    high_value = _discounted_growth_value(
        cash_per_share, high, discount_rate, terminal_growth, years
    )
    if (
        low_value is None
        or high_value is None
        or not low_value <= market_price <= high_value
    ):
        return None
    for _ in range(80):
        middle = (low + high) / 2.0
        value = _discounted_growth_value(
            cash_per_share,
            middle,
            discount_rate,
            terminal_growth,
            years,
        )
        if value is None:
            return None
        if value < market_price:
            low = middle
        else:
            high = middle
    return float((low + high) / 2.0)


class PointInTimeValuationBuilder:
    """Build price-independent valuation inputs from disclosed history."""

    def __init__(
        self,
        root: str | Path,
        market: str = "a_share",
        *,
        capital_market_assumptions: CapitalMarketAssumptions | None = None,
        benchmark_code: str = "sh.000300",
        benchmark_bundle: PriceHistoryBundle | None = None,
    ):
        self.root = Path(root)
        self.market = market
        self.market_store = PointInTimeMarketStore(self.root)
        self.fundamental_store = PointInTimeFundamentalStore(self.root)
        self.capital_market_store = CapitalMarketAssumptionStore(self.root)
        self.capital_market_assumptions = capital_market_assumptions
        self.benchmark_code = benchmark_code
        self.benchmark_bundle = benchmark_bundle

    def available_symbols(
        self, symbols: Iterable[str] | None = None
    ) -> list[str]:
        requested = {str(item) for item in symbols or []}
        market_codes = {
            path.stem
            for path in (self.root / "market").glob("*.csv")
        }
        statement_codes = {
            path.name.split(".statements.json")[0]
            for path in (self.root / "fundamentals").glob(
                "*.statements.json"
            )
        }
        return [
            code
            for code in sorted(market_codes & statement_codes)
            if detect_market(code) == self.market
            and (not requested or code in requested)
        ]

    @staticmethod
    def _market_row(
        bundle: PriceHistoryBundle, evaluation_date: date
    ) -> tuple[int, pd.Series] | None:
        dates = pd.to_datetime(bundle.prices["date"]).dt.date.to_numpy()
        index = int(np.searchsorted(dates, evaluation_date, side="right") - 1)
        if index < 0:
            return None
        row = bundle.prices.iloc[index]
        market_date = pd.Timestamp(row["date"]).date()
        if (evaluation_date - market_date).days > 10:
            return None
        return index, row

    @staticmethod
    def _annual_per_share_history(
        statements: list[Any], field: str
    ) -> list[tuple[date, float]]:
        result: list[tuple[date, float]] = []
        for item in statements:
            if item.period_type not in {"year", "annual", "12M"}:
                continue
            value = _finite(getattr(item, field, None))
            shares = _finite(
                item.common_shares_outstanding
                or item.total_shares
                or item.diluted_average_shares
            )
            if value is not None and shares is not None and shares > 0:
                result.append((item.period_end, value / shares))
        return result

    @staticmethod
    def _annual_dividends(
        bundle: PriceHistoryBundle, evaluation_date: date
    ) -> list[tuple[int, float]]:
        totals: dict[int, float] = {}
        for item in bundle.actions:
            if (
                item.cash_per_share is None
                or item.ex_date > evaluation_date
                or (
                    item.published_at is not None
                    and item.published_at > evaluation_date
                )
            ):
                continue
            totals[item.ex_date.year] = totals.get(item.ex_date.year, 0.0) + (
                float(item.cash_per_share)
            )
        return sorted(totals.items())

    @staticmethod
    def _growth_from_history(
        revenue: list[tuple[date, float]],
        earnings: list[tuple[date, float]],
        config: IntrinsicValueConfig,
    ) -> float:
        candidates = []
        for history in (revenue, earnings):
            positive = [(when, value) for when, value in history if value > 0]
            if len(positive) < 2:
                continue
            start, end = positive[max(0, len(positive) - 4)], positive[-1]
            value = _cagr(
                start[1], end[1], (end[0] - start[0]).days / 365.25
            )
            if value is not None:
                candidates.append(value)
        center = _robust_center(candidates)
        return float(
            np.clip(
                0.0 if center is None else center,
                config.minimum_growth,
                config.maximum_growth,
            )
        )

    def snapshot(
        self,
        symbol: str,
        evaluation_date: date,
        config: IntrinsicValueConfig | None = None,
    ) -> ValuationSnapshot | None:
        config = config or IntrinsicValueConfig()
        bundle = self.market_store.read(symbol)
        if bundle is None:
            return None
        selected = self._market_row(bundle, evaluation_date)
        if selected is None:
            return None
        _, market_row = selected
        market_date = pd.Timestamp(market_row["date"]).date()
        price = _finite(market_row["raw_close"])
        if price is None or price <= 0:
            return None
        statements = self.fundamental_store.as_of(symbol, evaluation_date)
        if not statements:
            return None
        statements = adjust_statement_shares(
            statements, bundle.actions, evaluation_date
        )
        assumptions = self.capital_market_assumptions
        if assumptions is None:
            assumptions = self.capital_market_store.as_of(
                evaluation_date,
                maximum_age_days=config.capital_market_stale_days,
            )
        if assumptions is None or assumptions.as_of > evaluation_date:
            assumptions = CapitalMarketAssumptions(
                as_of=evaluation_date,
                risk_free_rate=config.risk_free_rate,
                market_risk_premium=config.equity_risk_premium,
                risk_free_source="configured_fallback",
                market_risk_premium_source="configured_fallback",
            )
        benchmark = (
            self.benchmark_bundle
            or self.market_store.read(self.benchmark_code)
        )
        if benchmark is None:
            beta_estimate = None
            raw_beta, adjusted_beta, beta_observations = None, None, 0
        else:
            beta_estimate = estimate_robust_beta(
                bundle, benchmark, evaluation_date
            )
            reference = beta_estimate.long_horizon_weekly
            raw_beta = reference.beta if reference is not None else None
            adjusted_beta = beta_estimate.beta
            beta_observations = (
                reference.observations if reference is not None else 0
            )
        company = derive_company_fundamentals(
            statements,
            current_price=MetricValue(
                value=price,
                status=MetricStatus.OBSERVED,
                as_of=market_date,
                source="point_in_time_raw_close",
            ),
            evaluation_date=evaluation_date,
        )
        shares = _metric_value(company.total_shares)
        revenue = _metric_value(company.ttm_revenue)
        earnings = _metric_value(company.ttm_net_income_parent)
        fcf = _metric_value(company.ttm_free_cash_flow)
        book = _metric_value(company.book_value_per_share)
        roe = _metric_value(company.roe_ttm)
        if roe is not None:
            roe /= 100.0
        earnings_history = self._annual_per_share_history(
            statements, "net_income_parent"
        )
        revenue_history = self._annual_per_share_history(
            statements, "revenue"
        )
        fcf_history = self._annual_per_share_history(
            statements, "free_cash_flow"
        )
        dividend_history = self._annual_dividends(bundle, evaluation_date)
        trailing_start = evaluation_date - timedelta(days=365)
        ttm_dividend = sum(
            float(item.cash_per_share)
            for item in bundle.actions
            if item.cash_per_share is not None
            and trailing_start < item.ex_date <= evaluation_date
            and (
                item.published_at is None
                or item.published_at <= evaluation_date
            )
        )
        eps = earnings / shares if (
            earnings is not None and shares is not None and shares > 0
        ) else None
        fcf_ps = fcf / shares if (
            fcf is not None and shares is not None and shares > 0
        ) else None
        revenue_ps = revenue / shares if (
            revenue is not None and shares is not None and shares > 0
        ) else None
        payout = (
            ttm_dividend / eps
            if eps is not None and eps > 0 and ttm_dividend > 0
            else None
        )
        conversion = (
            fcf / earnings
            if fcf is not None and earnings is not None and earnings > 0
            else None
        )
        capital_cost = estimate_capital_cost(
            evaluation_date=evaluation_date,
            assumptions=assumptions,
            statements=statements,
            shares=shares,
            market_price=price,
            raw_beta=raw_beta,
            adjusted_beta=adjusted_beta,
            beta_observations=beta_observations,
            beta_method=(
                beta_estimate.method
                if beta_estimate is not None
                else "benchmark_unavailable"
            ),
            beta_low=(
                beta_estimate.lower_quartile
                if beta_estimate is not None
                else None
            ),
            beta_high=(
                beta_estimate.upper_quartile
                if beta_estimate is not None
                else None
            ),
            beta_components=(
                tuple(item.to_dict() for item in beta_estimate.components)
                if beta_estimate is not None
                else ()
            ),
        )
        publication_dates = [
            item.published_at
            for item in statements
            if item.published_at is not None
        ]
        age = (
            (evaluation_date - max(publication_dates)).days
            if publication_dates
            else None
        )
        diagnostics = []
        if fcf_ps is None:
            diagnostics.append("ttm_fcf_unavailable")
        if book is None:
            diagnostics.append("book_value_per_share_unavailable")
        if not ttm_dividend:
            diagnostics.append("ttm_dividend_unavailable")
        if age is None or age > config.financial_stale_days:
            diagnostics.append("financial_data_stale")
        diagnostics.extend(capital_cost.diagnostics)
        return ValuationSnapshot(
            symbol=symbol,
            evaluation_date=evaluation_date,
            market_date=market_date,
            current_price=price,
            shares=shares,
            revenue_per_share=revenue_ps,
            earnings_per_share=eps,
            free_cash_flow_per_share=fcf_ps,
            book_value_per_share=book,
            dividend_per_share=ttm_dividend or None,
            roe=roe,
            growth=self._growth_from_history(
                revenue_history, earnings_history, config
            ),
            payout_ratio=payout,
            cash_conversion=conversion,
            earnings_stability=_stability(
                value for _, value in earnings_history[-5:]
            ),
            dividend_stability=_stability(
                value for _, value in dividend_history[-5:]
            ),
            fcf_history_count=len(fcf_history),
            dividend_history_count=len(dividend_history),
            financial_age_days=age,
            capital_cost=capital_cost,
            diagnostics=tuple(diagnostics),
        )

    def latest_date(self, symbols: Iterable[str] | None = None) -> date:
        dates = []
        for symbol in self.available_symbols(symbols):
            bundle = self.market_store.read(symbol)
            if bundle is not None and len(bundle.prices):
                dates.append(pd.to_datetime(bundle.prices["date"]).max().date())
        if not dates:
            raise ValueError("no point-in-time market data is available")
        return max(dates)


class IntrinsicValueEngine:
    """Economic-compatibility gate over auditable valuation experts."""

    def __init__(self, config: IntrinsicValueConfig | None = None):
        self.config = config or IntrinsicValueConfig()

        hurdle = (
            self.config.investor_required_return_low,
            self.config.investor_required_return,
            self.config.investor_required_return_high,
        )
        if not 0.0 < hurdle[0] <= hurdle[1] <= hurdle[2] < 1.0:
            raise ValueError(
                "investor required returns must satisfy "
                "0 < low <= base <= high < 1"
            )
        if hurdle[0] <= self.config.terminal_growth + 0.0025:
            raise ValueError(
                "the low investor required return must exceed terminal "
                "growth by at least 25 basis points"
            )

    def _required_return_policy(
        self, snapshot: ValuationSnapshot
    ) -> dict[str, Any]:
        """Separate market CAPM diagnostics from the investor hurdle.

        Scenario names describe valuation outcomes. Downside therefore carries
        the highest discount rate and upside the lowest. The investor hurdle is
        a floor, never a replacement for a genuinely higher market-implied
        cost of equity.
        """

        capital_cost = snapshot.capital_cost
        if capital_cost is not None:
            market_base = capital_cost.cost_of_equity
            observed_low = capital_cost.cost_of_equity_low
            observed_high = capital_cost.cost_of_equity_high
            market_kind = (
                "configured_cost_of_equity_fallback"
                if capital_cost.discount_rate_kind.startswith("configured_")
                else "point_in_time_cost_of_equity"
            )
        else:
            market_base = (
                self.config.risk_free_rate
                + self.config.beta_assumption * self.config.equity_risk_premium
            )
            market_kind = "configured_cost_of_equity_fallback"
            observed_low = None
            observed_high = None

        sensitivity = max(self.config.rate_sensitivity, 0.0)
        market_low = (
            float(observed_low)
            if observed_low is not None
            else max(float(market_base) - sensitivity, 0.001)
        )
        market_high = (
            float(observed_high)
            if observed_high is not None
            else float(market_base) + sensitivity
        )
        market_base = float(market_base)
        personal = {
            "upside": self.config.investor_required_return_low,
            "base": self.config.investor_required_return,
            "downside": self.config.investor_required_return_high,
        }
        market = {
            "upside": market_low,
            "base": market_base,
            "downside": market_high,
        }
        if self.config.market_cost_of_equity_floor:
            applied = {
                name: max(float(personal[name]), float(market[name]))
                for name in personal
            }
            policy_kind = "investor_hurdle_with_market_cost_floor"
        else:
            applied = {name: float(value) for name, value in personal.items()}
            policy_kind = "investor_hurdle_only"

        # Preserve scenario ordering even when separately estimated market
        # confidence bounds cross.
        applied["base"] = max(
            applied["upside"], min(applied["base"], applied["downside"])
        )
        applied["downside"] = max(applied["downside"], applied["base"])
        applied["upside"] = min(applied["upside"], applied["base"])
        return {
            "kind": policy_kind,
            "cash_flow_basis": "equity_cash_flow",
            "market_cost_kind": market_kind,
            "market_cost_of_equity": market,
            "investor_required_return": personal,
            "applied_required_return": applied,
            "market_floor_enabled": self.config.market_cost_of_equity_floor,
            "terminal_growth": self.config.terminal_growth,
        }

    def _rates(
        self, snapshot: ValuationSnapshot
    ) -> tuple[float, float, float, str]:
        policy = self._required_return_policy(snapshot)
        applied = policy["applied_required_return"]
        return (
            float(applied["downside"]),
            float(applied["base"]),
            float(applied["upside"]),
            str(policy["kind"]),
        )

    def _dcf(
        self,
        snapshot: ValuationSnapshot,
        risk: SubjectiveRiskAdjustment,
    ) -> ExpertValuation:
        cash = snapshot.free_cash_flow_per_share
        available = cash is not None and cash > 0
        conversion = snapshot.cash_conversion
        compatibility = 0.0
        if available:
            compatibility = 0.35 + 0.12 * min(snapshot.fcf_history_count, 4)
            if conversion is not None and 0.35 <= conversion <= 1.5:
                compatibility += 0.15
            compatibility *= 0.55 + 0.45 * snapshot.earnings_stability
        if not available:
            return ExpertValuation(
                expert_id="cash_flow_dcf",
                available=False,
                compatibility=0.0,
                safety_margin=0.15,
                diagnostics=("positive_point_in_time_fcf_required",),
            )
        low_rate, base_rate, high_rate, rate_kind = self._rates(snapshot)
        growth = snapshot.growth
        values = (
            _discounted_growth_value(
                cash * 0.85,
                min(growth - 0.03, 0.03),
                low_rate,
                0.0,
                self.config.projection_years,
            ),
            _discounted_growth_value(
                cash,
                growth,
                base_rate,
                self.config.terminal_growth,
                self.config.projection_years,
            ),
            _discounted_growth_value(
                cash * 1.10,
                min(growth + 0.02, self.config.maximum_growth),
                high_rate,
                min(self.config.terminal_growth + 0.01, 0.025),
                self.config.projection_years,
            ),
        )

        return ExpertValuation(
            expert_id="cash_flow_dcf",
            available=all(value is not None for value in values),
            compatibility=float(np.clip(compatibility, 0.0, 1.0)),
            low=values[0],
            base=values[1],
            high=values[2],
            safety_margin=0.15,
            assumptions={
                "discount_rate_kind": rate_kind,
                "required_return_policy": self._required_return_policy(snapshot),
                "cash_flow_kind": "fcfe_proxy_ttm_ocf_minus_capex_per_share",
                "cash_per_share": cash,
                "net_debt_per_share": None,
                "cash_flow_warning": (
                    "net_borrowing_unavailable_fcfe_proxy; no net-debt bridge"
                ),
                "capital_cost": (
                    snapshot.capital_cost.to_dict()
                    if snapshot.capital_cost is not None
                    else None
                ),
                "explicit_growth": growth,
                "terminal_growth": self.config.terminal_growth,
                "discount_rates": [low_rate, base_rate, high_rate],
            },
        )

    def _earnings_power(
        self,
        snapshot: ValuationSnapshot,
        risk: SubjectiveRiskAdjustment,
    ) -> ExpertValuation:
        earnings = snapshot.earnings_per_share
        if earnings is None or earnings <= 0:
            return ExpertValuation(
                expert_id="earnings_power_dcf",
                available=False,
                compatibility=0.0,
                safety_margin=0.20,
                diagnostics=("positive_point_in_time_earnings_required",),
            )
        conversion = snapshot.cash_conversion
        if conversion is None or conversion <= 0:
            distributable = 0.60
        else:
            distributable = float(np.clip(conversion, 0.40, 0.90))
        cash = earnings * distributable
        low_rate, base_rate, high_rate, rate_kind = self._rates(snapshot)
        growth = snapshot.growth
        values = (
            _discounted_growth_value(
                cash * 0.80,
                min(growth - 0.04, 0.02),
                low_rate,
                0.0,
                self.config.projection_years,
            ),
            _discounted_growth_value(
                cash,
                min(growth, 0.08),
                base_rate,
                self.config.terminal_growth,
                self.config.projection_years,
            ),
            _discounted_growth_value(
                cash * 1.10,
                min(growth + 0.02, 0.10),
                high_rate,
                0.025,
                self.config.projection_years,
            ),
        )
        compatibility = (
            0.45
            + 0.40 * snapshot.earnings_stability
            + (0.10 if snapshot.free_cash_flow_per_share is None else 0.0)
        )
        if snapshot.free_cash_flow_per_share is not None:
            compatibility *= 0.55
        return ExpertValuation(
            expert_id="earnings_power_dcf",
            available=all(value is not None for value in values),
            compatibility=float(np.clip(compatibility, 0.0, 1.0)),
            low=values[0],
            base=values[1],
            high=values[2],
            safety_margin=0.20,
            assumptions={
                "discount_rate_kind": rate_kind,
                "required_return_policy": self._required_return_policy(snapshot),
                "cash_flow_kind": "normalized_distributable_earnings_per_share",
                "earnings_per_share": earnings,
                "distributable_fraction": distributable,
                "cash_per_share": cash,
                "explicit_growth": min(growth, 0.08),
                "terminal_growth": self.config.terminal_growth,
                "discount_rates": [low_rate, base_rate, high_rate],
            },
        )

    def _ddm(
        self,
        snapshot: ValuationSnapshot,
        risk: SubjectiveRiskAdjustment,
    ) -> ExpertValuation:
        dividend = snapshot.dividend_per_share
        if dividend is None or dividend <= 0:
            return ExpertValuation(
                expert_id="dividend_discount",
                available=False,
                compatibility=0.0,
                safety_margin=0.10,
                diagnostics=("positive_ttm_dividend_required",),
            )
        payout = snapshot.payout_ratio
        sustainable = snapshot.growth
        if payout is not None and snapshot.roe is not None:
            sustainable = snapshot.roe * max(0.0, 1.0 - payout)
        growth = float(np.clip(sustainable, -0.03, 0.07))
        low_rate, base_rate, high_rate, rate_kind = self._rates(snapshot)
        values = (
            _discounted_growth_value(
                dividend * 0.90,
                min(growth - 0.02, 0.01),
                low_rate,
                0.0,
                self.config.projection_years,
            ),
            _discounted_growth_value(
                dividend,
                growth,
                base_rate,
                min(self.config.terminal_growth, growth, 0.02),
                self.config.projection_years,
            ),
            _discounted_growth_value(
                dividend * 1.05,
                min(growth + 0.015, 0.08),
                high_rate,
                min(max(growth, 0.0), 0.025),
                self.config.projection_years,
            ),
        )
        payout_score = 0.6 if payout is None else (
            1.0 if 0.25 <= payout <= 0.90 else 0.45
        )
        compatibility = (
            0.25
            + 0.12 * min(snapshot.dividend_history_count, 4)
            + 0.20 * snapshot.dividend_stability
        ) * payout_score
        growth_fit = float(np.clip(
            1.0 - max(snapshot.growth - 0.03, 0.0) / 0.09,
            0.25,
            1.0,
        ))
        compatibility *= growth_fit
        return ExpertValuation(
            expert_id="dividend_discount",
            available=all(value is not None for value in values),
            compatibility=float(np.clip(compatibility, 0.0, 1.0)),
            low=values[0],
            base=values[1],
            high=values[2],
            safety_margin=0.10,
            assumptions={
                "discount_rate_kind": rate_kind,
                "required_return_policy": self._required_return_policy(snapshot),
                "cash_flow_kind": "ttm_cash_dividend_per_share",
                "dividend_per_share": dividend,
                "payout_ratio": payout,
                "cash_per_share": dividend,
                "explicit_growth": growth,
                "terminal_growth": min(
                    self.config.terminal_growth, growth, 0.02
                ),
                "discount_rates": [low_rate, base_rate, high_rate],
            },
        )

    @staticmethod
    def _residual_value(
        book: float,
        roe: float,
        payout: float,
        discount_rate: float,
        years: int,
        fade: float,
    ) -> float | None:
        if book <= 0 or discount_rate <= 0:
            return None
        value = book
        current_book = book
        current_roe = roe
        for year in range(1, years + 1):
            residual = (current_roe - discount_rate) * current_book
            value += residual / (1.0 + discount_rate) ** year
            earnings = current_roe * current_book
            current_book += earnings * max(0.0, 1.0 - payout)
            current_roe = discount_rate + (
                current_roe - discount_rate
            ) * fade
        terminal_residual = (current_roe - discount_rate) * current_book
        if terminal_residual > 0:
            value += (
                terminal_residual / 0.06
                / (1.0 + discount_rate) ** years
            )
        return float(value) if np.isfinite(value) and value > 0 else None

    def _residual_income(
        self,
        snapshot: ValuationSnapshot,
        risk: SubjectiveRiskAdjustment,
    ) -> ExpertValuation:
        book = snapshot.book_value_per_share
        roe = snapshot.roe
        if book is None or book <= 0 or roe is None:
            return ExpertValuation(
                expert_id="residual_income",
                available=False,
                compatibility=0.0,
                safety_margin=0.15,
                diagnostics=("positive_book_value_and_roe_required",),
            )
        payout = float(np.clip(snapshot.payout_ratio or 0.35, 0.0, 1.0))
        low_rate, base_rate, high_rate, rate_kind = self._rates(snapshot)
        values = (
            self._residual_value(
                book, roe * 0.80, payout, low_rate, self.config.projection_years, 0.55
            ),
            self._residual_value(
                book, roe, payout, base_rate, self.config.projection_years, 0.70
            ),
            self._residual_value(
                book, roe * 1.10, payout, high_rate, self.config.projection_years, 0.78
            ),
        )
        compatibility = 0.35 + 0.35 * snapshot.earnings_stability
        if snapshot.free_cash_flow_per_share is None:
            compatibility += 0.20
        else:
            compatibility *= 0.45
        if snapshot.dividend_per_share is not None:
            compatibility += 0.10
        return ExpertValuation(
            expert_id="residual_income",
            available=all(value is not None for value in values),
            compatibility=float(np.clip(compatibility, 0.0, 1.0)),
            low=values[0],
            base=values[1],
            high=values[2],
            safety_margin=0.15,
            assumptions={
                "discount_rate_kind": rate_kind,
                "required_return_policy": self._required_return_policy(snapshot),
                "cash_flow_kind": "residual_income_from_book_value_and_roe",
                "book_value_per_share": book,
                "roe": roe,
                "payout_ratio": payout,
                "discount_rates": [low_rate, base_rate, high_rate],
            },
        )

    def _reverse_dcf_diagnostic(
        self,
        item: ExpertValuation,
        snapshot: ValuationSnapshot,
    ) -> dict[str, Any]:
        """Explain the growth priced by the market without revaluing the firm."""

        cash_per_share = _finite(item.assumptions.get("cash_per_share"))
        rates = item.assumptions.get("discount_rates", [])
        required_return = _finite(rates[1]) if len(rates) > 1 else None
        terminal_growth = _finite(item.assumptions.get("terminal_growth"))
        fundamental_growth = _finite(
            item.assumptions.get("explicit_growth")
        )
        implied_growth = _market_implied_growth(
            cash_per_share,
            snapshot.current_price,
            required_return,
            terminal_growth,
            self.config.projection_years,
        )
        growth_gap = (
            implied_growth - fundamental_growth
            if implied_growth is not None and fundamental_growth is not None
            else None
        )
        if implied_growth is None:
            interpretation = "market_implied_growth_not_solved"
        elif growth_gap is None:
            interpretation = "fundamental_growth_unavailable"
        elif growth_gap < -0.02:
            interpretation = "market_prices_lower_growth_than_fundamentals"
        elif growth_gap > 0.02:
            interpretation = "market_prices_higher_growth_than_fundamentals"
        else:
            interpretation = "market_and_fundamental_growth_are_close"
        return {
            "current_price": snapshot.current_price,
            "cash_per_share": cash_per_share,
            "required_return": required_return,
            "terminal_growth": terminal_growth,
            "market_implied_explicit_growth": implied_growth,
            "fundamental_explicit_growth": fundamental_growth,
            "growth_gap": growth_gap,
            "interpretation": interpretation,
            "feeds_intrinsic_value": False,
        }

    def estimate(
        self,
        snapshot: ValuationSnapshot,
        risk: SubjectiveRiskAdjustment | None = None,
    ) -> IntrinsicValueEstimate:
        selected_risk = risk or SubjectiveRiskAdjustment()
        if not selected_risk.active_on(snapshot.evaluation_date):
            inactive_reason = (
                "configured_subjective_risk_not_yet_effective"
                if selected_risk.effective_from is not None
                and snapshot.evaluation_date < selected_risk.effective_from
                else "configured_subjective_risk_expired"
            )
            selected_risk = SubjectiveRiskAdjustment(reason=inactive_reason)
        required_return_policy = self._required_return_policy(snapshot)
        experts = (
            self._dcf(snapshot, selected_risk),
            self._earnings_power(snapshot, selected_risk),
            self._ddm(snapshot, selected_risk),
            self._residual_income(snapshot, selected_risk),
        )
        available = [
            item
            for item in experts
            if item.available
            and item.compatibility >= self.config.minimum_expert_weight
            and all(
                value is not None and np.isfinite(value) and value > 0
                for value in (item.low, item.base, item.high)
            )
        ]
        if not available:
            return IntrinsicValueEstimate(
                symbol=snapshot.symbol,
                evaluation_date=snapshot.evaluation_date,
                market_date=snapshot.market_date,
                current_price=snapshot.current_price,
                fair_value_low=None,
                fair_value=None,
                fair_value_high=None,
                buy_price=None,
                margin_of_safety=None,
                fair_value_gap=None,
                confidence=0.0,
                gate={name: 0.0 for name in EXPERT_NAMES},
                market_implied_growth={name: None for name in EXPERT_NAMES},
                experts=experts,
                risk=selected_risk,
                required_return_policy=required_return_policy,
                reverse_dcf={},
                diagnostics=(*snapshot.diagnostics, "no_available_valuation_expert"),
            )
        available = sorted(
            available,
            key=lambda item: item.compatibility,
            reverse=True,
        )[: max(1, self.config.active_expert_count)]
        compatibility = np.asarray(
            [item.compatibility for item in available], dtype=np.float64
        )
        temperature = max(self.config.gate_temperature, 1e-6)
        compatibility = np.exp(
            (compatibility - compatibility.max()) / temperature
        )
        compatibility /= compatibility.sum()
        gate = {name: 0.0 for name in EXPERT_NAMES}
        implied_growth = {name: None for name in EXPERT_NAMES}
        reverse_dcf: dict[str, dict[str, Any]] = {}
        scenario_values: list[float] = []
        scenario_weights: list[float] = []
        margin = 0.0
        for item, weight in zip(available, compatibility):
            gate[item.expert_id] = float(weight)
            diagnostic = self._reverse_dcf_diagnostic(item, snapshot)
            reverse_dcf[item.expert_id] = diagnostic
            implied_growth[item.expert_id] = diagnostic[
                "market_implied_explicit_growth"
            ]
            margin += float(weight) * item.safety_margin
            for value, scenario_weight in zip(
                (item.low, item.base, item.high), (0.25, 0.50, 0.25)
            ):
                scenario_values.append(float(value))
                scenario_weights.append(float(weight) * scenario_weight)
        low = _weighted_quantile(
            scenario_values,
            scenario_weights,
            self.config.conservative_quantile,
        )
        fair = _weighted_quantile(scenario_values, scenario_weights, 0.50)
        high = _weighted_quantile(scenario_values, scenario_weights, 0.75)
        haircut = float(
            np.clip(
                self.config.default_subjective_haircut
                + selected_risk.expected_price_haircut(),
                0.0,
                0.60,
            )
        )
        buy_price = low * (1.0 - margin) * (1.0 - haircut)
        confidence = float(
            np.clip(
                0.35
                + 0.35 * max(item.compatibility for item in available)
                + 0.15 * snapshot.earnings_stability
                + 0.15 * snapshot.dividend_stability,
                0.0,
                1.0,
            )
        )
        if snapshot.financial_age_days is None:
            confidence *= 0.65
        elif snapshot.financial_age_days > self.config.financial_stale_days:
            confidence *= 0.70
        return IntrinsicValueEstimate(
            symbol=snapshot.symbol,
            evaluation_date=snapshot.evaluation_date,
            market_date=snapshot.market_date,
            current_price=snapshot.current_price,
            fair_value_low=low,
            fair_value=fair,
            fair_value_high=high,
            buy_price=buy_price,
            margin_of_safety=buy_price / snapshot.current_price - 1.0,
            fair_value_gap=fair / snapshot.current_price - 1.0,
            confidence=confidence,
            gate=gate,
            market_implied_growth=implied_growth,
            experts=experts,
            risk=selected_risk,
            required_return_policy=required_return_policy,
            reverse_dcf=reverse_dcf,
            diagnostics=snapshot.diagnostics,
        )
