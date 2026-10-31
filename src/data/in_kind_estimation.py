"""Explicit Stock Connect settlement assumptions, separate from actual facts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml

from .market_history import PriceHistoryBundle


def _session_after(day: date, lag: int) -> date:
    import exchange_calendars

    if isinstance(lag, bool) or int(lag) != lag or not 0 <= lag <= 30:
        raise ValueError("in-kind settlement lag must be 0..30 trading sessions")
    calendar = exchange_calendars.get_calendar(
        "XHKG", start=str(day - timedelta(days=366)), end=str(day + timedelta(days=90))
    )
    sessions = calendar.sessions[calendar.sessions >= pd.Timestamp(day)]
    if len(sessions) <= lag:
        raise ValueError("in-kind settlement calendar does not cover the assumed lag")
    return sessions[int(lag)].date()


def _quote_at(source: PriceHistoryBundle, day: date, column: str) -> float:
    prices = source.prices.loc[pd.to_datetime(source.prices["date"]).dt.date <= day]
    if prices.empty:
        raise ValueError(f"{source.code}: no causal {column} on or before {day}")
    value = float(prices.sort_values("date").iloc[-1][column])
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"{source.code}: invalid raw quote for settlement estimate")
    return value


def _resolve_current_policy(
    config: dict,
) -> tuple[dict, float, float, float, str] | None:
    """Resolve and hash the exact policy shared by creation and cache checks."""
    settings = (
        (config.get("point_in_time_data", {}) or {}).get("market_history", {}) or {}
    ).get("in_kind_estimation", {}) or {}
    enabled = settings.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("in-kind estimation enabled must be a boolean")  # noqa: TRY004 -- config value validation
    if enabled is not True:
        return None
    path = Path(__file__).resolve().parents[2] / "config" / "in_kind_estimation.yaml"
    policy = yaml.safe_load(path.read_text(encoding="utf-8"))
    overrides = {key: value for key, value in settings.items() if key != "enabled"}
    if set(overrides) - set(policy):
        raise ValueError("unknown in-kind estimation policy settings")
    policy.update(overrides)
    if policy.get("profile") != "stock_connect_individual":
        raise ValueError("unsupported in-kind estimation account profile")
    if policy.get("disposal_policy") not in {"hold", "sell_on_delivery"}:
        raise ValueError("unsupported in-kind estimated disposal policy")
    from ..search.config import get_market_optimizer_config

    execution = get_market_optimizer_config("hk", config).execution
    tax = float(execution.withholding_rates.get("hk", policy["withholding_rate"]))
    commission = float(execution.commission_rate)
    if not 0 <= tax < 1 or not 0 <= commission < 1:
        raise ValueError("invalid in-kind withholding or execution commission")
    for key in ("additional_cash_collection_fee", "issuer_fractional_expense_rate"):
        value = float(policy[key])
        if not np.isfinite(value) or value < 0:
            raise ValueError("invalid in-kind fee assumption")
    expense = float(policy["issuer_fractional_expense_rate"])
    if expense >= 1:
        raise ValueError("issuer fractional expense rate must be below one")
    identity = {
        **policy,
        "withholding_rate": tax,
        "execution_commission_rate": commission,
        "calendar_version": version("exchange-calendars"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    policy_id = f"stock_connect_estimate_v1:{digest}"
    return policy, tax, commission, expense, policy_id


def current_in_kind_estimation_policy_id(config: dict) -> str | None:
    """Return the enabled policy identity, or None for explicitly disabled use."""
    resolved = _resolve_current_policy(config)
    return resolved[-1] if resolved is not None else None


def validate_cached_in_kind_estimates(
    bundle: PriceHistoryBundle, config: dict
) -> PriceHistoryBundle:
    """Reject cached estimates after opt-out or any execution-policy change.

    This is a preparation-layer gate. Reading historical source files remains
    possible, but a cache cannot authorize its own estimated execution policy.
    """
    estimated = [
        action
        for action in bundle.actions
        if action.in_kind_terms is not None
        and action.in_kind_terms.settlement_basis == "stock_connect_estimate"
    ]
    if not estimated:
        return bundle
    current = current_in_kind_estimation_policy_id(config)
    if current is None:
        raise ValueError("in-kind estimated policy disabled; reprepare market inputs")
    if any(
        action.in_kind_terms.estimation_policy_id != current for action in estimated
    ):
        raise ValueError("in-kind estimated policy changed; reprepare market inputs")
    return bundle


def resolve_in_kind_estimates(
    bundle: PriceHistoryBundle,
    config: dict,
    external_loader: Callable[[str, date, date], PriceHistoryBundle],
) -> PriceHistoryBundle:
    """Resolve authorized estimates from source terms and real raw quotes.

    Disabled unless the application explicitly enables this profile. No
    estimate is written into an actual-delivery, actual-settlement or evidence
    hash field. Source qfq and account execution assumptions stay independent.
    """
    resolved = _resolve_current_policy(config)
    if resolved is None:
        return bundle
    policy, tax, commission, expense, policy_id = resolved
    actions = deepcopy(bundle.actions)
    external = dict(bundle.external_prices)
    end = pd.Timestamp(bundle.prices["date"].max()).date()
    sources = {}
    for action in actions:
        terms = action.in_kind_terms
        if action.action_type != "in_kind_distribution" or terms is None:
            continue
        from .in_kind_distributions import (
            confirmed_reconciliation_pending,
            in_kind_distribution_issues,
        )

        if confirmed_reconciliation_pending(action):
            # Explicit actual reconciliation has priority over an enabled
            # estimation profile. Missing receipts must remain a visible gate.
            continue
        if terms.settlement_basis == "confirmed" and not in_kind_distribution_issues(
            action
        ):
            continue
        if not terms.asset_code or not terms.asset_currency or not terms.record_date:
            continue
        base = (
            terms.share_certificate_dispatch_date
            or terms.expected_delivery_date
            or action.payable_date
        )
        if base is None:
            terms.diagnostics.append(
                "settlement_estimate_requires_disclosed_dispatch_or_expected_date"
            )
            continue
        terms.settlement_basis = "stock_connect_estimate"
        terms.estimation_policy_id = policy_id
        terms.estimation_parent_currency = bundle.currency
        terms.estimation_sources = list(policy["sources"])
        terms.estimated_minimum_cash_payment = 0.0
        terms.estimation_assumptions = [
            "Mainland individual/securities-fund Stock Connect account; settlement is estimated, not actual receipt.",
            f"HK trading-session delivery lag={policy['delivery_lag_sessions']}; cash lag={policy['cash_lag_sessions']}.",
            f"Account dividend withholding={tax}; delivered-share tax base is the raw closing value on estimated delivery.",
            f"Whole-share sale uses the existing aggregate execution commission={commission}; this is a model, not a statutory fee schedule.",
            "No additional 0.12% HKSCC cash-collection fee is applied to China Connect; portfolio-level fees are not charged again per event.",
            f"Unobserved issuer fractional-disposal expense rate is explicitly estimated as {expense}; cash-collection fixed estimate={policy['additional_cash_collection_fee']}.",
            "Registered-holder cash thresholds are not applied per Stock Connect beneficiary; nominee pro-rata redistribution and zero further issuer adjustment are explicit estimates.",
        ]
        if terms.allocation_rule == "tiered_ads_election":
            if policy["foreign_security_election"] != "cash":
                raise ValueError(
                    "Stock Connect foreign-security distribution requires an explicit cash policy"
                )
            if not isinstance(policy["issuer_qualified_assumption"], bool):
                raise ValueError("issuer qualification assumption must be boolean")
            terms.eligible = policy["issuer_qualified_assumption"]
            terms.account_election = "cash"
            if policy["nominee_cash_model"] != "pro_rata_declared_cash_election":
                raise ValueError("unsupported nominee cash allocation model")
            if (
                not terms.declared_cash_election_per_unit
                or not terms.minimum_parent_shares
            ):
                raise ValueError(
                    "nominee cash model requires declared cash election terms"
                )
            terms.estimated_nominee_cash_per_parent_share = (
                terms.declared_cash_election_per_unit / terms.minimum_parent_shares
                if terms.eligible
                else None
            )
            terms.estimated_cash_settlement_date = _session_after(
                base, policy["tiered_cash_lag_sessions"]
            )
            terms.estimated_cash_settlement_fee = float(
                policy["additional_cash_collection_fee"]
            )
            terms.estimated_cash_settlement_withholding_rate = tax
            if terms.cash_currency != bundle.currency:
                raise ValueError(
                    "estimated tiered cash FX needs matching source quote currencies"
                )
            terms.estimated_cash_fx_rate = 1.0
            terms.estimation_assumptions.append(
                "Foreign ADS cash uses an omnibus nominee pro-rata estimate from the disclosed cash-election price/qualifying lot; this does not alter direct registered-holder tiers. HKSCC's later circular corroborates the model, not an ex-date market observation."
            )
            continue
        if terms.asset_currency != bundle.currency:
            raise ValueError(
                "cross-currency distributed-share estimates require causal FX"
            )
        terms.eligible = True
        terms.account_election = "shares"
        terms.disposal_policy = str(policy["disposal_policy"])
        delivery = _session_after(base, policy["delivery_lag_sessions"])
        payment = max(delivery, _session_after(base, policy["cash_lag_sessions"]))
        terms.estimated_delivery_date = delivery
        terms.estimated_tradable_from_date = delivery
        terms.estimated_cash_settlement_date = payment
        if terms.asset_code not in sources:
            exposure_end = (
                end
                if terms.disposal_policy == "hold"
                else min(end, max(delivery, payment))
            )
            sources[terms.asset_code] = external_loader(
                terms.asset_code, action.ex_date - timedelta(days=10), exposure_end
            )
        source = sources[terms.asset_code]
        if source.currency != terms.asset_currency:
            raise ValueError("external quote currency conflicts with declaration")
        reference_delivery = min(delivery, end)
        reference_cash = min(payment, end)
        close = _quote_at(source, reference_delivery, "raw_close")
        low = _quote_at(source, reference_delivery, "raw_low")
        terms.estimated_fee_per_asset_share = (
            low * commission if terms.disposal_policy == "sell_on_delivery" else 0.0
        )
        terms.estimated_withholding_per_asset_share = close * tax
        gross = _quote_at(source, reference_cash, "raw_close") * (1 - expense)
        terms.estimated_cash_in_lieu_gross_per_asset_share = gross
        terms.estimated_cash_in_lieu_per_asset_share = gross * (1 - tax)
        from .in_kind_market_inputs import external_asset_frame

        dates = bundle.prices.loc[
            (pd.to_datetime(bundle.prices["date"]).dt.date >= action.ex_date)
            & (
                pd.to_datetime(bundle.prices["date"]).dt.date
                <= (
                    end
                    if terms.disposal_policy == "hold"
                    else min(end, max(delivery, payment))
                )
            ),
            "date",
        ]
        external[terms.asset_code] = external_asset_frame(source, bundle, dates, config)
    return replace(bundle, actions=actions, external_prices=external)
