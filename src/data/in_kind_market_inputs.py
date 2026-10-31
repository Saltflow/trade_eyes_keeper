"""Prepare independently sourced raw prices for distributed external shares."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..backtest.execution import build_corporate_action_schedule
from ..backtest.in_kind import has_share_entitlement
from ..instruments.classifier import detect_market
from .market_history import PriceHistoryBundle, corporate_action_issues


def external_asset_frame(
    bundle: PriceHistoryBundle,
    parent: PriceHistoryBundle,
    dates,
    config: dict,
) -> pd.DataFrame:
    """Use raw quotes and explicit actions without same-day foreign-close leakage.

    The initial contract requires the same trading market. A USD ADS delivered
    into an HK account needs a separate quote-availability/execution clock;
    matching calendar dates alone cannot prove the US close was already known.
    """
    bundle = bundle.validate()
    if detect_market(bundle.code) != detect_market(parent.code):
        raise ValueError("in-kind cross-market quote availability is unverified")
    currencies = (bundle.currency, parent.currency)
    if not all(currencies):
        raise ValueError("in-kind external and parent quote currencies are required")
    if currencies[0] != currencies[1]:
        raise ValueError("in-kind cross-currency valuation requires causal FX evidence")
    issues = corporate_action_issues(bundle.actions)
    if issues:
        raise ValueError(
            "external asset corporate action evidence: " + "; ".join(issues)
        )
    if any(action.action_type == "in_kind_distribution" for action in bundle.actions):
        raise ValueError(
            "nested in-kind external distributions require their own ledger"
        )
    source = bundle.prices.sort_values("date").copy()
    target_dates = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize()
    quote_dates = pd.DatetimeIndex(pd.to_datetime(source["date"])).normalize()
    if quote_dates.duplicated().any() or target_dates.duplicated().any():
        raise ValueError("in-kind quote dates must be unique")
    if len(target_dates) == 0:
        raise ValueError("in-kind valuation window is empty")
    if quote_dates.min() > target_dates.min() or quote_dates.max() < target_dates.max():
        raise ValueError("in-kind external raw quote coverage is incomplete")
    source.index = quote_dates
    # Same-market suspended sessions carry a last known valuation only. They
    # cannot be used as a fictitious sale at a forward-filled low price.
    calendar = quote_dates.union(target_dates)
    aligned = source.reindex(calendar)
    frame = aligned.reindex(target_dates).copy()
    frame["raw_close"] = aligned["raw_close"].ffill().reindex(target_dates)
    frame["raw_low"] = aligned["raw_low"].ffill().reindex(target_dates)
    frame["tradable"] = frame["tradable"].eq(True)
    values = frame[["raw_close", "raw_low"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("in-kind external raw prices must be finite and positive")
    action_schedule = build_corporate_action_schedule(
        bundle.actions, target_dates, [bundle.code]
    )
    # These are gross native-currency cash flows. The execution account applies
    # its withholding profile once; this data layer never subtracts taxes.
    frame["external_cash_dividend"] = action_schedule.gross_cash_dividends[:, 0]
    frame["external_share_multiplier"] = action_schedule.share_multipliers[:, 0]
    if np.any(action_schedule.share_rounding_up):
        raise ValueError(
            "external share rounding requires an explicit entitlement rule"
        )
    frame["fx_rate"] = 1.0  # Proven identical quote currencies, not an FX fallback.
    frame["date"] = target_dates
    return frame[
        [
            "date",
            "raw_close",
            "raw_low",
            "fx_rate",
            "tradable",
            "external_cash_dividend",
            "external_share_multiplier",
        ]
    ].reset_index(drop=True)


def attach_in_kind_market_inputs(
    bundle: PriceHistoryBundle,
    config: dict,
    store,
    provider,
    end: date,
) -> PriceHistoryBundle:
    """Resolve external price dependencies only for complete executable terms."""
    from .in_kind_estimation import validate_cached_in_kind_estimates

    validate_cached_in_kind_estimates(bundle, config)
    actions = sorted(
        [
            action
            for action in bundle.actions
            if action.action_type == "in_kind_distribution" and action.ex_date <= end
        ],
        key=lambda action: action.ex_date,
    )
    if not actions or corporate_action_issues(actions):
        # The existing readiness gate returns the precise missing evidence. Do
        # not fetch unrelated prices to disguise missing settlement terms.
        return bundle
    external = dict(bundle.external_prices)
    from .in_kind_distributions import in_kind_distribution_issues

    for action in actions:
        if in_kind_distribution_issues(action):
            continue
        if not has_share_entitlement(action):
            # A Stock Connect cash election has no US ADS position or foreign
            # execution clock. Its cash leg is valued by the account policy.
            continue
        terms = action.in_kind_terms
        asset = terms.asset_code
        if terms.asset_currency != bundle.currency:
            raise ValueError(
                "in-kind external currency differs from parent quote currency"
            )
        required_end = end
        if terms.disposal_policy == "sell_on_delivery":
            dates_needed = [
                getattr(terms, name, None)
                for name in (
                    "execution_delivery_date",
                    "execution_tradable_from_date",
                    "execution_cash_in_lieu_payment_date",
                )
            ]
            dates_needed = [value for value in dates_needed if value is not None]
            if dates_needed:
                required_end = min(end, max(dates_needed))
        # Prices before the ex-date are unnecessary for the entitlement; retain
        # enough prior source observations for a known valuation on its date.
        # Serialized numerical frames do not independently prove currency or
        # corporate-action coverage. Rebuild from the strict source bundle,
        # reusing its local cache before considering a network request.
        source = store.read(asset, config=config)
        if source is None or (
            pd.Timestamp(source.prices["date"].min()).date() > action.ex_date
            or pd.Timestamp(source.prices["date"].max()).date() < required_end
        ):
            source = provider.fetch(
                asset, action.ex_date - timedelta(days=10), required_end
            )
        if source.currency != terms.asset_currency:
            raise ValueError("in-kind raw quote currency conflicts with declaration")
        dates = bundle.prices.loc[
            (pd.to_datetime(bundle.prices["date"]).dt.date >= action.ex_date)
            & (pd.to_datetime(bundle.prices["date"]).dt.date <= required_end),
            "date",
        ]
        external[asset] = external_asset_frame(source, bundle, dates, config)
    return replace(bundle, external_prices=external)
