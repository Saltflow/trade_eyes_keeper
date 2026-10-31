"""Explicit, account-resolved cross-security distribution ledger.

Declaration terms alone never make an event executable.  A schedule requires
actual delivery/settlement evidence and external raw prices in the parent's
quote currency.  Entitlements enter NAV on the ex-date, but are not spendable
cash or saleable securities until the resolved delivery date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

try:
    from numba.extending import register_jitable
except ImportError:

    def register_jitable(function):
        return function


def has_share_entitlement(action_or_terms: object) -> bool:
    """Whether an account path can receive external shares at any position size."""
    terms = getattr(action_or_terms, "in_kind_terms", action_or_terms)
    if terms is None:
        return True
    if getattr(terms, "execution_allocation_rule", None) == "nominee_pro_rata_cash":
        return False
    if getattr(terms, "allocation_rule", None) != "tiered_ads_election":
        return True
    return (
        getattr(terms, "eligible", None) is True
        and getattr(terms, "account_election", None) == "shares"
    )


@register_jitable
def tiered_allocation_numeric(
    held,
    eligible,
    shares_election,
    qualifying_lot,
    cash_parent_lot,
    cash_election_per_unit,
    cash_per_lot,
    minimum_cash_parent_shares,
):
    """Disclosed whole-lot election and residual tiers, without cash/FX guesses."""
    if held <= 0:
        return 0.0, 0.0
    if eligible and held >= qualifying_lot:
        units = np.floor(held / qualifying_lot)
        residual = max(held - units * qualifying_lot, 0.0)
        cash = np.floor(residual / cash_parent_lot) * cash_per_lot
        if shares_election:
            return units, cash
        return 0.0, units * cash_election_per_unit + cash
    if held < minimum_cash_parent_shares:
        return 0.0, 0.0
    return 0.0, np.floor(held / cash_parent_lot) * cash_per_lot


def tiered_allocation(held: float, terms: object) -> tuple[float, float]:
    """Return declared external shares and gross native-cash entitlement.

    Actual account settlement and its causal evidence remain separate gates.
    The global small-holder threshold never cancels a large holder's residual.
    """
    quantity = _number(held, "parent shares")
    if getattr(terms, "eligible", None) is None:
        raise ValueError("tiered account qualification is unknown")
    election = getattr(terms, "account_election", None)
    if election not in {"shares", "cash"}:
        raise ValueError("tiered account election is unknown")
    values = [
        _number(getattr(terms, name, None), name, positive=True)
        for name in (
            "minimum_parent_shares",
            "cash_parent_lot",
            "declared_cash_election_per_unit",
            "cash_per_lot",
            "minimum_cash_parent_shares",
        )
    ]
    ratio = _number(
        getattr(terms, "shares_per_parent_share", None), "share ratio", positive=True
    )
    if not np.isclose(ratio * values[0], 1.0, rtol=1e-12, atol=0):
        raise ValueError("tiered qualifying lot must represent one external unit")
    return tiered_allocation_numeric(
        quantity, terms.eligible, election == "shares", *values
    )


def merge_in_kind_prices(
    *sources: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """Combine complementary external histories; never overwrite a conflict.

    Source date ranges may differ between distributing parents.  Normalized
    calendar dates identify observations; overlapping nonmissing values must
    agree in every supplied column, including FX and corporate actions.
    """
    merged: dict[str, pd.DataFrame] = {}
    for source in sources:
        for asset, frame in source.items():
            if "date" not in frame or frame.columns.duplicated().any():
                raise ValueError(f"invalid in-kind external history schema: {asset}")
            current = frame.copy(deep=True)
            parsed = pd.to_datetime(current["date"], errors="raise")
            if parsed.isna().any() or parsed.dt.tz is not None:
                raise ValueError(f"invalid in-kind external history dates: {asset}")
            current["date"] = parsed.dt.normalize()
            if current["date"].duplicated().any():
                raise ValueError(f"duplicate in-kind external price dates: {asset}")
            current = current.set_index("date").sort_index()
            previous = merged.get(str(asset))
            if previous is None:
                merged[str(asset)] = current
                continue
            dates = previous.index.intersection(current.index)
            columns = previous.columns.intersection(current.columns)
            left, right = previous.loc[dates, columns], current.loc[dates, columns]
            conflict = left.notna() & right.notna() & ~left.eq(right)
            if conflict.to_numpy().any():
                row, column = np.argwhere(conflict.to_numpy())[0]
                raise ValueError(
                    f"conflicting in-kind external history: {asset} "
                    f"{dates[row].date()} {columns[column]}"
                )
            merged[str(asset)] = previous.combine_first(current).sort_index()
    return {asset: frame.reset_index() for asset, frame in merged.items()}


@dataclass(frozen=True)
class InKindSchedule:
    """Numerical event inputs; state is private to each simulated portfolio."""

    events: np.ndarray
    prices: np.ndarray
    sale_prices: np.ndarray
    fx_rates: np.ndarray
    cash_dividends: np.ndarray
    share_multipliers: np.ndarray
    asset_codes: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    metadata: tuple[dict, ...] = ()
    dates: tuple[str, ...] = ()
    cash_fx_rates: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.cash_fx_rates is None:
            object.__setattr__(self, "cash_fx_rates", self.fx_rates)

    @classmethod
    def empty(cls, rows: int) -> InKindSchedule:
        values = np.zeros((rows, 0), dtype=np.float64)
        return cls(np.zeros((0, 30)), values, values, values, values, values)

    def sliced(self, start: int, end: int) -> InKindSchedule:
        events = self.events.copy()
        # Negative event rows cannot capture a new entitlement in a freshly
        # funded evaluation window.  They are not moved onto its first day.
        for column in (1, 2, 3, 10, 12, 22, 23, 26):
            positive = events[:, column] >= 0
            events[positive, column] -= start
        return InKindSchedule(
            events,
            self.prices[start:end].copy(),
            self.sale_prices[start:end].copy(),
            self.fx_rates[start:end].copy(),
            self.cash_dividends[start:end].copy(),
            self.share_multipliers[start:end].copy(),
            self.asset_codes,
            self.event_ids,
            self.metadata,
            self.dates[start:end],
            self.cash_fx_rates[start:end].copy(),
        )

    def scaled(self, factor: float) -> InKindSchedule:
        return InKindSchedule(
            self.events.copy(),
            self.prices * factor,
            self.sale_prices * factor,
            self.fx_rates * factor,
            self.cash_dividends * factor,
            self.share_multipliers.copy(),
            self.asset_codes,
            self.event_ids,
            self.metadata,
            self.dates,
            self.cash_fx_rates * factor,
        )

    def describe(self, quantities, fractions, audit) -> dict:
        """Serialize final rights and dated, signed account cash movements."""
        positions, cashflows = [], []
        for index, event in enumerate(self.events):
            if audit[index, 0] + audit[index, 1] + audit[index, 13] <= 0:
                continue
            meta = dict(self.metadata[index])
            identity, asset = self.event_ids[index], self.asset_codes[index]
            quantity, fraction = float(quantities[index]), float(fractions[index])
            last = len(self.prices) - 1
            value = quantity * self.prices[last, index]
            if fraction > 0:
                if last >= int(event[12]):
                    amount = fraction * event[6]
                    eligible_cash = fraction * event[28] >= event[9]
                    value += (amount if eligible_cash else 0.0) * self.fx_rates[
                        last, index
                    ]
                else:
                    value += fraction * self.prices[last, index]
            cash_receivable = 0.0
            if event[14] != 0.0:
                column = 8 if last >= int(event[23]) else 9
                cash_receivable = audit[index, column] * self.cash_fx_rates[last, index]
                value += cash_receivable
            positions.append(
                {
                    **meta,
                    "event_id": identity,
                    "asset_code": asset,
                    "quantity": quantity,
                    "fractional_quantity": fraction,
                    "delivered": bool(audit[index, 7]),
                    "pending_quantity": 0.0 if audit[index, 7] else quantity,
                    "value": float(value),
                    "cash_receivable": float(cash_receivable),
                }
            )
            for kind, column, day_column, sign, affects_cash in (
                ("delivery_fee", 2, 2, -1, True),
                ("delivery_tax", 3, 2, -1, True),
                ("fractional_net_cash", 4, 3, 1, True),
                ("fractional_cash_forfeited", 5, 3, 1, False),
                ("disposal_proceeds", 6, 10, 1, True),
                ("tiered_cash_fee", 10, 22, -1, False),
                ("tiered_cash_tax", 11, 22, -1, False),
                ("tiered_net_cash", 12, 22, 1, True),
            ):
                amount = float(audit[index, column]) * sign
                if amount == 0:
                    continue
                day = int(event[day_column])
                cashflows.append(
                    {
                        "event_id": identity,
                        "asset_code": asset,
                        "date": self.dates[day],
                        "amount": amount,
                        "kind": kind,
                        "affects_cash": affects_cash,
                        "settlement_basis": meta.get("settlement_basis", "confirmed"),
                        "policy_hash": meta.get("policy_hash"),
                        "assumption_basis": meta.get("assumption_basis"),
                    }
                )
        return {"in_kind_positions": positions, "in_kind_cashflows": cashflows}

    def kernel_args(self) -> tuple[np.ndarray, ...]:
        return tuple(
            np.ascontiguousarray(value, dtype=np.float64)
            for value in (
                self.events,
                self.prices,
                self.sale_prices,
                self.fx_rates,
                self.cash_fx_rates,
                self.cash_dividends,
                self.share_multipliers,
            )
        )


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if value is None or isinstance(value, (bool, np.bool_)):
        raise ValueError(f"in-kind distribution lacks explicit {name}")
    result = float(value)
    if not np.isfinite(result) or result < 0 or (positive and result <= 0):
        raise ValueError(f"invalid in-kind distribution {name}")
    return result


def _day(value: object, name: str) -> pd.Timestamp:
    if value is None:
        raise ValueError(f"in-kind distribution lacks {name}")
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError(f"invalid in-kind distribution {name}")
    return result.normalize()


def execution_term(terms: object, name: str):
    """Read a source-resolved execution value without relabeling estimates actual."""
    alias = {
        "actual_delivery_date": "execution_delivery_date",
        "actual_delivery_published_at": "execution_delivery_published_at",
        "delivery_source_url": "execution_delivery_source_url",
        "last_distribution_issuance_date": "execution_last_distribution_issuance_date",
    }.get(name, "execution_" + name)
    if hasattr(terms, alias):
        return getattr(terms, alias)
    return getattr(terms, name, None)


def build_in_kind_schedule(
    actions: Iterable[object],
    dates: Iterable[object],
    symbols: Iterable[str],
    external_prices: Mapping[str, pd.DataFrame] | None,
) -> InKindSchedule:
    """Resolve causal account terms and separate security/native-cash legs."""
    from ..data.in_kind_distributions import in_kind_distribution_issues

    axis = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize()
    codes = list(symbols)
    selected = [
        a for a in actions if getattr(a, "action_type", "") == "in_kind_distribution"
    ]
    if not selected:
        return InKindSchedule.empty(len(axis))
    if axis.hasnans or not axis.is_monotonic_increasing or axis.has_duplicates:
        raise ValueError("in-kind schedule requires unique increasing dates")
    events, values, lows, fx_values, cash_fx_values, dividends, multipliers = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    assets, identities, metadata = [], [], []
    for action in selected:
        ex = _day(getattr(action, "ex_date", None), "ex_date")
        if not len(axis) or ex > axis[-1] or ex < axis[0]:
            continue
        parent = str(getattr(action, "code", ""))
        if parent not in codes:
            raise ValueError(f"in-kind parent outside universe: {parent}")
        terms = getattr(action, "in_kind_terms", None)
        if terms is None:
            raise ValueError(
                f"in-kind distribution lacks resolved terms: {parent} {ex}"
            )
        issues = in_kind_distribution_issues(action)
        if issues:
            raise ValueError("; ".join(issues))

        def get(name, terms=terms):
            return execution_term(terms, name)

        nominee = get("allocation_rule") == "nominee_pro_rata_cash"
        tiered = nominee or get("allocation_rule") == "tiered_ads_election"
        share_leg = has_share_entitlement(terms)
        if not tiered and get("allocation_rule") != "linear_floor":
            raise ValueError("unsupported in-kind allocation rule")
        if not tiered and (
            get("eligible") is not True or get("account_election") != "shares"
        ):
            raise ValueError(
                "in-kind account eligibility and shares election are required"
            )
        if tiered and not nominee:
            tiered_allocation(0.0, terms)
        nominee_rate = (
            _number(
                get("nominee_cash_per_parent_share"),
                "nominee_cash_per_parent_share",
                positive=True,
            )
            if nominee
            else 0.0
        )
        published = _day(get("evidence_published_at"), "evidence_published_at")
        if published > ex or not get("source_url"):
            raise ValueError("in-kind allocation lacks causal source evidence")
        record = _day(get("record_date"), "record_date")
        if record < ex:
            raise ValueError("in-kind record date precedes ex-date")
        ratio = _number(
            get("shares_per_parent_share"), "shares_per_parent_share", positive=True
        )
        minimum = _number(get("minimum_parent_shares"), "minimum_parent_shares")
        threshold = _number(get("minimum_cash_payment"), "minimum_cash_payment")
        cash_date, tier_known = ex, ex
        cash_fee = cash_tax = cash_tax_rate = cash_fx = 0.0
        cash_lot = cash_per_unit = cash_per_lot = cash_minimum = 0.0
        if tiered:
            cash_date = _day(get("cash_settlement_date"), "cash_settlement_date")
            tier_known = _day(
                get("cash_settlement_published_at"), "cash_settlement_published_at"
            )
            cash_fee = _number(get("cash_settlement_fee"), "cash_settlement_fee")
            cash_tax = _number(
                get("cash_settlement_withholding_amount"),
                "cash_settlement_withholding_amount",
            )
            rate = get("cash_settlement_withholding_rate")
            if rate is None and get("settlement_basis") == "confirmed":
                # A confirmed full withholding amount already accounts for
                # the charge.  Do not add a second percentage deduction.
                rate = 0.0
            cash_tax_rate = _number(
                rate,
                "cash_settlement_withholding_rate",
            )
            if cash_tax_rate >= 1:
                raise ValueError("tiered cash withholding rate must be less than one")
            cash_fx = _number(get("cash_fx_rate"), "cash_fx_rate", positive=True)
            fx_known = _day(get("cash_fx_published_at"), "cash_fx_published_at")
            if (
                not get("cash_currency")
                or fx_known > ex
                or cash_date < record
                or tier_known > cash_date
            ):
                raise ValueError(
                    "tiered cash currency, FX or payment lacks causal evidence"
                )
            cash_lot = _number(get("cash_parent_lot"), "cash_parent_lot", positive=True)
            cash_per_unit = _number(
                get("declared_cash_election_per_unit"),
                "declared_cash_election_per_unit",
                positive=True,
            )
            cash_per_lot = _number(get("cash_per_lot"), "cash_per_lot", positive=True)
            cash_minimum = _number(
                get("minimum_cash_parent_shares"),
                "minimum_cash_parent_shares",
                positive=True,
            )
        delivery = cash_date
        fee = tax = 0.0
        tradable_from = delivery
        if share_leg:
            if get("disposal_policy") not in {"hold", "sell_on_delivery"}:
                raise ValueError("in-kind account disposal policy is required")
            delivery = _day(get("actual_delivery_date"), "actual_delivery_date")
            confirmed = _day(
                get("actual_delivery_published_at"), "actual_delivery_published_at"
            )
            if (
                delivery < record
                or confirmed > delivery
                or not get("delivery_source_url")
            ):
                raise ValueError("in-kind actual delivery lacks causal confirmation")
            fee = _number(get("fee_per_asset_share"), "fee_per_asset_share")
            tax = _number(
                get("withholding_per_asset_share"), "withholding_per_asset_share"
            )
            tradable_from = delivery
            if get("tradable_from_date") is not None:
                tradable_from = max(
                    delivery, _day(get("tradable_from_date"), "tradable_from_date")
                )
            if tiered:
                anchor = _day(
                    get("last_distribution_issuance_date"),
                    "last_distribution_issuance_date",
                )
                lock_days = _number(
                    get("minimum_sale_lock_days"), "minimum_sale_lock_days"
                )
                tradable_from = _day(get("tradable_from_date"), "tradable_from_date")
                if tradable_from < anchor + pd.Timedelta(days=lock_days):
                    raise ValueError(
                        "in-kind disposal precedes the distribution sale lock"
                    )
                tradable_from = max(delivery, tradable_from)
        fractional = not tiered and get("fractional_policy") == "cash_in_lieu"
        if not tiered and get("fractional_policy") not in {"exact", "cash_in_lieu"}:
            raise ValueError("in-kind fractional entitlement policy is required")
        settlement, cash_known, cash_price = delivery, delivery, 0.0
        threshold_price = 0.0
        if fractional:
            cash_price = _number(
                get("cash_in_lieu_per_asset_share"), "cash_in_lieu_per_asset_share"
            )
            threshold_rate = get("cash_in_lieu_gross_per_asset_share")
            if threshold_rate is None:
                if threshold > 0 and tax > 0:
                    raise ValueError(
                        "in-kind cash threshold needs the issuer pre-withholding rate"
                    )
                threshold_rate = cash_price
            threshold_price = _number(
                threshold_rate, "cash_in_lieu_gross_per_asset_share"
            )
            settlement = _day(
                get("cash_in_lieu_payment_date"), "cash_in_lieu_payment_date"
            )
            cash_known = _day(
                get("cash_in_lieu_published_at"), "cash_in_lieu_published_at"
            )
            if (
                settlement < delivery
                or cash_known > settlement
                or not get("cash_in_lieu_source_url")
            ):
                raise ValueError(
                    "in-kind fractional cash lacks causal settlement evidence"
                )
        ex_row = int(axis.searchsorted(ex))
        delivery_row = int(axis.searchsorted(delivery))
        settle_row = int(axis.searchsorted(settlement))
        tradable_row = int(axis.searchsorted(tradable_from))
        asset = str(get("asset_code") or "cash:" + str(get("cash_currency")))
        dispose_row = -1
        if share_leg:
            if not get("asset_code") or not get("asset_currency"):
                raise ValueError(
                    "in-kind external security identity/currency is required"
                )
            frame = (external_prices or {}).get(asset)
            required = {
                "date",
                "raw_close",
                "raw_low",
                "fx_rate",
                "tradable",
                "external_cash_dividend",
                "external_share_multiplier",
            }
            if frame is None or not required.issubset(frame.columns):
                raise ValueError(
                    f"in-kind external raw/FX/action history is incomplete: {asset}"
                )
            raw = frame.copy()
            raw["date"] = pd.to_datetime(raw["date"], errors="raise").dt.normalize()
            if raw["date"].duplicated().any():
                raise ValueError(f"duplicate in-kind external price dates: {asset}")
            raw = raw.set_index("date").sort_index()
            aligned = raw.reindex(raw.index.union(axis))
            for column in ("external_cash_dividend", "external_share_multiplier"):
                if raw[column].isna().any():
                    raise ValueError(
                        f"in-kind external action history has gaps: {asset}"
                    )
            close = (
                pd.to_numeric(aligned["raw_close"], errors="coerce")
                .ffill()
                .reindex(axis)
            )
            fx = (
                pd.to_numeric(aligned["fx_rate"], errors="coerce").ffill().reindex(axis)
            )
            low = pd.to_numeric(aligned["raw_low"], errors="coerce").reindex(axis)
            available = aligned["tradable"].reindex(axis).eq(True)
            active_close, active_fx = close.iloc[ex_row:], fx.iloc[ex_row:]
            if (
                not np.isfinite(active_close).all()
                or (active_close <= 0).any()
                or not np.isfinite(active_fx).all()
                or (active_fx <= 0).any()
            ):
                raise ValueError(f"in-kind external valuation/FX coverage gap: {asset}")
            sale = np.where(available & low.gt(0), low * fx, np.nan)
            if get("disposal_policy") == "sell_on_delivery" and tradable_row < len(
                axis
            ):
                possible = np.flatnonzero(np.isfinite(sale[tradable_row:]))
                if not len(possible):
                    raise ValueError(
                        f"in-kind delivered security has no executable sale: {asset}"
                    )
                dispose_row = tradable_row + int(possible[0])
            external_cash = raw["external_cash_dividend"].reindex(axis, fill_value=0.0)
            external_split = raw["external_share_multiplier"].reindex(
                axis, fill_value=1.0
            )
            exposure_end = axis[-1]
            if dispose_row >= 0:
                exposure_end = axis[min(max(dispose_row, settle_row), len(axis) - 1)]
            if raw.index.max() < exposure_end:
                raise ValueError(
                    f"in-kind external history ends before exposure: {asset}"
                )
            relevant = (raw.index >= ex) & (raw.index <= exposure_end)
            if raw.loc[relevant, "external_cash_dividend"].ne(0.0).any():
                raise ValueError(
                    f"external dividend settlement is not resolved: {asset}"
                )
            if raw.loc[relevant, "external_share_multiplier"].ne(1.0).any():
                raise ValueError(
                    f"external share-action entitlement is not resolved: {asset}"
                )
        else:
            # This account cannot receive ADSs: no security quote, USD FX or
            # invented market price is required for its native-cash-only leg.
            close = fx = pd.Series(0.0, index=axis)
            sale = np.full(len(axis), np.nan)
            external_cash = pd.Series(0.0, index=axis)
            external_split = pd.Series(1.0, index=axis)
        cash_rates = (
            np.full(len(axis), cash_fx) if tiered else fx.to_numpy(dtype=np.float64)
        )
        events.append(
            [
                codes.index(parent),
                ex_row,
                delivery_row,
                settle_row,
                ratio,
                minimum,
                cash_price,
                fee,
                tax,
                threshold,
                dispose_row,
                float(fractional),
                int(axis.searchsorted(cash_known)),
                codes.index(asset)
                if asset in codes
                else len(codes)
                + (assets.index(asset) if asset in assets else len(assets)),
                2.0 if nominee else float(tiered),
                float(get("eligible") is True),
                float(get("account_election") == "shares"),
                minimum,
                cash_lot,
                cash_per_unit,
                cash_per_lot,
                cash_minimum,
                int(axis.searchsorted(cash_date)) if tiered else -1,
                int(axis.searchsorted(tier_known)) if tiered else -1,
                cash_fee,
                cash_tax,
                tradable_row,
                cash_tax_rate,
                threshold_price,
                nominee_rate,
            ]
        )
        values.append((close * fx).fillna(0.0).to_numpy(dtype=np.float64))
        lows.append(sale)
        fx_values.append(fx.fillna(0.0).to_numpy(dtype=np.float64))
        cash_fx_values.append(cash_rates)
        dividends.append((external_cash * fx).fillna(0.0).to_numpy(dtype=np.float64))
        multipliers.append(external_split.to_numpy(dtype=np.float64))
        assets.append(asset)
        identities.append(f"in_kind:{parent}:{ex.date()}:{asset}")
        metadata.append(
            {
                "parent_code": parent,
                "source_url": get("source_url"),
                "ex_date": str(ex.date()),
                "delivery_date": str(delivery.date()),
                "cash_currency": get("cash_currency")
                if tiered
                else get("asset_currency"),
                "settlement_basis": get("settlement_basis") or "confirmed",
                "assumption_basis": get("assumption_basis")
                or get("estimation_assumptions"),
                "policy_hash": get("policy_hash") or get("estimation_policy_id"),
                "estimation_sources": get("estimation_sources") or [],
                "allocation_rule": get("allocation_rule"),
                "declared_allocation_rule": getattr(terms, "allocation_rule", None),
            }
        )
    if not events:
        return InKindSchedule.empty(len(axis))
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate in-kind distributions")
    return InKindSchedule(
        np.asarray(events, dtype=np.float64),
        np.column_stack(values),
        np.column_stack(lows),
        np.column_stack(fx_values),
        np.column_stack(dividends),
        np.column_stack(multipliers),
        tuple(assets),
        tuple(identities),
        tuple(metadata),
        tuple(str(value.date()) for value in axis),
        np.column_stack(cash_fx_values),
    )


def advance_in_kind(
    row,
    parent_shares,
    events,
    prices,
    sale_prices,
    fx_rates,
    cash_fx_rates,
    cash_dividends,
    share_multipliers,
    quantities,
    fractions,
    audit=None,
):
    """Advance one day's immutable-source ledger; usable by Python and Numba."""
    cash, value = 0.0, 0.0
    if audit is None:
        audit = np.zeros((len(events), 14), dtype=np.float64)
    for event in range(len(events)):
        data = events[event]
        if row == int(data[1]):
            held = parent_shares[int(data[0])]
            if data[14] != 0.0:
                units, declared_cash = 0.0, 0.0
                if data[14] == 2.0:
                    declared_cash = held * data[29]
                else:
                    units, declared_cash = tiered_allocation_numeric(
                        held,
                        data[15] == 1.0,
                        data[16] == 1.0,
                        data[17],
                        data[18],
                        data[19],
                        data[20],
                        data[21],
                    )
                quantities[event] = units
                audit[event, 0] = units
                audit[event, 13] = declared_cash
                if declared_cash > 0.0:
                    tax = data[25] + declared_cash * data[27]
                    net = declared_cash - data[24] - tax
                    if net < 0:
                        raise ValueError(
                            "tiered cash costs exceed declared entitlement"
                        )
                    audit[event, 8] = net
                    audit[event, 9] = declared_cash
            elif held >= data[5] and held > 0.0:
                entitlement = held * data[4]
                if data[11] == 1.0:
                    quantities[event] = np.floor(entitlement + 1e-10)
                    fractions[event] = max(entitlement - quantities[event], 0.0)
                else:
                    quantities[event] = entitlement
                audit[event, 0] = quantities[event]
                audit[event, 1] = fractions[event]
        elif quantities[event] > 0.0 or fractions[event] > 0.0:
            # Explicit external cash/split events accompany the raw prices.
            cash += (quantities[event] + fractions[event]) * cash_dividends[row, event]
            quantities[event] *= share_multipliers[row, event]
            fractions[event] *= share_multipliers[row, event]
        if row == int(data[2]):
            fee = quantities[event] * data[7] * fx_rates[row, event]
            tax = quantities[event] * data[8] * fx_rates[row, event]
            cash -= fee + tax
            audit[event, 2] += fee
            audit[event, 3] += tax
            audit[event, 7] = 1.0
        if row == int(data[3]) and fractions[event] > 0.0:
            # data[6] is the source-confirmed NET cash-in-lieu rate.  Its
            # attributed fees/tax must never be deducted a second time.
            net = fractions[event] * data[6]
            if fractions[event] * data[28] >= data[9]:
                payment = net * fx_rates[row, event]
                cash += payment
                audit[event, 4] += payment
            else:
                audit[event, 5] += net * fx_rates[row, event]
            fractions[event] = 0.0
        if row == int(data[10]) and quantities[event] > 0.0:
            proceeds = quantities[event] * sale_prices[row, event]
            cash += proceeds
            audit[event, 6] += proceeds
            quantities[event] = 0.0
        if data[14] != 0.0 and row == int(data[22]) and audit[event, 9] > 0.0:
            cash += audit[event, 8] * cash_fx_rates[row, event]
            audit[event, 10] += data[24] * cash_fx_rates[row, event]
            tax = data[25] + audit[event, 9] * data[27]
            audit[event, 11] += tax * cash_fx_rates[row, event]
            audit[event, 12] += audit[event, 8] * cash_fx_rates[row, event]
            audit[event, 8] = 0.0
            audit[event, 9] = 0.0
        value += quantities[event] * prices[row, event]
        if row >= int(data[12]):
            amount = fractions[event] * data[6]
            if fractions[event] * data[28] >= data[9]:
                value += amount * fx_rates[row, event]
        else:
            value += fractions[event] * prices[row, event]
        if data[14] != 0.0:
            column = 8 if row >= int(data[23]) else 9
            value += audit[event, column] * cash_fx_rates[row, event]
    return cash, value


