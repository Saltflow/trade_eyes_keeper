"""Current, industry-relative valuation features for quantitative research.

The module deliberately separates three economic questions:

* company fundamentals describe capacity (earnings, cash flow and growth);
* reverse DCF describes the growth currently required by the market price;
* industry ranks make each quantity comparable with contemporaneous peers.

The available Baostock taxonomy is a dated *current* snapshot.  Consequently
this output is explicitly research-only and cannot be attached to a historical
walk-forward strategy panel until a dated taxonomy history is supplied.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

import numpy as np
from scipy.stats import rankdata

from .api import FundamentalPricingSnapshot
from .industry import IndustryRelativeSnapshot


CURRENT_VALUATION_FEATURE_CONTRACT = "current-valuation-industry-features-1"

VALUATION_FEATURE_NAMES = (
    "valuation:beta",
    "valuation:capm_cost_of_equity",
    "valuation:market_implied_growth_5y",
    "valuation:market_vs_fundamental_growth",
    "industry_relative:market_implied_growth_5y",
    "industry_relative:market_vs_fundamental_growth",
)


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _symbol(value: object) -> str:
    """Preserve six-digit A-share symbols when CSV readers infer integers."""

    text = str(value).strip()
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _centered_rank(values: np.ndarray) -> np.ndarray:
    if len(values) < 2:
        return np.zeros(len(values), dtype=np.float64)
    ranks = rankdata(values, method="average")
    return (ranks - (len(values) + 1.0) / 2.0) / ((len(values) - 1.0) / 2.0)


class CurrentValuationFeatureBuilder:
    """Merge a dated valuation report with an equally dated industry snapshot."""

    @staticmethod
    def _validate_inputs(
        snapshot: FundamentalPricingSnapshot,
        industry: IndustryRelativeSnapshot,
        rows: Mapping[str, Mapping[str, Any]],
    ) -> None:
        snapshot.validate()
        industry.validate()
        if snapshot.feature_date != industry.feature_date:
            raise ValueError("fundamental and industry snapshots must share a date")
        if snapshot.symbols != industry.symbols:
            raise ValueError("fundamental and industry snapshots must share symbol order")
        unknown = sorted(set(rows) - set(snapshot.symbols))
        if unknown:
            raise ValueError(f"valuation report contains unknown symbols: {unknown[:3]}")
        for symbol, row in rows.items():
            evaluation_date = _parse_date(row.get("evaluation_date"))
            if evaluation_date != snapshot.feature_date:
                raise ValueError(
                    f"{symbol}: valuation report date must equal feature_date"
                )

    @staticmethod
    def _rank_by_peer_scope(
        values: np.ndarray,
        observed: np.ndarray,
        industry: IndustryRelativeSnapshot,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Rank only among the peer universe selected by the industry builder."""

        result = np.zeros(len(values), dtype=np.float64)
        available = np.zeros(len(values), dtype=bool)
        codes = industry.industry_codes
        scopes = industry.peer_scopes
        for index, (code, scope) in enumerate(zip(codes, scopes)):
            if not observed[index] or code is None or scope is None:
                continue
            if scope == "industry":
                peers = np.asarray(
                    [item == code for item in codes], dtype=bool
                )
            elif scope == "sector_fallback":
                peers = np.asarray(
                    [
                        item is not None and item[:1] == code[:1]
                        for item in codes
                    ],
                    dtype=bool,
                )
            else:
                continue
            peers &= observed
            if int(peers.sum()) < 2:
                continue
            peer_indices = np.flatnonzero(peers)
            peer_ranks = _centered_rank(values[peer_indices])
            result[index] = peer_ranks[
                int(np.flatnonzero(peer_indices == index)[0])
            ]
            available[index] = True
        return result, available

    def build(
        self,
        snapshot: FundamentalPricingSnapshot,
        industry: IndustryRelativeSnapshot,
        reverse_dcf_rows: Iterable[Mapping[str, Any]],
    ) -> FundamentalPricingSnapshot:
        """Return an auditable current snapshot; never synthesize missing values."""

        records: dict[str, Mapping[str, Any]] = {}
        for row in reverse_dcf_rows:
            symbol = _symbol(row.get("symbol"))
            if not symbol:
                raise ValueError("valuation report row is missing symbol")
            if symbol in records:
                raise ValueError(f"valuation report has duplicate symbol: {symbol}")
            records[symbol] = row
        self._validate_inputs(snapshot, industry, records)

        values = np.zeros(
            (len(snapshot.symbols), len(VALUATION_FEATURE_NAMES)), dtype=np.float64
        )
        mask = np.zeros_like(values, dtype=bool)
        for index, symbol in enumerate(snapshot.symbols):
            row = records.get(symbol)
            if row is None:
                continue
            beta = _finite(row.get("beta"))
            capm = _finite(row.get("capm_cost_of_equity"))
            status = str(row.get("market_cost_growth_status") or "")
            implied = (
                _finite(row.get("market_implied_growth_5y"))
                if status == "solved"
                else None
            )
            fundamental = _finite(row.get("fundamental_growth"))
            gap = (
                implied - fundamental
                if implied is not None and fundamental is not None
                else None
            )
            for feature_index, value in enumerate((beta, capm, implied, gap)):
                if value is not None:
                    values[index, feature_index] = value
                    mask[index, feature_index] = True

        implied_rank, implied_rank_mask = self._rank_by_peer_scope(
            values[:, 2], mask[:, 2], industry
        )
        gap_rank, gap_rank_mask = self._rank_by_peer_scope(
            values[:, 3], mask[:, 3], industry
        )
        values[:, 4] = implied_rank
        values[:, 5] = gap_rank
        mask[:, 4] = implied_rank_mask
        mask[:, 5] = gap_rank_mask
        return FundamentalPricingSnapshot(
            feature_date=snapshot.feature_date,
            symbols=snapshot.symbols,
            feature_names=VALUATION_FEATURE_NAMES,
            values=values,
            availability_mask=mask,
            metadata={
                "contract": CURRENT_VALUATION_FEATURE_CONTRACT,
                "mode": "current_snapshot_research_only",
                "feature_semantics": {
                    "company_capacity": list(snapshot.feature_names),
                    "market_expectations": [
                        "valuation:market_implied_growth_5y",
                        "valuation:market_vs_fundamental_growth",
                    ],
                    "market_risk": [
                        "valuation:beta",
                        "valuation:capm_cost_of_equity",
                    ],
                    "peer_relative_expectations": list(
                        VALUATION_FEATURE_NAMES[4:]
                    ),
                },
                "discounting": (
                    "market-implied growth is reverse-DCF at CAPM cost of "
                    "equity for an FCFE proxy; investor hurdle is excluded"
                ),
                "historical_walk_forward_eligible": False,
                "current_taxonomy_must_not_be_backfilled": True,
                "industry_feature_date": industry.feature_date.isoformat(),
            },
        ).validate()
