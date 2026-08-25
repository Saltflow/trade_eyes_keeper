"""Build quarterly, disclosure-dated inputs for fundamental pricing models."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle
from src.instruments.calculations import (
    derive_company_fundamentals,
    to_standalone_quarters,
)
from src.instruments.classifier import detect_market
from src.instruments.models import MetricStatus, MetricValue
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    adjust_statement_shares,
)

from .api import (
    PRICING_FEATURE_NAMES,
    FundamentalPricingDataset,
    FundamentalPricingSnapshot,
)


def _number(metric) -> float | None:
    value = getattr(metric, "value", None)
    if value is None or not np.isfinite(value):
        return None
    return float(value)


def _growth(company, name: str) -> float | None:
    item = company.growth.get(name)
    if item is None or item.status != MetricStatus.DERIVED:
        return None
    value = item.value_pct
    return float(value) if value is not None and np.isfinite(value) else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if (
        numerator is None
        or denominator is None
        or not np.isfinite(numerator)
        or not np.isfinite(denominator)
        or abs(denominator) < 1e-12
    ):
        return None
    result = numerator / denominator
    return float(result) if np.isfinite(result) else None


def _cagr(
    statements,
    field: str,
    years: int = 3,
) -> float | None:
    annuals = [
        item
        for item in statements
        if item.period_type in {"year", "annual", "12M"}
        and getattr(item, field) is not None
    ]
    if len(annuals) < 2:
        return None
    current = annuals[-1]
    candidates = [
        item
        for item in annuals[:-1]
        if 365 * (years - 1) <= (current.period_end - item.period_end).days
        <= 366 * (years + 1)
    ]
    if not candidates:
        return None
    prior = min(
        candidates,
        key=lambda item: abs(
            (current.period_end - item.period_end).days - 365.25 * years
        ),
    )
    current_value = getattr(current, field)
    prior_value = getattr(prior, field)
    elapsed = (current.period_end - prior.period_end).days / 365.25
    if current_value is None or prior_value is None or elapsed <= 0:
        return None
    if current_value <= 0 or prior_value <= 0:
        return None
    return float((current_value / prior_value) ** (1.0 / elapsed) - 1.0)


def _growth_stability(statements, field: str) -> float | None:
    quarters = [
        item
        for item in to_standalone_quarters(statements)
        if item.period_type in {"quarter", "3M"}
    ]
    by_period = {item.period_end: item for item in quarters}
    growth = []
    for item in quarters[-12:]:
        prior_date = date(item.period_end.year - 1, item.period_end.month, item.period_end.day)
        prior = by_period.get(prior_date)
        current_value = getattr(item, field)
        prior_value = getattr(prior, field) if prior is not None else None
        if current_value is None or prior_value is None or abs(prior_value) < 1e-12:
            continue
        value = current_value / prior_value - 1.0
        if np.isfinite(value):
            growth.append(float(np.clip(value, -5.0, 5.0)))
    if len(growth) < 3:
        annuals = [
            item
            for item in statements
            if item.period_type in {"year", "annual", "12M"}
        ][-6:]
        annual_growth = []
        for previous, current in zip(annuals, annuals[1:]):
            gap = (current.period_end - previous.period_end).days
            current_value = getattr(current, field)
            prior_value = getattr(previous, field)
            if (
                not 330 <= gap <= 400
                or current_value is None
                or prior_value is None
                or abs(prior_value) < 1e-12
            ):
                continue
            value = current_value / prior_value - 1.0
            if np.isfinite(value):
                annual_growth.append(float(np.clip(value, -5.0, 5.0)))
        growth = annual_growth
    if len(growth) < 3:
        return None
    median = float(np.median(growth))
    mad = float(np.median(np.abs(np.asarray(growth) - median)))
    return -mad


def _ttm_or_annual_value(statements, field: str) -> float | None:
    """Return a complete trailing-four-quarter value or a disclosed annual value."""

    quarters = [
        item
        for item in to_standalone_quarters(statements)
        if item.period_type in {"quarter", "3M"}
    ]
    if len(quarters) >= 4:
        selected = quarters[-4:]
        gaps = [
            (current.period_end - previous.period_end).days
            for previous, current in zip(selected, selected[1:])
        ]
        values = [getattr(item, field) for item in selected]
        if all(55 <= gap <= 125 for gap in gaps) and all(
            value is not None for value in values
        ):
            return float(sum(values))

    annuals = [
        item
        for item in statements
        if item.period_type in {"year", "annual", "12M"}
        and getattr(item, field) is not None
    ]
    return float(getattr(annuals[-1], field)) if annuals else None


def _ttm_dividend_yield(
    bundle: PriceHistoryBundle,
    evaluation_date: date,
    price: float,
) -> float | None:
    start = evaluation_date - timedelta(days=365)
    dividends = [
        float(item.cash_per_share)
        for item in bundle.actions
        if item.cash_per_share is not None
        and start < item.ex_date <= evaluation_date
        and (item.published_at is None or item.published_at <= evaluation_date)
    ]
    if not dividends or price <= 0:
        return None
    return float(sum(dividends) / price)


class QuarterlyPricingDatasetBuilder:
    """Construct one no-lookahead company observation per calendar quarter."""

    def __init__(
        self,
        root: str | Path,
        *,
        forward_trading_days: int = 63,
        market: str = "a_share",
    ):
        self.root = Path(root)
        self.forward_trading_days = max(1, int(forward_trading_days))
        self.market = market
        self.market_store = PointInTimeMarketStore(self.root)
        self.fundamental_store = PointInTimeFundamentalStore(self.root)

    def available_symbols(self, symbols: Iterable[str] | None = None) -> list[str]:
        requested = {str(item) for item in symbols or []}
        market_codes = {
            path.stem for path in (self.root / "market").glob("*.csv")
        }
        statement_codes = {
            path.name.split(".statements.json")[0]
            for path in (self.root / "fundamentals").glob("*.statements.json")
        }
        result = sorted(market_codes & statement_codes)
        return [
            code
            for code in result
            if detect_market(code) == self.market
            and (not requested or code in requested)
        ]

    @staticmethod
    def _quarter_anchors(bundles: dict[str, PriceHistoryBundle]) -> list[date]:
        by_quarter: dict[tuple[int, int], list[date]] = defaultdict(list)
        for bundle in bundles.values():
            for timestamp in pd.to_datetime(bundle.prices["date"]):
                quarter = (timestamp.month - 1) // 3 + 1
                by_quarter[(timestamp.year, quarter)].append(timestamp.date())
        return sorted(max(items) for items in by_quarter.values())

    @staticmethod
    def _market_row(bundle: PriceHistoryBundle, anchor: date) -> tuple[int, pd.Series] | None:
        frame = bundle.prices
        dates = pd.to_datetime(frame["date"]).dt.date.to_numpy()
        index = int(np.searchsorted(dates, anchor, side="right") - 1)
        if index < 0:
            return None
        row = frame.iloc[index]
        actual = pd.Timestamp(row["date"]).date()
        if (anchor - actual).days > 10:
            return None
        return index, row

    def _features(
        self,
        code: str,
        bundle: PriceHistoryBundle,
        evaluation_date: date,
        raw_price: float,
    ) -> dict[str, float | None]:
        statements = self.fundamental_store.as_of(code, evaluation_date)
        if not statements:
            return {name: None for name in PRICING_FEATURE_NAMES}
        statements = adjust_statement_shares(
            statements, bundle.actions, evaluation_date
        )
        company = derive_company_fundamentals(
            statements,
            current_price=MetricValue(
                value=raw_price,
                status=MetricStatus.OBSERVED,
                as_of=evaluation_date,
                source="point_in_time_raw_close",
            ),
            evaluation_date=evaluation_date,
        )
        shares = _number(company.total_shares)
        market_cap = raw_price * shares if shares is not None else None
        revenue = _number(company.ttm_revenue)
        income = _number(company.ttm_net_income_parent)
        adjusted_income = _number(company.ttm_adjusted_net_income_parent)
        fcf = _number(company.ttm_free_cash_flow)
        pe = _number(company.pe_ttm)
        pb = _number(company.pb)
        latest_publication = max(
            item.published_at for item in statements if item.published_at is not None
        )
        capex = _ttm_or_annual_value(statements, "capital_expenditures")
        return {
            "earnings_yield": 1.0 / pe if pe is not None and pe > 0 else None,
            "book_yield": 1.0 / pb if pb is not None and pb > 0 else None,
            "fcf_yield": _safe_ratio(fcf, market_cap),
            "dividend_yield": _ttm_dividend_yield(
                bundle, evaluation_date, raw_price
            ),
            "roe_ttm": _number(company.roe_ttm),
            "net_margin": _safe_ratio(income, revenue),
            "adjusted_margin": _safe_ratio(adjusted_income, revenue),
            "fcf_margin": _safe_ratio(fcf, revenue),
            "cash_conversion": _safe_ratio(fcf, income),
            "capex_intensity": _safe_ratio(capex, revenue),
            "revenue_yoy": _growth(company, "revenue_yoy"),
            "revenue_qoq": _growth(company, "revenue_qoq"),
            "net_income_yoy": _growth(company, "net_income_yoy"),
            "net_income_qoq": _growth(company, "net_income_qoq"),
            "revenue_cagr_3y": _cagr(statements, "revenue"),
            "net_income_cagr_3y": _cagr(statements, "net_income_parent"),
            "revenue_growth_stability": _growth_stability(statements, "revenue"),
            "income_growth_stability": _growth_stability(
                statements, "net_income_parent"
            ),
            "financial_age_days": float(
                (evaluation_date - latest_publication).days
            ),
        }

    def build(
        self,
        *,
        symbols: Iterable[str] | None = None,
    ) -> FundamentalPricingDataset:
        selected = self.available_symbols(symbols)
        bundles = {
            code: bundle
            for code in selected
            if (bundle := self.market_store.read(code)) is not None
        }
        anchors = self._quarter_anchors(bundles)
        rows = []
        for anchor in anchors:
            quarter_rows = []
            for code, bundle in bundles.items():
                selected_row = self._market_row(bundle, anchor)
                if selected_row is None:
                    continue
                index, market_row = selected_row
                future_index = index + self.forward_trading_days
                if future_index >= len(bundle.prices):
                    continue
                raw_price = float(market_row["raw_close"])
                start_qfq = float(market_row["qfq_close"])
                future = bundle.prices.iloc[future_index]
                end_qfq = float(future["qfq_close"])
                if not all(np.isfinite(value) and value > 0 for value in (
                    raw_price, start_qfq, end_qfq
                )):
                    continue
                features = self._features(code, bundle, anchor, raw_price)
                if not any(value is not None for value in features.values()):
                    continue
                quarter_rows.append(
                    {
                        "date": anchor,
                        "label_end": pd.Timestamp(future["date"]).date(),
                        "symbol": code,
                        "features": features,
                        "return": end_qfq / start_qfq - 1.0,
                    }
                )
            if len(quarter_rows) < 2:
                continue
            mean_return = float(np.mean([item["return"] for item in quarter_rows]))
            for item in quarter_rows:
                item["excess"] = item["return"] - mean_return
                rows.append(item)

        values = np.full(
            (len(rows), len(PRICING_FEATURE_NAMES)), np.nan, dtype=np.float64
        )
        mask = np.zeros_like(values, dtype=bool)
        for row_index, row in enumerate(rows):
            for feature_index, name in enumerate(PRICING_FEATURE_NAMES):
                value = row["features"][name]
                if value is not None and np.isfinite(value):
                    values[row_index, feature_index] = float(value)
                    mask[row_index, feature_index] = True
        dataset = FundamentalPricingDataset(
            feature_dates=tuple(item["date"] for item in rows),
            label_end_dates=tuple(item["label_end"] for item in rows),
            symbols=tuple(item["symbol"] for item in rows),
            feature_names=PRICING_FEATURE_NAMES,
            values=values,
            availability_mask=mask,
            forward_returns=np.asarray(
                [item["return"] for item in rows], dtype=np.float64
            ),
            excess_returns=np.asarray(
                [item["excess"] for item in rows], dtype=np.float64
            ),
            metadata={
                "contract": "quarterly-fundamental-pricing-data-1",
                "root": str(self.root.resolve()),
                "market": self.market,
                "forward_trading_days": self.forward_trading_days,
                "available_symbols": selected,
                "quarter_count": len({item["date"] for item in rows}),
            },
        )
        return dataset.validate()

    def build_latest(
        self,
        *,
        symbols: Iterable[str] | None = None,
    ) -> FundamentalPricingSnapshot:
        """Build the newest unlabelled snapshot without requiring future prices."""

        selected = self.available_symbols(symbols)
        bundles = {
            code: bundle
            for code in selected
            if (bundle := self.market_store.read(code)) is not None
        }
        if not bundles:
            raise ValueError("no point-in-time market bundles are available")
        anchor = max(
            pd.to_datetime(bundle.prices["date"]).max().date()
            for bundle in bundles.values()
        )
        rows: list[tuple[str, dict[str, float | None]]] = []
        actual_dates: dict[str, str] = {}
        for code, bundle in bundles.items():
            selected_row = self._market_row(bundle, anchor)
            if selected_row is None:
                continue
            _, market_row = selected_row
            raw_price = float(market_row["raw_close"])
            if not np.isfinite(raw_price) or raw_price <= 0:
                continue
            features = self._features(code, bundle, anchor, raw_price)
            if not any(value is not None for value in features.values()):
                continue
            rows.append((code, features))
            actual_dates[code] = pd.Timestamp(market_row["date"]).date().isoformat()

        values = np.full(
            (len(rows), len(PRICING_FEATURE_NAMES)), np.nan, dtype=np.float64
        )
        mask = np.zeros_like(values, dtype=bool)
        for row_index, (_, features) in enumerate(rows):
            for feature_index, name in enumerate(PRICING_FEATURE_NAMES):
                value = features[name]
                if value is not None and np.isfinite(value):
                    values[row_index, feature_index] = float(value)
                    mask[row_index, feature_index] = True
        return FundamentalPricingSnapshot(
            feature_date=anchor,
            symbols=tuple(code for code, _ in rows),
            feature_names=PRICING_FEATURE_NAMES,
            values=values,
            availability_mask=mask,
            metadata={
                "contract": "fundamental-pricing-snapshot-1",
                "root": str(self.root.resolve()),
                "market": self.market,
                "actual_market_dates": actual_dates,
            },
        ).validate()