def in_kind_concentration(
    parent_shares,
    parent_prices,
    events,
    prices,
    fx_rates,
    quantities,
    fractions,
    row,
    position_value,
    cash_fx_rates=None,
    audit=None,
):
    """HHI groups repeated distributions and monitored external stocks once."""
    if position_value <= 0:
        return 0.0
    if len(events) == 0:
        result = 0.0
        for column in range(len(parent_shares)):
            amount = parent_shares[column] * parent_prices[column]
            if amount > 0:
                result += (amount / position_value) ** 2
        return result
    exposures = np.zeros(len(parent_shares) + len(events), dtype=np.float64)
    for column in range(len(parent_shares)):
        exposures[column] = parent_shares[column] * parent_prices[column]
    for event in range(len(events)):
        amount = quantities[event] * prices[row, event]
        if row >= int(events[event, 12]):
            net = fractions[event] * events[event, 6]
            if fractions[event] * events[event, 28] >= events[event, 9]:
                amount += net * fx_rates[row, event]
        else:
            amount += fractions[event] * prices[row, event]
        if cash_fx_rates is not None and audit is not None and events[event, 14] != 0.0:
            column = 8 if row >= int(events[event, 23]) else 9
            amount += audit[event, column] * cash_fx_rates[row, event]
        exposures[int(events[event, 13])] += amount
    result = 0.0
    for amount in exposures:
        if amount > 0:
            result += (amount / position_value) ** 2
    return result
