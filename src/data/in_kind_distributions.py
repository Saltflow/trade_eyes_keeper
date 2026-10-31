"""Disclosed in-specie terms and separately evidenced account settlement.

Declaration dates, estimated dispatch dates and account delivery dates have
different meanings. This module never promotes the first two into the last.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import numpy as np
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .market_history import CorporateAction

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
_DATE = rf"\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}"
CONFIRMED_RECONCILIATION_MARKER = "in_kind_confirmed_reconciliation_from_estimate"


class InKindDistributionTerms(BaseModel):
    """Source facts plus explicit account assumptions needed by the ledger.

    Fee/withholding values are asset-currency amounts per external share,
    never implicit percentages. A missing value is not an assertion of zero.
    The cash-in-lieu rate is a confirmed net rate; its separate tax/fee fields
    retain attribution and must not be subtracted from that rate twice.
    """

    asset_code: str | None = None
    asset_name: str | None = None
    asset_currency: str | None = None
    shares_per_parent_share: float | None = None
    ratio_numerator: float | None = None
    ratio_denominator: float | None = None
    record_date: date | None = None
    expected_delivery_date: date | None = None
    share_certificate_dispatch_date: date | None = None
    actual_delivery_date: date | None = None
    actual_delivery_published_at: date | None = None
    delivery_source_url: str | None = None
    evidence_published_at: date | None = None
    source_url: str | None = None
    source_sha256: str | None = None
    dispatch_source_url: str | None = None
    dispatch_published_at: date | None = None
    source_adjustment_factor: float | None = None
    adjustment_published_at: date | None = None
    adjustment_source_url: str | None = None
    adjustment_source_sha256: str | None = None
    delivery_source_sha256: str | None = None
    allocation_rule: Literal["linear_floor", "tiered_ads_election"] = "linear_floor"
    minimum_parent_shares: float | None = None
    eligible: bool | None = None
    account_election: Literal["shares", "cash"] | None = None
    fractional_policy: Literal["exact", "cash_in_lieu"] | None = None
    cash_in_lieu_per_asset_share: float | None = None
    cash_in_lieu_gross_per_asset_share: float | None = None
    cash_in_lieu_payment_date: date | None = None
    cash_in_lieu_published_at: date | None = None
    cash_in_lieu_source_url: str | None = None
    cash_in_lieu_source_sha256: str | None = None
    minimum_cash_payment: float | None = None
    fee_per_asset_share: float | None = None
    withholding_per_asset_share: float | None = None
    disposal_policy: Literal["hold", "sell_on_delivery"] | None = None
    declared_cash_election_per_unit: float | None = None
    cash_parent_lot: float | None = None
    cash_per_lot: float | None = None
    minimum_cash_parent_shares: float | None = None
    cash_currency: str | None = None
    cash_fx_rate: float | None = None
    cash_fx_published_at: date | None = None
    cash_fx_source_url: str | None = None
    cash_fx_source_sha256: str | None = None
    cash_settlement_date: date | None = None
    cash_settlement_published_at: date | None = None
    cash_settlement_source_url: str | None = None
    cash_settlement_source_sha256: str | None = None
    cash_settlement_fee: float | None = None
    cash_settlement_withholding_amount: float | None = None
    minimum_sale_lock_days: int | None = None
    lock_anchor: Literal["last_distribution_issuance"] | None = None
    last_distribution_issuance_date: date | None = None
    last_distribution_issuance_published_at: date | None = None
    last_distribution_issuance_source_url: str | None = None
    last_distribution_issuance_source_sha256: str | None = None
    tradable_from_date: date | None = None
    tradable_from_published_at: date | None = None
    tradable_from_source_url: str | None = None
    tradable_from_source_sha256: str | None = None
    settlement_basis: Literal["confirmed", "stock_connect_estimate"] = "confirmed"
    estimation_policy_id: str | None = None
    estimation_assumptions: list[str] = Field(default_factory=list)
    estimation_sources: list[str] = Field(default_factory=list)
    estimation_parent_currency: str | None = None
    estimated_delivery_date: date | None = None
    estimated_cash_settlement_date: date | None = None
    estimated_cash_in_lieu_per_asset_share: float | None = None
    estimated_cash_in_lieu_gross_per_asset_share: float | None = None
    estimated_fee_per_asset_share: float | None = None
    estimated_withholding_per_asset_share: float | None = None
    estimated_cash_settlement_fee: float | None = None
    estimated_cash_settlement_withholding_rate: float | None = None
    estimated_cash_fx_rate: float | None = None
    estimated_tradable_from_date: date | None = None
    estimated_last_distribution_issuance_date: date | None = None
    estimated_nominee_cash_per_parent_share: float | None = None
    estimated_minimum_cash_payment: float | None = None
    diagnostics: list[str] = Field(default_factory=list)

    def _execution_value(self, estimated_name: str, confirmed_name: str):
        if self.settlement_basis == "stock_connect_estimate":
            return getattr(self, estimated_name)
        return getattr(self, confirmed_name)

    @property
    def execution_allocation_rule(self):
        if (
            self.settlement_basis == "stock_connect_estimate"
            and self.estimated_nominee_cash_per_parent_share is not None
        ):
            return "nominee_pro_rata_cash"
        return self.allocation_rule

    @property
    def execution_nominee_cash_per_parent_share(self):
        return self.estimated_nominee_cash_per_parent_share

    @property
    def execution_minimum_cash_payment(self):
        return self._execution_value(
            "estimated_minimum_cash_payment", "minimum_cash_payment"
        )

    @property
    def execution_delivery_date(self):
        return self._execution_value("estimated_delivery_date", "actual_delivery_date")

    @property
    def execution_delivery_published_at(self):
        return (
            self.evidence_published_at
            if self.settlement_basis == "stock_connect_estimate"
            else self.actual_delivery_published_at
        )

    @property
    def execution_delivery_source_url(self):
        return (
            "policy:" + str(self.estimation_policy_id)
            if self.settlement_basis == "stock_connect_estimate"
            else self.delivery_source_url
        )

    @property
    def execution_cash_in_lieu_per_asset_share(self):
        return self._execution_value(
            "estimated_cash_in_lieu_per_asset_share", "cash_in_lieu_per_asset_share"
        )

    @property
    def execution_cash_in_lieu_gross_per_asset_share(self):
        return self._execution_value(
            "estimated_cash_in_lieu_gross_per_asset_share",
            "cash_in_lieu_gross_per_asset_share",
        )

    @property
    def execution_cash_in_lieu_payment_date(self):
        return self._execution_value(
            "estimated_cash_settlement_date", "cash_in_lieu_payment_date"
        )

    @property
    def execution_cash_in_lieu_published_at(self):
        return (
            self.estimated_cash_settlement_date
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_in_lieu_published_at
        )

    @property
    def execution_cash_in_lieu_source_url(self):
        return (
            "policy:" + str(self.estimation_policy_id)
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_in_lieu_source_url
        )

    @property
    def execution_fee_per_asset_share(self):
        return self._execution_value(
            "estimated_fee_per_asset_share", "fee_per_asset_share"
        )

    @property
    def execution_withholding_per_asset_share(self):
        return self._execution_value(
            "estimated_withholding_per_asset_share", "withholding_per_asset_share"
        )

    @property
    def execution_cash_settlement_date(self):
        return self._execution_value(
            "estimated_cash_settlement_date", "cash_settlement_date"
        )

    @property
    def execution_cash_settlement_published_at(self):
        return (
            self.evidence_published_at
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_settlement_published_at
        )

    @property
    def execution_cash_settlement_source_url(self):
        return (
            "policy:" + str(self.estimation_policy_id)
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_settlement_source_url
        )

    @property
    def execution_cash_settlement_fee(self):
        return self._execution_value(
            "estimated_cash_settlement_fee", "cash_settlement_fee"
        )

    @property
    def execution_cash_settlement_withholding_amount(self):
        return (
            0.0
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_settlement_withholding_amount
        )

    @property
    def execution_cash_settlement_withholding_rate(self):
        return (
            self.estimated_cash_settlement_withholding_rate
            if self.settlement_basis == "stock_connect_estimate"
            else 0.0
        )

    @property
    def execution_cash_fx_rate(self):
        return self._execution_value("estimated_cash_fx_rate", "cash_fx_rate")

    @property
    def execution_cash_fx_published_at(self):
        return (
            self.evidence_published_at
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_fx_published_at
        )

    @property
    def execution_cash_fx_source_url(self):
        return (
            "policy:" + str(self.estimation_policy_id)
            if self.settlement_basis == "stock_connect_estimate"
            else self.cash_fx_source_url
        )

    @property
    def execution_tradable_from_date(self):
        return self._execution_value(
            "estimated_tradable_from_date", "tradable_from_date"
        )

    @property
    def execution_last_distribution_issuance_date(self):
        return self._execution_value(
            "estimated_last_distribution_issuance_date",
            "last_distribution_issuance_date",
        )


def in_kind_distribution_issues(action: CorporateAction) -> tuple[str, ...]:
    """Reject incomplete settlement rather than making a linear cash proxy."""
    label = f"{action.code} {action.ex_date}"
    terms = getattr(action, "in_kind_terms", None)
    if terms is None:
        return (f"in-kind distribution is not supported: {label}; terms missing",)
    if terms.settlement_basis == "stock_connect_estimate":
        return _estimated_distribution_issues(action, terms)
    issues = []
    prefix = f"in-kind corporate action {label}: "
    tiered = terms.allocation_rule == "tiered_ads_election"
    receives_shares = not tiered or (
        terms.eligible is True and terms.account_election == "shares"
    )
    required = [
        "asset_code",
        "asset_currency",
        "record_date",
        "evidence_published_at",
        "source_url",
        "source_sha256",
        "minimum_parent_shares",
        "eligible",
        "account_election",
        "minimum_cash_payment",
    ]
    if receives_shares:
        required.extend(
            [
                "actual_delivery_date",
                "actual_delivery_published_at",
                "delivery_source_url",
                "delivery_source_sha256",
                "fee_per_asset_share",
                "withholding_per_asset_share",
                "disposal_policy",
            ]
        )
    if not tiered:
        required.append("fractional_policy")
    missing = [name for name in required if getattr(terms, name) in (None, "")]
    if missing:
        issues.append(prefix + "missing verified terms: " + ", ".join(missing))
    if not tiered and (
        terms.eligible is not True or terms.account_election != "shares"
    ):
        issues.append(prefix + "account share eligibility/election unverified")
    if terms.asset_currency is not None and not re.fullmatch(
        r"[A-Z]{3}", terms.asset_currency
    ):
        issues.append(prefix + "invalid asset quote currency")
    published = terms.evidence_published_at
    if published is None or published > action.ex_date:
        issues.append(prefix + "declaration lacks causal publication date")
    if action.published_at is None or action.published_at != published:
        issues.append(prefix + "action and declaration publication dates conflict")
    if (
        action.record_date is not None
        and terms.record_date is not None
        and action.record_date != terms.record_date
    ):
        issues.append(prefix + "action and declaration record dates conflict")
    if terms.record_date is not None and terms.record_date < action.ex_date:
        issues.append(prefix + "record date precedes ex-date")
    delivery = terms.actual_delivery_date
    if delivery is not None and delivery < (terms.record_date or action.ex_date):
        issues.append(prefix + "actual delivery precedes entitlement record")
    if delivery is not None and (
        terms.actual_delivery_published_at is None
        or terms.actual_delivery_published_at > delivery
    ):
        issues.append(prefix + "actual delivery lacks contemporaneous evidence")
    numeric_fields = [
        "shares_per_parent_share",
        "minimum_parent_shares",
        "minimum_cash_payment",
    ]
    if receives_shares:
        numeric_fields.extend(["fee_per_asset_share", "withholding_per_asset_share"])
    for name in numeric_fields:
        value = getattr(terms, name)
        if value is None or not np.isfinite(value) or value < 0:
            issues.append(prefix + f"invalid or unknown {name}")
    if terms.shares_per_parent_share == 0:
        issues.append(prefix + "share ratio must be positive")
    numerator, denominator = terms.ratio_numerator, terms.ratio_denominator
    if (numerator is not None or denominator is not None) and (
        numerator is None
        or denominator is None
        or not np.isfinite(numerator)
        or not np.isfinite(denominator)
        or numerator <= 0
        or denominator <= 0
        or terms.shares_per_parent_share is None
        or not np.isclose(
            numerator / denominator,
            terms.shares_per_parent_share,
            rtol=1e-12,
            atol=0,
        )
    ):
        issues.append(
            prefix + "declared numerator/denominator conflict with share ratio"
        )
    for name in (
        "source_sha256",
        "delivery_source_sha256",
        "cash_in_lieu_source_sha256",
        "adjustment_source_sha256",
        "cash_fx_source_sha256",
        "cash_settlement_source_sha256",
        "last_distribution_issuance_source_sha256",
        "tradable_from_source_sha256",
    ):
        value = getattr(terms, name)
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            issues.append(prefix + f"invalid {name}")
    factor = terms.source_adjustment_factor
    if factor is not None:
        if not np.isfinite(factor) or factor <= 0:
            issues.append(
                prefix + "source adjustment factor must be finite and positive"
            )
        if (
            terms.adjustment_published_at is None
            or terms.adjustment_published_at > action.ex_date
            or not terms.adjustment_source_url
            or not terms.adjustment_source_sha256
        ):
            issues.append(prefix + "adjustment factor lacks causal source evidence")
    if tiered:
        issues.extend(_tiered_distribution_issues(action, terms, receives_shares))
    elif terms.fractional_policy == "cash_in_lieu":
        for name in (
            "cash_in_lieu_per_asset_share",
            "cash_in_lieu_payment_date",
            "cash_in_lieu_published_at",
            "cash_in_lieu_source_url",
            "cash_in_lieu_source_sha256",
        ):
            if getattr(terms, name) in (None, ""):
                issues.append(prefix + f"missing verified {name}")
        amount = terms.cash_in_lieu_per_asset_share
        if amount is not None and (not np.isfinite(amount) or amount < 0):
            issues.append(prefix + "invalid cash-in-lieu rate")
        paid = terms.cash_in_lieu_payment_date
        disclosed = terms.cash_in_lieu_published_at
        if paid is not None and paid < (delivery or action.ex_date):
            issues.append(prefix + "cash payment precedes actual delivery")
        if paid is not None and disclosed is not None and disclosed > paid:
            issues.append(prefix + "cash settlement lacks contemporaneous evidence")
    return tuple(dict.fromkeys(issues))


def _estimated_distribution_issues(
    action: CorporateAction, terms: InKindDistributionTerms
) -> tuple[str, ...]:
    """Validate declared facts and explicitly labelled execution assumptions."""
    prefix = f"in-kind corporate action {action.code} {action.ex_date}: "
    issues = []
    required = [
        "asset_code",
        "asset_currency",
        "record_date",
        "evidence_published_at",
        "source_url",
        "source_sha256",
        "shares_per_parent_share",
        "minimum_parent_shares",
        "minimum_cash_payment",
        "eligible",
        "account_election",
        "estimation_policy_id",
        "estimation_parent_currency",
    ]
    tiered = terms.allocation_rule == "tiered_ads_election"
    shares = not tiered or (
        terms.eligible is True and terms.account_election == "shares"
    )
    if shares:
        required.extend(
            [
                "estimated_delivery_date",
                "estimated_tradable_from_date",
                "estimated_fee_per_asset_share",
                "estimated_withholding_per_asset_share",
                "disposal_policy",
            ]
        )
    if tiered:
        required.extend(
            [
                "declared_cash_election_per_unit",
                "cash_parent_lot",
                "cash_per_lot",
                "minimum_cash_parent_shares",
                "cash_currency",
                "estimated_cash_fx_rate",
                "estimated_cash_settlement_date",
                "estimated_cash_settlement_fee",
                "estimated_cash_settlement_withholding_rate",
            ]
        )
    elif terms.fractional_policy == "cash_in_lieu":
        required.extend(
            [
                "estimated_cash_settlement_date",
                "estimated_cash_in_lieu_per_asset_share",
                "estimated_cash_in_lieu_gross_per_asset_share",
            ]
        )
    missing = [name for name in required if getattr(terms, name) in (None, "")]
    if missing:
        issues.append(
            prefix + "missing explicit estimate/declaration: " + ", ".join(missing)
        )
    if not terms.estimation_assumptions or not terms.estimation_sources:
        issues.append(prefix + "estimated policy assumptions and sources are required")
    if not re.fullmatch(
        r"stock_connect_estimate_v1:[0-9a-f]{64}", terms.estimation_policy_id or ""
    ):
        issues.append(prefix + "invalid estimation policy identity")
    if not re.fullmatch(r"[0-9a-f]{64}", terms.source_sha256 or ""):
        issues.append(prefix + "invalid declaration source hash")
    for currency in (terms.asset_currency, terms.estimation_parent_currency):
        if not re.fullmatch(r"[A-Z]{3}", currency or ""):
            issues.append(prefix + "invalid source quote currency")
    published = terms.evidence_published_at
    if (
        published is None
        or published > action.ex_date
        or action.published_at != published
    ):
        issues.append(prefix + "estimated execution lacks causal declared terms")
    if terms.record_date is None or terms.record_date < action.ex_date:
        issues.append(prefix + "invalid declared record date")
    elif action.record_date is not None and action.record_date != terms.record_date:
        issues.append(prefix + "action and declaration record dates conflict")
    if shares:
        if terms.eligible is not True or terms.account_election != "shares":
            issues.append(
                prefix + "estimated share eligibility/election is not explicit"
            )
        delivery, unlocked = (
            terms.estimated_delivery_date,
            terms.estimated_tradable_from_date,
        )
        if delivery is not None and delivery < (terms.record_date or action.ex_date):
            issues.append(prefix + "estimated delivery precedes record date")
        if delivery is not None and unlocked is not None and unlocked < delivery:
            issues.append(prefix + "estimated tradability precedes delivery")
        if terms.minimum_sale_lock_days:
            anchor = terms.estimated_last_distribution_issuance_date
            if (
                anchor is None
                or unlocked is None
                or unlocked < anchor + timedelta(days=terms.minimum_sale_lock_days)
            ):
                issues.append(
                    prefix + "estimated tradability violates declared sale lock"
                )
    payment = terms.estimated_cash_settlement_date
    if payment is not None and payment < (terms.record_date or action.ex_date):
        issues.append(prefix + "estimated cash payment precedes record date")
    if (
        not tiered
        and payment is not None
        and terms.estimated_delivery_date is not None
        and payment < terms.estimated_delivery_date
    ):
        issues.append(prefix + "estimated fractional payment precedes delivery")
    if tiered and terms.cash_currency != terms.estimation_parent_currency:
        issues.append(prefix + "estimated cash FX currency evidence is incomplete")
    for name in required:
        value = getattr(terms, name)
        if (
            isinstance(value, (float, int))
            and not isinstance(value, bool)
            and (not np.isfinite(value) or value < 0)
        ):
            issues.append(prefix + "invalid estimated numeric value: " + name)
    if terms.shares_per_parent_share is not None and terms.shares_per_parent_share <= 0:
        issues.append(prefix + "share ratio must be positive")
    if (terms.ratio_numerator is not None or terms.ratio_denominator is not None) and (
        terms.ratio_numerator is None
        or terms.ratio_denominator is None
        or terms.ratio_denominator <= 0
        or terms.shares_per_parent_share is None
        or not np.isclose(
            terms.ratio_numerator / terms.ratio_denominator,
            terms.shares_per_parent_share,
            rtol=1e-12,
            atol=0,
        )
    ):
        issues.append(
            prefix + "declared numerator/denominator conflict with share ratio"
        )
    rate = terms.estimated_cash_settlement_withholding_rate
    if rate is not None and not 0 <= rate < 1:
        issues.append(prefix + "invalid estimated withholding rate")
    gross, net = (
        terms.estimated_cash_in_lieu_gross_per_asset_share,
        terms.estimated_cash_in_lieu_per_asset_share,
    )
    if gross is not None and net is not None and net > gross:
        issues.append(prefix + "estimated fractional net exceeds gross")
    return tuple(dict.fromkeys(issues))


def _tiered_distribution_issues(
    action: CorporateAction, terms: InKindDistributionTerms, receives_shares: bool
) -> list[str]:
    prefix = f"in-kind corporate action {action.code} {action.ex_date}: "
    issues = []
    required = [
        "declared_cash_election_per_unit",
        "cash_parent_lot",
        "cash_per_lot",
        "minimum_cash_parent_shares",
        "cash_currency",
        "cash_fx_rate",
        "cash_fx_published_at",
        "cash_fx_source_url",
        "cash_fx_source_sha256",
        "cash_settlement_date",
        "cash_settlement_published_at",
        "cash_settlement_source_url",
        "cash_settlement_source_sha256",
        "cash_settlement_fee",
        "cash_settlement_withholding_amount",
    ]
    if receives_shares:
        required.extend(
            [
                "minimum_sale_lock_days",
                "lock_anchor",
                "last_distribution_issuance_date",
                "last_distribution_issuance_published_at",
                "last_distribution_issuance_source_url",
                "last_distribution_issuance_source_sha256",
                "tradable_from_date",
                "tradable_from_published_at",
                "tradable_from_source_url",
                "tradable_from_source_sha256",
            ]
        )
    missing = [name for name in required if getattr(terms, name) in (None, "")]
    if missing:
        issues.append(prefix + "missing verified tiered terms: " + ", ".join(missing))
    for name in (
        "declared_cash_election_per_unit",
        "cash_parent_lot",
        "cash_per_lot",
        "minimum_cash_parent_shares",
        "cash_fx_rate",
    ):
        value = getattr(terms, name)
        if value is not None and (not np.isfinite(value) or value <= 0):
            issues.append(prefix + "invalid tiered " + name)
    for name in ("cash_settlement_fee", "cash_settlement_withholding_amount"):
        value = getattr(terms, name)
        if value is not None and (not np.isfinite(value) or value < 0):
            issues.append(prefix + "invalid tiered " + name)
    if terms.cash_currency is not None and not re.fullmatch(
        r"[A-Z]{3}", terms.cash_currency
    ):
        issues.append(prefix + "invalid tiered cash currency")
    if (
        terms.cash_fx_published_at is not None
        and terms.cash_fx_published_at > action.ex_date
    ):
        issues.append(prefix + "tiered cash FX lacks causal contract evidence")
    payment, published = terms.cash_settlement_date, terms.cash_settlement_published_at
    if payment is not None and payment < (terms.record_date or action.ex_date):
        issues.append(prefix + "tiered cash payment precedes entitlement record")
    if payment is not None and published is not None and published > payment:
        issues.append(prefix + "tiered cash settlement lacks contemporaneous evidence")
    if receives_shares:
        anchor, unlocked = (
            terms.last_distribution_issuance_date,
            terms.tradable_from_date,
        )
        days = terms.minimum_sale_lock_days
        if days is not None and days < 0:
            issues.append(prefix + "invalid tiered sale lock duration")
        if (
            anchor is not None
            and terms.last_distribution_issuance_published_at is not None
            and terms.last_distribution_issuance_published_at > anchor
        ):
            issues.append(prefix + "last issuance lacks contemporaneous evidence")
        if (
            anchor is not None
            and unlocked is not None
            and days is not None
            and unlocked < anchor + timedelta(days=max(0, days))
        ):
            issues.append(prefix + "tradable date precedes declared sale lock expiry")
        if unlocked is not None:
            if (
                terms.actual_delivery_date is not None
                and unlocked < terms.actual_delivery_date
            ):
                issues.append(prefix + "tradable date precedes actual custody delivery")
            if (
                terms.tradable_from_published_at is not None
                and terms.tradable_from_published_at > unlocked
            ):
                issues.append(prefix + "tradable date lacks contemporaneous evidence")
    return issues


def confirmed_reconciliation_pending(action: CorporateAction) -> bool:
    """Whether explicit actual reconciliation still needs confirmed facts."""
    terms = getattr(action, "in_kind_terms", None)
    return bool(
        terms is not None
        and terms.settlement_basis == "confirmed"
        and CONFIRMED_RECONCILIATION_MARKER in action.diagnostics
        and in_kind_distribution_issues(action)
    )


def verify_settlement_evidence(
    content: bytes,
    expected_sha256: str,
    action: CorporateAction,
    *,
    reconcile_estimated: bool = False,
) -> CorporateAction:
    """Import an explicitly supplied structured receipt, never a guessed path.

    The caller owns authentication of the supplied broker receipt and expected
    hash. This verifies byte integrity, instrument/event identity and typed
    facts. It neither reads files nor downloads URLs. An incomplete receipt
    can add proven facts but leaves the usual readiness gate closed.
    Estimated events require explicit confirmed reconciliation; their policy
    assumptions cannot validate actual facts or remain an execution fallback.
    """
    if not isinstance(content, bytes) or len(content) > 64 * 1024:
        raise ValueError("in-kind settlement receipt must be at most 64 KiB")
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256)):
        raise ValueError("invalid expected settlement receipt SHA256")
    actual_sha = hashlib.sha256(content).hexdigest()
    if actual_sha != expected_sha256:
        raise ValueError("in-kind settlement receipt integrity mismatch")
    try:
        receipt = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("in-kind settlement receipt is not UTF-8 JSON") from exc
    terms = getattr(action, "in_kind_terms", None)
    if not isinstance(receipt, dict) or terms is None:
        raise ValueError("in-kind settlement requires a verified declaration")
    from_estimate = terms.settlement_basis == "stock_connect_estimate"
    if from_estimate and reconcile_estimated is not True:
        raise ValueError(
            "estimated event requires explicit confirmed reconciliation; "
            "use --reconcile-estimated and provide actual account facts"
        )
    if (
        receipt.get("schema") != "in-kind-settlement-v1"
        or str(receipt.get("code", "")) != str(action.code)
        or receipt.get("ex_date") != action.ex_date.isoformat()
        or receipt.get("asset_code") != terms.asset_code
    ):
        raise ValueError("in-kind settlement receipt instrument/event mismatch")
    updates = receipt.get("terms")
    source_url = receipt.get("source_url")
    if (
        not isinstance(updates, dict)
        or not isinstance(source_url, str)
        or not source_url
    ):
        raise ValueError("in-kind settlement receipt needs explicit terms and source")
    allowed = {
        "eligible",
        "account_election",
        "actual_delivery_date",
        "actual_delivery_published_at",
        "cash_in_lieu_per_asset_share",
        "cash_in_lieu_gross_per_asset_share",
        "cash_in_lieu_payment_date",
        "cash_in_lieu_published_at",
        "fee_per_asset_share",
        "withholding_per_asset_share",
        "disposal_policy",
        "cash_fx_rate",
        "cash_fx_published_at",
        "cash_settlement_date",
        "cash_settlement_published_at",
        "cash_settlement_fee",
        "cash_settlement_withholding_amount",
        "last_distribution_issuance_date",
        "last_distribution_issuance_published_at",
        "tradable_from_date",
        "tradable_from_published_at",
    }
    if not updates or set(updates) - allowed:
        raise ValueError("settlement receipt cannot change declaration terms")
    values = terms.dict()
    if from_estimate:
        # These values were supplied by the model, not by a broker receipt.
        # Switching basis must remove them before actual evidence is checked.
        for name, value in list(values.items()):
            if name.startswith(("estimated_", "estimation_")):
                values[name] = [] if isinstance(value, list) else None
        # An estimated object may contain facts from an older partial import.
        # None of its execution facts have passed the confirmed gate as a
        # complete contract. Reconcile them afresh rather than inheriting a
        # formerly unchecked value or treating an estimate as broker evidence.
        for name in allowed:
            values[name] = None
        for prefix in (
            "delivery",
            "cash_in_lieu",
            "cash_fx",
            "cash_settlement",
            "last_distribution_issuance",
            "tradable_from",
        ):
            for suffix in ("_source_url", "_source_sha256"):
                values[prefix + suffix] = None
        values["settlement_basis"] = "confirmed"
    values.update(updates)
    if "actual_delivery_date" in updates:
        values.update(delivery_source_url=source_url, delivery_source_sha256=actual_sha)
    if "cash_in_lieu_per_asset_share" in updates:
        values.update(
            cash_in_lieu_source_url=source_url, cash_in_lieu_source_sha256=actual_sha
        )
    for field, source_prefix in (
        ("cash_fx_rate", "cash_fx"),
        ("cash_settlement_date", "cash_settlement"),
        ("last_distribution_issuance_date", "last_distribution_issuance"),
        ("tradable_from_date", "tradable_from"),
    ):
        if field in updates:
            values[source_prefix + "_source_url"] = source_url
            values[source_prefix + "_source_sha256"] = actual_sha
    candidate = InKindDistributionTerms(**values)
    for name in (
        "cash_in_lieu_per_asset_share",
        "cash_in_lieu_gross_per_asset_share",
        "fee_per_asset_share",
        "withholding_per_asset_share",
        "cash_fx_rate",
        "cash_settlement_fee",
        "cash_settlement_withholding_amount",
    ):
        value = getattr(candidate, name)
        if (
            name in updates
            and value is not None
            and (not np.isfinite(value) or value < 0)
        ):
            raise ValueError("invalid settlement receipt amount: " + name)
    if (
        "cash_fx_rate" in updates
        and candidate.cash_fx_rate is not None
        and candidate.cash_fx_rate <= 0
    ):
        raise ValueError("invalid settlement receipt amount: cash_fx_rate")
    if (
        candidate.cash_in_lieu_gross_per_asset_share is not None
        and candidate.cash_in_lieu_per_asset_share is not None
        and candidate.cash_in_lieu_per_asset_share
        > candidate.cash_in_lieu_gross_per_asset_share
    ):
        raise ValueError("settlement receipt net cash exceeds gross cash")
    result = deepcopy(action)
    result.in_kind_terms = candidate
    issues = in_kind_distribution_issues(result)
    if any(
        marker in issue
        for issue in issues
        for marker in ("conflict", "precedes", "invalid source", "contemporaneous")
    ):
        raise ValueError(
            "in-kind settlement receipt contains inconsistent facts: "
            + "; ".join(issues)
        )
    diagnostic = "in_kind_settlement_receipt_sha256=" + actual_sha
    if diagnostic not in result.diagnostics:
        result.diagnostics.append(diagnostic)
    if from_estimate:
        result.diagnostics.append(CONFIRMED_RECONCILIATION_MARKER)
    return result


def _official_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {
            "www.hkexnews.hk",
            "www1.hkexnews.hk",
        }
        or not parsed.path.lower().endswith(".pdf")
    ):
        raise ValueError("in-kind evidence must be an official HTTPS HKEX PDF")


def _day(value: str) -> date:
    return datetime.strptime(value, "%d %B %Y").date()  # noqa: DTZ007


def match_hkex_in_kind_notice(
    text: str,
    action: CorporateAction,
    *,
    published_at: date,
    source_url: str,
    listed_code: str | None = None,
) -> CorporateAction | None:
    """Resolve source terms without assigning account eligibility or delivery.

    Identity is tied to an authenticated issuer search row or a filing stock
    code. A matching dated record/entitlement is required as well as a ratio.
    This narrow parser accepts generic English HKEX declaration structures;
    unfamiliar layouts remain unresolved, never guessed through a model.
    """
    if action.action_type != "in_kind_distribution":
        return None
    _official_url(source_url)
    compact = re.sub(r"\s+", " ", text).strip()
    if listed_code is None or str(listed_code).zfill(5) != str(action.code).zfill(5):
        return None
    if published_at > action.ex_date:
        return None
    # A generic document merely mentioning an investee is not a declaration.
    if not re.search(
        r"(?:declare|declaration of).{0,200}?distribution", compact, re.IGNORECASE
    ):
        return None
    record_values = {
        _day(value)
        for value in re.findall(
            rf"(?:Record Date.{{0,90}}?(?:is|means)|[“\"]Record Date[”\"])\s+"
            rf"({_DATE})",
            compact,
            re.IGNORECASE,
        )
    }
    if len(record_values) != 1:
        return None
    record = next(iter(record_values))
    if record < action.ex_date or (record - action.ex_date).days > 14:
        return None
    explicit_ex_dates = set()
    for segment in re.findall(
        r"(?:Commencement of dealing|First day of dealing).{0,300}?"
        r"(?=Latest time|Closure of register|Record Date)",
        compact,
        re.IGNORECASE,
    ):
        if "ex-entitlement" in segment.lower():
            explicit_ex_dates.update(
                _day(value) for value in re.findall(_DATE, segment)
            )
    if explicit_ex_dates and explicit_ex_dates != {action.ex_date}:
        return None
    ratio_matches = re.findall(
        r"(?:basis of\s+)?(\d+(?:\.\d+)?)\s+(?:Class\s+[AB]\s+)?"
        r"(?:ordinary\s+)?shares?\s+of\s+(.{1,80}?)\s+for every\s+"
        r"([\d,]+)\s+Shares",
        compact,
        re.IGNORECASE,
    )
    if ratio_matches:
        ratios = {
            (float(a), float(c.replace(",", "")), b.strip())
            for a, b, c in ratio_matches
        }
        if len(ratios) != 1:
            raise ValueError("conflicting in-kind declaration ratios")
        numerator, denominator, asset_name = next(iter(ratios))
        if not all(
            np.isfinite(value) and value > 0 for value in (numerator, denominator)
        ):
            raise ValueError("invalid in-kind declaration ratio")
        # Resolve the HK ordinary-share code from its own definition, not an
        # unrelated US depositary ticker or the parent's stock code.
        identity = re.search(
            r"[“\"]" + re.escape(asset_name) + r"[”\"]\s+(.{1,700}?)"
            r"stock code\s*:\s*(\d{1,5})\s*\)",
            compact,
            re.IGNORECASE,
        )
        if identity is None:
            return None
        terms = InKindDistributionTerms(
            asset_code=identity.group(2).zfill(5),
            asset_name=asset_name,
            asset_currency=(
                "HKD"
                if re.search(
                    r"closing price of HK\$\s*[\d,.]+ per "
                    r"(?:Class [AB] ordinary share|share) of " + re.escape(asset_name),
                    compact,
                    re.IGNORECASE,
                )
                else None
            ),
            shares_per_parent_share=numerator / denominator,
            ratio_numerator=numerator,
            ratio_denominator=denominator,
            minimum_parent_shares=0,
            record_date=record,
            fractional_policy="cash_in_lieu",
        )
        if not re.search(
            r"rounded down to the nearest whole number", compact, re.IGNORECASE
        ):
            return None
    else:
        ads = re.search(
            r"one ADS for every whole multiple of\s+([\d,]+)\s+Shares",
            compact,
            re.IGNORECASE,
        )
        if ads is None:
            return None
        denominator = float(ads.group(1).replace(",", ""))
        if not np.isfinite(denominator) or denominator <= 0:
            raise ValueError("invalid ADS declaration ratio")
        terms = InKindDistributionTerms(
            asset_name="distribution ADS",
            ratio_numerator=1,
            ratio_denominator=denominator,
            shares_per_parent_share=1 / denominator,
            record_date=record,
            allocation_rule="tiered_ads_election",
            minimum_parent_shares=denominator,
            diagnostics=[
                "account election and custody receipt require separate evidence"
            ],
        )
        ticker = re.search(
            r"ticker symbol of .{1,120}? on the NYSE is [“\"]([A-Z][A-Z.]{0,9})[”\"]",
            compact,
        )
        if ticker:
            terms.asset_code = ticker.group(1)
        if ticker and re.search(r"US\$\s*[\d.]+.*?per ADS", compact):
            terms.asset_currency = "USD"
        if re.search(r"cash distribution of HK\$", compact, re.IGNORECASE):
            terms.cash_currency = "HKD"
        lock = re.search(
            r"period of (\d+) days commencing on the date of the last issuance",
            compact,
            re.IGNORECASE,
        )
        if lock:
            terms.minimum_sale_lock_days = int(lock.group(1))
            terms.lock_anchor = "last_distribution_issuance"
        lot = re.search(
            r"cash distribution of HK\$\s*(\d+(?:\.\d+)?) for every whole multiple "
            r"of\s*([\d,]+)\s*Shares",
            compact,
            re.IGNORECASE,
        )
        minimum = re.search(
            r"holds less than\s*([\d,]+)\s*Shares on the Record Date will not "
            r"receive any cash",
            compact,
            re.IGNORECASE,
        )
        if lot:
            terms.cash_per_lot = float(lot.group(1))
            terms.cash_parent_lot = float(lot.group(2).replace(",", ""))
        if minimum:
            terms.minimum_cash_parent_shares = float(minimum.group(1).replace(",", ""))
        offer = re.search(
            r"equivalent to approximately HK\$\s*(\d+(?:\.\d+)?)\) per ADS",
            compact,
            re.IGNORECASE,
        )
        if offer and re.search(
            r"rounded down to the nearest Hong Kong dollar", compact, re.IGNORECASE
        ):
            terms.declared_cash_election_per_unit = float(
                math.floor(float(offer.group(1)))
            )
        name = re.search(
            r"SEPARATE LISTING OF ([A-Z][A-Z ]+?)(?: The Spin-off| The Board)", compact
        )
        if name:
            terms.asset_name = name.group(1).strip()
    thresholds = {
        float(value.replace(",", ""))
        for value in re.findall(
            r"(?:net proceeds|entitlement to sums) of less than HK\$\s*([\d,]+(?:\.\d+)?)",
            compact,
            re.IGNORECASE,
        )
    }
    if len(thresholds) > 1:
        raise ValueError("conflicting in-kind minimum cash payments")
    if thresholds:
        terms.minimum_cash_payment = next(iter(thresholds))
    # These dates are explicitly estimates in declarations. Keep them separate.
    expected = {
        _day(value)
        for value in re.findall(
            rf"expected to be (?:on or about|despatched.{{0,160}}?on\s+(?:\w+,\s*)?)"
            rf"\s*({_DATE})",
            compact,
            re.IGNORECASE,
        )
    }
    if len(expected) == 1:
        terms.expected_delivery_date = next(iter(expected))
    terms.evidence_published_at = published_at
    terms.source_url = source_url
    terms.source_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    terms.diagnostics.append("account settlement is not proved by declaration")
    result = deepcopy(action)
    result.published_at = published_at
    result.record_date = record
    result.in_kind_terms = terms
    result.source = "hkex_in_kind_declaration"
    result.source_url = source_url
    result.diagnostics.append("hkex_in_kind_declaration_matched")
    return result


def match_hkex_in_kind_completion_notice(
    text: str,
    action: CorporateAction,
    *,
    published_at: date,
    source_url: str,
    listed_code: str | None = None,
) -> CorporateAction | None:
    """Retain confirmed issuer dispatch without inventing custody delivery.

    The JD completion filing explicitly warns that receipt may be delayed;
    its disposal date also does not establish the per-recipient net amount.
    """
    terms = getattr(action, "in_kind_terms", None)
    if action.action_type != "in_kind_distribution" or terms is None:
        return None
    _official_url(source_url)
    if listed_code is None or str(listed_code).zfill(5) != str(action.code).zfill(5):
        return None
    if published_at < (terms.evidence_published_at or action.ex_date):
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    if not terms.asset_name or terms.asset_name.lower() not in compact.lower():
        return None
    dispatch = {
        _day(value)
        for value in re.findall(
            rf"were dispatched.{{0,420}}?on\s+(?:\w+,\s*)?({_DATE})",
            compact,
            re.IGNORECASE,
        )
    }
    if len(dispatch) != 1:
        return None
    day = next(iter(dispatch))
    if day > published_at or day < (terms.record_date or action.ex_date):
        raise ValueError("in-kind completion dispatch date conflicts with record")
    result = deepcopy(action)
    result.in_kind_terms.share_certificate_dispatch_date = day
    result.in_kind_terms.dispatch_source_url = source_url
    result.in_kind_terms.dispatch_published_at = published_at
    result.in_kind_terms.diagnostics.append(
        "issuer dispatch confirmed; account receipt and net cash remain unverified"
    )
    return result
