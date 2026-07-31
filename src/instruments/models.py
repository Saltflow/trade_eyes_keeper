"""Data contracts for typed instruments and fundamental observations.

These models deliberately live outside the optimizer.  A current profile is a
reporting/audit object until a complete point-in-time history exists.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class InstrumentType(str, Enum):
    EQUITY = "equity"
    INDEX_ETF = "index_etf"
    SECTOR_ETF = "sector_etf"
    BOND_ETF = "bond_etf"
    COMMODITY_ETF = "commodity_etf"
    REIT = "reit"
    ACTIVE_FUND = "active_fund"
    UNKNOWN = "unknown"


class MetricStatus(str, Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    KNOWN_ZERO = "known_zero"
    NOT_APPLICABLE = "not_applicable"
    NOT_MEANINGFUL = "not_meaningful"
    MISSING = "missing"
    STALE = "stale"
    CONFLICT = "conflict"


class MetricValue(BaseModel):
    """One numeric value with enough context to audit its meaning."""

    value: Optional[float] = None
    status: MetricStatus = MetricStatus.MISSING
    as_of: Optional[date] = None
    published_at: Optional[date] = None
    period: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    currency: Optional[str] = None
    confidence: Optional[float] = None
    note: Optional[str] = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def missing(cls, note: str = "", source: str = "") -> "MetricValue":
        return cls(
            status=MetricStatus.MISSING,
            note=note or None,
            source=source or None,
        )

    @classmethod
    def not_applicable(cls, note: str = "") -> "MetricValue":
        return cls(status=MetricStatus.NOT_APPLICABLE, note=note or None)


class FinancialStatementSnapshot(BaseModel):
    """A statement version whose availability is controlled by published_at."""

    period_end: date
    published_at: Optional[date] = None
    period_type: str = "quarter"
    is_cumulative: bool = False
    currency: Optional[str] = None
    accounting_standard: Optional[str] = None
    source: str
    source_url: Optional[str] = None

    total_shares: Optional[float] = None
    common_shares_outstanding: Optional[float] = None
    diluted_average_shares: Optional[float] = None
    parent_equity: Optional[float] = None
    average_parent_equity: Optional[float] = None
    book_value_per_share: Optional[float] = None

    revenue: Optional[float] = None
    net_income_parent: Optional[float] = None
    adjusted_net_income_parent: Optional[float] = None
    basic_eps: Optional[float] = None
    diluted_eps: Optional[float] = None
    operating_cash_flow: Optional[float] = None
    free_cash_flow: Optional[float] = None
    reported_roe: Optional[float] = None

    diagnostics: list[str] = Field(default_factory=list)


class GrowthMetric(BaseModel):
    value_pct: Optional[float] = None
    status: MetricStatus = MetricStatus.MISSING
    interpretation: Optional[str] = None
    current_value: Optional[float] = None
    prior_value: Optional[float] = None
    current_period: Optional[date] = None
    prior_period: Optional[date] = None
    source: Optional[str] = None


class CompanyFundamentals(BaseModel):
    statements: list[FinancialStatementSnapshot] = Field(default_factory=list)
    total_shares: MetricValue = Field(default_factory=MetricValue)
    market_cap: MetricValue = Field(default_factory=MetricValue)
    free_float_market_cap: MetricValue = Field(default_factory=MetricValue)
    ttm_revenue: MetricValue = Field(default_factory=MetricValue)
    ttm_net_income_parent: MetricValue = Field(default_factory=MetricValue)
    ttm_adjusted_net_income_parent: MetricValue = Field(default_factory=MetricValue)
    book_value_per_share: MetricValue = Field(default_factory=MetricValue)
    pe_ttm: MetricValue = Field(default_factory=MetricValue)
    pb: MetricValue = Field(default_factory=MetricValue)
    roe_ttm: MetricValue = Field(default_factory=MetricValue)
    quoted_pe: MetricValue = Field(default_factory=MetricValue)
    quoted_pb: MetricValue = Field(default_factory=MetricValue)
    ttm_dividend_per_share: MetricValue = Field(default_factory=MetricValue)
    latest_dividend_per_share: MetricValue = Field(default_factory=MetricValue)
    dividend_yield: MetricValue = Field(default_factory=MetricValue)
    growth: dict[str, GrowthMetric] = Field(default_factory=dict)


class HoldingFundamentals(BaseModel):
    pe_ttm: MetricValue = Field(default_factory=MetricValue)
    pb: MetricValue = Field(default_factory=MetricValue)
    roe_ttm: MetricValue = Field(default_factory=MetricValue)
    dividend_yield: MetricValue = Field(default_factory=MetricValue)
    revenue_yoy: GrowthMetric = Field(default_factory=GrowthMetric)
    revenue_qoq: GrowthMetric = Field(default_factory=GrowthMetric)
    net_income_yoy: GrowthMetric = Field(default_factory=GrowthMetric)
    net_income_qoq: GrowthMetric = Field(default_factory=GrowthMetric)


class FundHolding(BaseModel):
    code: str
    name: Optional[str] = None
    market: Optional[str] = None
    currency: Optional[str] = None
    weight: float
    as_of: Optional[date] = None
    source: Optional[str] = None
    fundamentals: HoldingFundamentals = Field(default_factory=HoldingFundamentals)


class LookThroughMetric(BaseModel):
    value: MetricValue = Field(default_factory=MetricValue)
    covered_weight: float = 0.0


class FundProfile(BaseModel):
    issuer: Optional[str] = None
    tracking_index: Optional[str] = None
    asset_class: Optional[str] = None
    region_exposure: Optional[str] = None
    sector_exposure: Optional[str] = None
    aum: MetricValue = Field(default_factory=MetricValue)
    expense_ratio: MetricValue = Field(default_factory=MetricValue)
    nav_per_unit: MetricValue = Field(default_factory=MetricValue)
    premium_discount_rate: MetricValue = Field(default_factory=MetricValue)
    ttm_dividend_per_unit: MetricValue = Field(default_factory=MetricValue)
    dividend_yield: MetricValue = Field(default_factory=MetricValue)
    tracking_difference: MetricValue = Field(default_factory=MetricValue)
    duration: MetricValue = Field(default_factory=MetricValue)
    yield_to_maturity: MetricValue = Field(default_factory=MetricValue)
    p_nav: MetricValue = Field(default_factory=MetricValue)
    ttm_ffo: MetricValue = Field(default_factory=MetricValue)
    ffo_per_unit: MetricValue = Field(default_factory=MetricValue)
    p_ffo: MetricValue = Field(default_factory=MetricValue)
    distribution_yield: MetricValue = Field(default_factory=MetricValue)
    occupancy_rate: MetricValue = Field(default_factory=MetricValue)
    property_type: Optional[str] = None
    holdings_as_of: Optional[date] = None
    top_holdings: list[FundHolding] = Field(default_factory=list)
    top_holdings_weight: float = 0.0
    look_through: dict[str, LookThroughMetric] = Field(default_factory=dict)


class InstrumentProfile(BaseModel):
    code: str
    name: Optional[str] = None
    market: str
    exchange: Optional[str] = None
    currency: Optional[str] = None
    instrument_type: InstrumentType
    asset_class: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    latest_price: MetricValue = Field(default_factory=MetricValue)
    company: Optional[CompanyFundamentals] = None
    fund: Optional[FundProfile] = None
    applicable_metrics: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    source_attempts: list[dict[str, Any]] = Field(default_factory=list)
    completeness: dict[str, Any] = Field(default_factory=dict)


class InstrumentAuditReport(BaseModel):
    generated_at: datetime
    evaluation_date: date
    profiles: list[InstrumentProfile]
    summary: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "instrument-audit-1"
