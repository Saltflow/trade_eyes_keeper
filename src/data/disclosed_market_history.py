"""Price sources paired with independently disclosed corporate actions."""

from datetime import date
from pathlib import Path

import pandas as pd

from src.instruments.classifier import detect_market

from .listing_dates import ListingDateStore
from .market_history import PriceHistoryBundle
from .price_adjustments import apply_disclosed_adjustments


class DisclosedMarketHistoryProvider:
    def __init__(self, config: dict):
        self.config = config

    def fetch(self, code: str, start: date, end: date) -> PriceHistoryBundle:
        market = detect_market(code)
        if market == "us":
            from .us_market_history import USDisclosedMarketHistoryProvider

            return USDisclosedMarketHistoryProvider(self.config).fetch(code, start, end)

        from .tencent_market_history import TencentMarketHistoryProvider

        bundle = TencentMarketHistoryProvider(self.config).fetch(code, start, end)
        settings = self.config.get("point_in_time_data", {}) or {}
        market_settings = settings.get("market_history", {}) or {}
        tolerance = int(market_settings.get("coverage_tolerance_days", 31))
        first = pd.Timestamp(bundle.prices["date"].min()).date()
        action_start = start
        if (first - start).days > tolerance:
            root = Path(settings.get("output_dir", "data/point_in_time"))
            evidence = ListingDateStore(root).resolve(
                code, **({"listing_year": first.year} if market == "hk" else {})
            )
            if evidence.listing_date <= start or first > evidence.listing_date:
                raise ValueError(
                    f"{code}: available history {first} conflicts with official "
                    f"listing date {evidence.listing_date}"
                )
            # Some fund feeds include pre-listing NAV observations. They are not
            # exchange-tradable prices and must never enter the backtest.
            listed_rows = bundle.prices.loc[
                pd.to_datetime(bundle.prices["date"]).dt.date >= evidence.listing_date
            ].copy()
            if (
                listed_rows.empty
                or pd.Timestamp(listed_rows["date"].min()).date()
                != evidence.listing_date
            ):
                raise ValueError(
                    f"{code}: no exchange price on official listing date "
                    f"{evidence.listing_date}"
                )
            bundle.prices = listed_rows.reset_index(drop=True)
            bundle.listing_evidence = evidence.as_dict()
            action_start = evidence.listing_date
            bundle.diagnostics.append(
                f"official_listing_date:{evidence.listing_date.isoformat()}"
            )
        if market == "hk":
            from .hk_corporate_actions import HkCorporateActionProvider

            actions = HkCorporateActionProvider(self.config).fetch(
                code, action_start, end
            )
            evidence = "etnet_complete_history_and_hkex_implementation_notices"
        elif str(code).startswith("5"):
            from .fund_corporate_actions import SseFundCorporateActionProvider

            evidence_dir = (
                Path(settings.get("output_dir", "data/point_in_time"))
                / "action_evidence"
                / "sse"
            )
            actions = SseFundCorporateActionProvider(
                evidence_dir=evidence_dir
            ).fetch_actions(code, action_start, end, market_prices=bundle.prices)
            evidence = "complete_sse_fund_index_and_implementation_notices"
        else:
            raise ValueError(f"No verified corporate action source for {code}")
        if any(action.action_type == "in_kind_distribution" for action in actions):
            from dataclasses import replace

            from .in_kind_estimation import resolve_in_kind_estimates
            from .market_history import PointInTimeMarketStore, corporate_action_issues

            def load_external(asset, first, last):
                source = self.fetch(asset, first, last)
                if corporate_action_issues(source.actions):
                    raise ValueError(
                        "external asset corporate-action evidence is unresolved"
                    )
                PointInTimeMarketStore(
                    settings.get("output_dir", "data/point_in_time")
                ).write(source)
                return source

            # Estimates are an explicitly enabled account policy. Statements
            # retain their source facts; the resolver records assumptions in
            # separate fields before the common execution-readiness gate.
            bundle = resolve_in_kind_estimates(
                replace(bundle, actions=actions),
                self.config,
                load_external,
            )
            actions = bundle.actions
        return apply_disclosed_adjustments(bundle, actions, evidence=evidence)
