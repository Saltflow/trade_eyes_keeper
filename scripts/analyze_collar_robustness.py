"""Robustness and attribution diagnostics for the fixed ETF collar.

This runner uses the same real SSE contract catalog, Sina option OHLC history,
and ETF price history as ``backtest_510300_collar.py``.  It deliberately does
not optimize a final strategy: the fixed 100% Put / 105% Call collar is
evaluated, while a nearby parameter surface is reported separately.

The attribution is a lifecycle cash-flow attribution.  A new put/call pair
contributes its net premium on entry and its put realization/call loss on
expiry, roll, or terminal mark.  This makes the insurance economics explicit
and reconciles to the final portfolio NAV, while daily NAV continues to use
closing marks.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backtest_510300_collar import (
    Contract,
    Roll,
    _active_roll_index,
    _build_rolls,
    _candidate_metrics,
    _load_catalog,
    _load_daily,
    _load_underlying,
    _metrics,
    _option_execution_price,
    _option_price,
    _spot_benchmark,
    _underlying_execution_price,
    _with_entry_prices,
    _simulate,
)


LISTING_STARTS = {
    "510300": "2019-12-23",
    "510500": "2022-09-19",
}
PUT_GRID = (0.90, 0.925, 0.95, 0.975, 1.00)
CALL_GRID = (1.05, 1.075, 1.10, 1.125, 1.15)
COMPONENTS = (
    "underlying_pnl",
    "put_claim_pnl",
    "call_loss_pnl",
    "net_premium_pnl",
    "transaction_cost_pnl",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default="510300,510500")
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--test-days", type=int, default=756)
    parser.add_argument("--cost-rate", type=float, default=0.002)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--output-dir", default="cache/analysis/collar_robustness"
    )
    args = parser.parse_args()
    args.codes = [item.strip() for item in args.codes.split(",") if item.strip()]
    unknown = sorted(set(args.codes).difference(LISTING_STARTS))
    if unknown:
        raise ValueError(f"Unsupported ETF option code(s): {unknown}")
    if args.cost_rate < 0:
        raise ValueError("--cost-rate must be non-negative")
    return args


def _underlying_path(code: str) -> Path:
    if code == "510500":
        return Path("cache/analysis/option_collar_510500/underlying_yahoo_ohlc.csv")
    return Path(f"cache/data/{code}.csv")


def _cache_path(code: str) -> Path:
    return Path(f"cache/analysis/option_collar_{code}_hl")


def _event_date_after(
    dates: Sequence[pd.Timestamp], expiry: pd.Timestamp
) -> pd.Timestamp:
    after = [item for item in dates if item > expiry]
    return after[0] if after else expiry


def _option_realization(
    underlying: pd.DataFrame,
    rolls: Sequence[Roll],
    daily: Mapping[str, pd.DataFrame],
    index: int,
) -> Tuple[pd.Timestamp, float, float, str]:
    """Return event date, put proceeds, call cost, and event type."""
    roll = rolls[index]
    expiry = pd.Timestamp(roll.expiry_date)
    dates = [pd.Timestamp(item).normalize() for item in underlying["date"]]
    next_roll_date = (
        pd.Timestamp(rolls[index + 1].roll_date)
        if index + 1 < len(rolls)
        else None
    )
    if next_roll_date is not None and next_roll_date <= expiry:
        put_value = _option_execution_price(
            daily[roll.put.security_id],
            next_roll_date,
            expiry,
            roll.underlying_close,
            roll.put,
            "sell",
        )
        call_value = _option_execution_price(
            daily[roll.call.security_id],
            next_roll_date,
            expiry,
            roll.underlying_close,
            roll.call,
            "buy",
        )
        return (
            next_roll_date,
            put_value * roll.put.contract_unit,
            call_value * roll.call.contract_unit,
            "roll_exit",
        )

    last_date = dates[-1]
    if expiry <= last_date:
        expiry_close = float(
            underlying.loc[underlying["date"] == expiry, "close"].iloc[0]
        )
        put_value = max(roll.put.strike - expiry_close, 0.0)
        call_value = max(expiry_close - roll.call.strike, 0.0)
        return (
            _event_date_after(dates, expiry),
            put_value * roll.put.contract_unit,
            call_value * roll.call.contract_unit,
            "expiry_settlement",
        )

    put_value = _option_price(
        daily[roll.put.security_id],
        last_date,
        expiry,
        float(underlying["close"].iloc[-1]),
        roll.put,
    )
    call_value = _option_price(
        daily[roll.call.security_id],
        last_date,
        expiry,
        float(underlying["close"].iloc[-1]),
        roll.call,
    )
    return (
        last_date,
        put_value * roll.put.contract_unit,
        call_value * roll.call.contract_unit,
        "terminal_mark",
    )


def _empty_ledger(underlying: pd.DataFrame) -> pd.DataFrame:
    ledger = pd.DataFrame({"date": underlying["date"].copy()})
    for column in COMPONENTS:
        ledger[column] = 0.0
    return ledger


def _underlying_pnl_ledger(
    underlying: pd.DataFrame, rolls: Sequence[Roll], transaction_cost_rate: float
) -> Tuple[pd.DataFrame, float]:
    ledger = _empty_ledger(underlying)
    dates = [pd.Timestamp(item).normalize() for item in underlying["date"]]
    rows = {date: index for index, date in enumerate(dates)}
    first = underlying.iloc[0]
    initial_units = rolls[0].call.contract_unit
    entry_price = _underlying_execution_price(first, "buy")
    first_roll = rolls[0]
    initial_cost = transaction_cost_rate * (
        entry_price * initial_units
        + first_roll.put_entry * first_roll.put.contract_unit
        + first_roll.call_entry * first_roll.call.contract_unit
    )
    ledger.loc[0, "underlying_pnl"] += initial_units * (
        float(first["close"]) - entry_price
    )
    ledger.loc[0, "transaction_cost_pnl"] -= initial_cost

    previous_index = _active_roll_index(rolls, dates[0])
    previous_units = rolls[previous_index].call.contract_unit
    previous_close = float(first["close"])
    for date, (_, row) in zip(dates[1:], underlying.iloc[1:].iterrows()):
        current_index = _active_roll_index(rolls, date)
        current_units = rolls[current_index].call.contract_unit
        close = float(row["close"])
        ledger.loc[rows[date], "underlying_pnl"] += previous_units * (
            close - previous_close
        )
        if current_index != previous_index:
            unit_delta = previous_units - current_units
            if unit_delta > 0:
                trade_price = _underlying_execution_price(row, "sell")
            elif unit_delta < 0:
                trade_price = _underlying_execution_price(row, "buy")
            else:
                trade_price = 0.0
            ledger.loc[rows[date], "underlying_pnl"] += unit_delta * (
                trade_price - close
            )
        previous_index = current_index
        previous_units = current_units
        previous_close = close
    return ledger, initial_units


def _spot_pnl_ledger(
    underlying: pd.DataFrame, transaction_cost_rate: float
) -> Tuple[pd.DataFrame, float]:
    ledger = _empty_ledger(underlying)
    first = underlying.iloc[0]
    entry_price = _underlying_execution_price(first, "buy")
    units = 1.0 / (entry_price * (1.0 + transaction_cost_rate))
    ledger.loc[0, "underlying_pnl"] = units * (
        float(first["close"]) - entry_price
    )
    ledger.loc[0, "transaction_cost_pnl"] = (
        -transaction_cost_rate * entry_price * units
    )
    previous_close = float(first["close"])
    for index, (_, row) in enumerate(underlying.iloc[1:].iterrows(), start=1):
        close = float(row["close"])
        ledger.loc[index, "underlying_pnl"] = units * (close - previous_close)
        previous_close = close
    return ledger, 1.0


def _attribute_collar(
    underlying: pd.DataFrame,
    rolls: Sequence[Roll],
    daily: Mapping[str, pd.DataFrame],
    transaction_cost_rate: float,
    frame: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, float]]:
    ledger, _ = _underlying_pnl_ledger(
        underlying, rolls, transaction_cost_rate
    )
    dates = pd.to_datetime(ledger["date"]).dt.normalize()
    positions = {date: index for index, date in enumerate(dates)}
    for roll in rolls:
        roll_date = pd.Timestamp(roll.roll_date)
        put_premium = roll.put_entry * roll.put.contract_unit
        call_premium = roll.call_entry * roll.call.contract_unit
        ledger.loc[positions[roll_date], "net_premium_pnl"] += (
            call_premium - put_premium
        )
    for index, roll in enumerate(rolls):
        event_date, put_value, call_value, _ = _option_realization(
            underlying, rolls, daily, index
        )
        event_date = pd.Timestamp(event_date).normalize()
        if event_date not in positions:
            raise RuntimeError(
                f"Attribution event date is outside history: {event_date}"
            )
        ledger.loc[positions[event_date], "put_claim_pnl"] += put_value
        ledger.loc[positions[event_date], "call_loss_pnl"] -= call_value

    for index in range(1, len(rolls)):
        roll = rolls[index]
        previous = rolls[index - 1]
        date = pd.Timestamp(roll.roll_date)
        row = underlying.loc[underlying["date"] == date].iloc[0]
        old_put = _option_execution_price(
            daily[previous.put.security_id],
            date,
            pd.Timestamp(previous.expiry_date),
            float(row["close"]),
            previous.put,
            "sell",
        )
        old_call = _option_execution_price(
            daily[previous.call.security_id],
            date,
            pd.Timestamp(previous.expiry_date),
            float(row["close"]),
            previous.call,
            "buy",
        )
        old_units = previous.call.contract_unit
        new_units = roll.call.contract_unit
        unit_delta = old_units - new_units
        if unit_delta > 0:
            underlying_price = _underlying_execution_price(row, "sell")
        elif unit_delta < 0:
            underlying_price = _underlying_execution_price(row, "buy")
        else:
            underlying_price = 0.0
        turnover = (
            old_put * previous.put.contract_unit
            + old_call * previous.call.contract_unit
            + roll.put_entry * roll.put.contract_unit
            + roll.call_entry * roll.call.contract_unit
            + abs(unit_delta) * underlying_price
        )
        ledger.loc[positions[date], "transaction_cost_pnl"] -= (
            transaction_cost_rate * turnover
        )

    ledger["collar_pnl"] = ledger[list(COMPONENTS)].sum(axis=1)
    collar_initial_capital = float(frame.attrs["initial_capital"])
    expected = float(frame["nav"].iloc[-1] - collar_initial_capital)
    actual = float(ledger["collar_pnl"].sum())
    error = actual - expected
    if abs(error) > max(1e-5, abs(expected) * 1e-8):
        raise RuntimeError(
            f"Collar attribution does not reconcile: ledger={actual}, "
            f"nav_delta={expected}, error={error}"
        )
    spot_frame = _spot_benchmark(underlying, transaction_cost_rate)
    spot_ledger, spot_initial_capital = _spot_pnl_ledger(
        underlying, transaction_cost_rate
    )
    spot_expected = float(spot_frame["nav"].iloc[-1] - spot_initial_capital)
    spot_actual = float(spot_ledger["underlying_pnl"].sum()) + float(
        spot_ledger["transaction_cost_pnl"].sum()
    )
    if abs(spot_actual - spot_expected) > max(1e-8, abs(spot_expected) * 1e-8):
        raise RuntimeError("Spot attribution does not reconcile")
    return ledger, spot_ledger, {
        "collar_initial_capital": collar_initial_capital,
        "spot_initial_capital": spot_initial_capital,
        "collar_reconciliation_error": error,
        "spot_reconciliation_error": spot_actual - spot_expected,
    }


def _test_window_attribution(
    ledger: pd.DataFrame,
    spot_ledger: pd.DataFrame,
    frame: pd.DataFrame,
    spot_frame: pd.DataFrame,
    test_start: pd.Timestamp,
) -> Dict[str, object]:
    """Reconcile component P&L from the first test-window NAV onward."""
    test_start = pd.Timestamp(test_start).normalize()
    collar_start = frame.loc[frame["date"] == test_start].iloc[0]
    spot_start = spot_frame.loc[spot_frame["date"] == test_start].iloc[0]
    collar_changes = ledger[ledger["date"] > test_start]
    spot_changes = spot_ledger[spot_ledger["date"] > test_start]
    collar_components = {
        column: float(collar_changes[column].sum()) for column in COMPONENTS
    }
    opening_put_mark = float(collar_start["put_price"] * collar_start["put_unit"])
    opening_call_mark = float(collar_start["call_price"] * collar_start["call_unit"])
    # The first test NAV already contains these option marks.  Subtracting
    # the long put and adding back the short call closes the window ledger.
    opening_option_mark = -opening_put_mark + opening_call_mark
    collar_components["opening_option_mark_pnl"] = opening_option_mark
    collar_actual = sum(collar_components.values())
    collar_expected = float(frame["nav"].iloc[-1] - collar_start["nav"])
    spot_components = {
        column: float(spot_changes[column].sum()) for column in COMPONENTS
    }
    spot_actual = sum(spot_components.values())
    spot_expected = float(spot_frame["nav"].iloc[-1] - spot_start["nav"])
    collar_start_nav = float(collar_start["nav"])
    spot_start_nav = float(spot_start["nav"])
    return {
        "start": test_start.strftime("%Y-%m-%d"),
        "end": pd.Timestamp(frame["date"].iloc[-1]).strftime("%Y-%m-%d"),
        "collar_start_nav": collar_start_nav,
        "spot_start_nav": spot_start_nav,
        "collar_components_pnl": collar_components,
        "collar_components_pct_of_start_nav": {
            column: value / collar_start_nav * 100.0
            for column, value in collar_components.items()
        },
        "spot_components_pnl": spot_components,
        "spot_components_pct_of_start_nav": {
            column: value / spot_start_nav * 100.0
            for column, value in spot_components.items()
        },
        "collar_total_pnl": collar_actual,
        "spot_total_pnl": spot_actual,
        "collar_total_return_pct": collar_actual / collar_start_nav * 100.0,
        "spot_total_return_pct": spot_actual / spot_start_nav * 100.0,
        "relative_return_pct_points": (
            collar_actual / collar_start_nav * 100.0
            - spot_actual / spot_start_nav * 100.0
        ),
        "collar_reconciliation_error": collar_actual - collar_expected,
        "spot_reconciliation_error": spot_actual - spot_expected,
    }


def _test_monthly_attribution(
    ledger: pd.DataFrame,
    spot_ledger: pd.DataFrame,
    frame: pd.DataFrame,
    spot_frame: pd.DataFrame,
    test_start: pd.Timestamp,
) -> pd.DataFrame:
    """Build monthly components whose sum reconciles to the test NAV change."""
    test_start = pd.Timestamp(test_start).normalize()
    collar_start = frame.loc[frame["date"] == test_start].iloc[0]
    spot_start = spot_frame.loc[spot_frame["date"] == test_start].iloc[0]
    collar = ledger[ledger["date"] > test_start].copy()
    collar["month"] = pd.to_datetime(collar["date"]).dt.to_period("M").astype(str)
    collar = collar.groupby("month", as_index=False)[list(COMPONENTS)].sum()
    collar["opening_option_mark_pnl"] = 0.0
    opening_put = float(collar_start["put_price"] * collar_start["put_unit"])
    opening_call = float(collar_start["call_price"] * collar_start["call_unit"])
    opening_mark = -opening_put + opening_call
    first_month = test_start.to_period("M").strftime("%Y-%m")
    collar.loc[collar["month"] == first_month, "opening_option_mark_pnl"] = (
        opening_mark
    )
    collar["collar_pnl"] = collar[
        list(COMPONENTS) + ["opening_option_mark_pnl"]
    ].sum(axis=1)

    spot = spot_ledger[spot_ledger["date"] > test_start].copy()
    spot["month"] = pd.to_datetime(spot["date"]).dt.to_period("M").astype(str)
    spot = spot.groupby("month", as_index=False)[list(COMPONENTS)].sum()
    spot = spot.rename(
        columns={column: f"spot_{column}" for column in COMPONENTS}
    )
    monthly = collar.merge(spot, on="month", how="outer").fillna(0.0)
    collar_months = _monthly_returns(
        frame, float(collar_start["nav"]), test_start
    ).rename(columns={"month_return": "collar_month_return"})
    spot_months = _monthly_returns(
        spot_frame, float(spot_start["nav"]), test_start
    ).rename(columns={"month_return": "spot_month_return"})
    monthly = monthly.merge(
        collar_months[["month", "month_end", "collar_month_return"]],
        on="month",
        how="left",
    )
    monthly = monthly.merge(
        spot_months[["month", "spot_month_return"]],
        on="month",
        how="left",
    )
    monthly["collar_pnl_pct"] = monthly["collar_pnl"] / collar_start["nav"] * 100.0
    monthly["spot_pnl"] = monthly[
        [f"spot_{column}" for column in COMPONENTS]
    ].sum(axis=1)
    monthly["spot_pnl_pct"] = monthly["spot_pnl"] / spot_start["nav"] * 100.0
    monthly["relative_pnl_pct"] = (
        monthly["collar_pnl_pct"] - monthly["spot_pnl_pct"]
    )
    monthly["relative_month_return_pct"] = (
        monthly["collar_month_return"] - monthly["spot_month_return"]
    ) * 100.0
    for column in list(COMPONENTS) + ["opening_option_mark_pnl"]:
        monthly[f"{column}_pct"] = (
            monthly[column] / collar_start["nav"] * 100.0
        )
        monthly[f"cumulative_{column}_pct"] = monthly[f"{column}_pct"].cumsum()
    monthly["cumulative_collar_pnl_pct"] = monthly["collar_pnl_pct"].cumsum()
    monthly["cumulative_spot_pnl_pct"] = monthly["spot_pnl_pct"].cumsum()
    monthly["cumulative_relative_pnl_pct"] = monthly["relative_pnl_pct"].cumsum()
    monthly = monthly.sort_values("month").reset_index(drop=True)
    monthly["relative_rank"] = (
        monthly["relative_month_return_pct"]
        .rank(method="first", ascending=False)
        .astype(int)
    )
    collar_expected = float(frame["nav"].iloc[-1] - collar_start["nav"])
    spot_expected = float(spot_frame["nav"].iloc[-1] - spot_start["nav"])
    collar_actual = float(monthly["collar_pnl"].sum())
    spot_actual = float(monthly["spot_pnl"].sum())
    if abs(collar_actual - collar_expected) > 1e-6:
        raise RuntimeError("Test monthly collar attribution does not reconcile")
    if abs(spot_actual - spot_expected) > 1e-8:
        raise RuntimeError("Test monthly spot attribution does not reconcile")
    return monthly


def _monthly_returns(
    frame: pd.DataFrame,
    initial_capital: float,
    start: pd.Timestamp | None = None,
) -> pd.DataFrame:
    selected = frame.copy()
    if start is not None:
        selected = selected[selected["date"] >= start]
    selected = selected.sort_values("date").reset_index(drop=True)
    if selected.empty:
        return pd.DataFrame(
            columns=["month", "month_end", "month_return", "end_nav"]
        )
    previous_nav = float(
        initial_capital if start is None else selected["nav"].iloc[0]
    )
    rows = []
    selected["month"] = selected["date"].dt.to_period("M").astype(str)
    for month, group in selected.groupby("month", sort=True):
        end_nav = float(group["nav"].iloc[-1])
        rows.append(
            {
                "month": month,
                "month_end": group["date"].iloc[-1],
                "month_return": end_nav / previous_nav - 1.0,
                "end_nav": end_nav,
            }
        )
        previous_nav = end_nav
    return pd.DataFrame(rows)


def _monthly_attribution(
    ledger: pd.DataFrame,
    spot_ledger: pd.DataFrame,
    frame: pd.DataFrame,
    spot_frame: pd.DataFrame,
    collar_initial_capital: float,
    spot_initial_capital: float,
) -> pd.DataFrame:
    monthly = ledger.copy()
    monthly["month"] = pd.to_datetime(monthly["date"]).dt.to_period("M").astype(str)
    monthly = monthly.groupby("month", as_index=False)[
        list(COMPONENTS) + ["collar_pnl"]
    ].sum()
    spot = spot_ledger.copy()
    spot["month"] = pd.to_datetime(spot["date"]).dt.to_period("M").astype(str)
    spot = spot.groupby("month", as_index=False)[list(COMPONENTS)].sum()
    spot = spot.rename(
        columns={column: f"spot_{column}" for column in COMPONENTS}
    )
    monthly = monthly.merge(spot, on="month", how="left")
    collar_months = _monthly_returns(
        frame, collar_initial_capital
    ).rename(columns={"month_return": "collar_month_return"})
    spot_months = _monthly_returns(
        spot_frame, spot_initial_capital
    ).rename(columns={"month_return": "spot_month_return"})
    monthly = monthly.merge(
        collar_months[["month", "month_end", "collar_month_return"]],
        on="month",
        how="left",
    )
    monthly = monthly.merge(
        spot_months[["month", "spot_month_return"]], on="month", how="left"
    )
    monthly["collar_attribution_pnl_pct"] = (
        monthly["collar_pnl"] / collar_initial_capital * 100.0
    )
    monthly["spot_attribution_pnl_pct"] = (
        monthly[[f"spot_{column}" for column in COMPONENTS]].sum(axis=1)
        / spot_initial_capital
        * 100.0
    )
    monthly["relative_pnl_pct"] = (
        monthly["collar_attribution_pnl_pct"]
        - monthly["spot_attribution_pnl_pct"]
    )
    monthly["relative_month_return_pct"] = (
        monthly["collar_month_return"] - monthly["spot_month_return"]
    ) * 100.0
    for column in COMPONENTS:
        monthly[f"{column}_pct"] = (
            monthly[column] / collar_initial_capital * 100.0
        )
        monthly[f"cumulative_{column}_pct"] = monthly[f"{column}_pct"].cumsum()
    monthly["cumulative_relative_pnl_pct"] = monthly["relative_pnl_pct"].cumsum()
    monthly = monthly.sort_values("month").reset_index(drop=True)
    monthly["relative_rank"] = (
        monthly["relative_pnl_pct"]
        .rank(method="first", ascending=False)
        .astype(int)
    )
    return monthly


def _leave_best_months(
    monthly: pd.DataFrame,
    collar_frame: pd.DataFrame,
    spot_frame: pd.DataFrame,
    collar_initial_capital: float,
    spot_initial_capital: float,
    test_start: pd.Timestamp | None = None,
) -> Dict[str, object]:
    if test_start is None:
        selected_monthly = monthly.copy()
        collar_months = _monthly_returns(
            collar_frame, collar_initial_capital
        ).rename(columns={"month_return": "collar_month_return"})
        spot_months = _monthly_returns(
            spot_frame, spot_initial_capital
        ).rename(columns={"month_return": "spot_month_return"})
        sample = "full"
    else:
        selected_monthly = monthly[
            monthly["month"] >= test_start.strftime("%Y-%m")
        ].copy()
        collar_months = _monthly_returns(
            collar_frame, collar_initial_capital, test_start
        ).rename(columns={"month_return": "collar_month_return"})
        spot_months = _monthly_returns(
            spot_frame, spot_initial_capital, test_start
        ).rename(columns={"month_return": "spot_month_return"})
        sample = "test"
    selected_monthly = selected_monthly.drop(
        columns=["collar_month_return", "spot_month_return"], errors="ignore"
    )
    selected_monthly = selected_monthly.merge(
        collar_months[["month", "collar_month_return"]],
        on="month",
        how="left",
        suffixes=("", "_returns"),
    )
    selected_monthly = selected_monthly.merge(
        spot_months[["month", "spot_month_return"]],
        on="month",
        how="left",
        suffixes=("", "_spot"),
    )
    if test_start is not None:
        selected_monthly["relative_pnl_pct"] = (
            selected_monthly["collar_month_return"]
            - selected_monthly["spot_month_return"]
        ) * 100.0
    selected_monthly = selected_monthly.sort_values(
        "relative_pnl_pct", ascending=False
    ).reset_index(drop=True)
    results = []
    top_months = selected_monthly[["month", "relative_pnl_pct"]].head(3)
    for remove_count in range(4):
        removed = set(top_months.head(remove_count)["month"])
        remaining = selected_monthly[~selected_monthly["month"].isin(removed)]
        returns = remaining["collar_month_return"].dropna().to_numpy(dtype=float)
        growth = float(np.prod(1.0 + returns)) if len(returns) else float("nan")
        cagr = (
            (growth ** (12.0 / len(returns)) - 1.0) * 100.0
            if len(returns)
            else float("nan")
        )
        std = float(np.std(returns, ddof=1)) if len(returns) > 1 else float("nan")
        sharpe = (
            float(np.mean(returns) / std * np.sqrt(12.0))
            if len(returns) > 1 and std > 1e-12
            else float("nan")
        )
        results.append(
            {
                "removed_count": remove_count,
                "removed_months": sorted(removed),
                "remaining_months": int(len(returns)),
                "remaining_cagr_pct": cagr,
                "remaining_monthly_sharpe": sharpe,
                "remaining_total_compounded_return_pct": (
                    (growth - 1.0) * 100.0 if len(returns) else float("nan")
                ),
            }
        )
    return {
        "sample": sample,
        "top_relative_months": [
            {
                "month": str(row["month"]),
                "relative_pnl_pct": float(row["relative_pnl_pct"]),
            }
            for _, row in top_months.iterrows()
        ],
        "results": results,
    }


def _plateau_summary(
    rows: Sequence[Mapping[str, object]], spot_test: Mapping[str, object]
) -> Dict[str, object]:
    cells = {(0.975, 1.05), (1.0, 1.05), (0.975, 1.075), (1.0, 1.075)}
    selected = [
        item
        for item in rows
        if item.get("status") == "ok"
        and (float(item["target_put"]), float(item["target_call"])) in cells
    ]
    spot_sharpe = float(spot_test["sharpe"])
    spot_return = float(spot_test["total_return_pct"])
    cell_rows = []
    for item in sorted(
        selected, key=lambda row: (float(row["target_put"]), float(row["target_call"]))
    ):
        row = dict(item)
        row["test_sharpe_minus_spot"] = float(row["test_sharpe"]) - spot_sharpe
        row["test_return_minus_spot_pct"] = (
            float(row["test_total_return_pct"]) - spot_return
        )
        row["beats_spot_on_both"] = (
            float(row["test_sharpe"]) > spot_sharpe
            and float(row["test_total_return_pct"]) > spot_return
        )
        cell_rows.append(row)
    sharpe_values = [float(item["test_sharpe"]) for item in cell_rows]
    return {
        "criterion": (
            "all four cells must beat naked spot on both test return and "
            "test Sharpe"
        ),
        "reference_cells": cell_rows,
        "reference_cell_count": len(cell_rows),
        "reference_cells_beat_both_count": sum(
            item["beats_spot_on_both"] for item in cell_rows
        ),
        "all_reference_cells_present": len(cell_rows) == len(cells),
        "all_reference_cells_beat_spot_on_both": bool(cell_rows)
        and all(item["beats_spot_on_both"] for item in cell_rows),
        "test_sharpe_min": min(sharpe_values) if sharpe_values else None,
        "test_sharpe_max": max(sharpe_values) if sharpe_values else None,
        "test_sharpe_range": (
            max(sharpe_values) - min(sharpe_values) if sharpe_values else None
        ),
    }


def _run_code(
    code: str,
    end: str,
    test_days: int,
    transaction_cost_rate: float,
    workers: int,
    output_root: Path,
) -> Dict[str, object]:
    start = LISTING_STARTS[code]
    cache_dir = _cache_path(code)
    underlying = _load_underlying(
        _underlying_path(code), start, end, code
    )
    catalog = _load_catalog(
        underlying, cache_dir / "sse_catalog.json", code
    )
    targets = list(itertools.product(PUT_GRID, CALL_GRID))
    rolls_by_target = {
        target: _build_rolls(underlying, catalog, *target, code)
        for target in targets
    }
    security_ids = sorted(
        {
            contract.security_id
            for rolls in rolls_by_target.values()
            for roll in rolls
            for contract in (roll.put, roll.call)
        }
    )
    daily = _load_daily(cache_dir, security_ids, workers)
    result_rows: List[Dict[str, object]] = []
    evaluated: Dict[Tuple[float, float], Tuple[List[Roll], pd.DataFrame]] = {}
    for target, base_rolls in rolls_by_target.items():
        try:
            rolls = _with_entry_prices(base_rolls, daily)
            frame = _simulate(
                underlying,
                rolls,
                daily,
                transaction_cost_rate=transaction_cost_rate,
            )
            row, _, _ = _candidate_metrics(frame, *target, test_days)
            result_rows.append(row)
            evaluated[target] = (rolls, frame)
        except Exception as exc:
            result_rows.append(
                {
                    "target_put": target[0],
                    "target_call": target[1],
                    "status": "failed",
                    "error": str(exc),
                }
            )
    fixed_target = (1.0, 1.05)
    if fixed_target not in evaluated:
        raise RuntimeError(f"Fixed target failed for {code}")
    fixed_rolls, fixed_frame = evaluated[fixed_target]
    spot_frame = _spot_benchmark(underlying, transaction_cost_rate)
    split_index = len(fixed_frame) - test_days
    test_start = pd.Timestamp(fixed_frame["date"].iloc[split_index])
    _, fixed_train, fixed_test = _candidate_metrics(
        fixed_frame, 1.0, 1.05, test_days
    )
    _, spot_train, spot_test = _candidate_metrics(
        spot_frame, 0.0, 0.0, test_days
    )
    for row in result_rows:
        if row.get("status") != "ok":
            continue
        row["spot_test_sharpe"] = spot_test["sharpe"]
        row["spot_test_total_return_pct"] = spot_test["total_return_pct"]
        row["test_sharpe_minus_spot"] = (
            float(row["test_sharpe"]) - float(spot_test["sharpe"])
        )
        row["test_return_minus_spot_pct"] = (
            float(row["test_total_return_pct"])
            - float(spot_test["total_return_pct"])
        )
        row["beats_spot_on_both"] = (
            row["test_sharpe_minus_spot"] > 0
            and row["test_return_minus_spot_pct"] > 0
        )
    ledger, spot_ledger, reconciliation = _attribute_collar(
        underlying,
        fixed_rolls,
        daily,
        transaction_cost_rate,
        fixed_frame,
    )
    test_attribution = _test_window_attribution(
        ledger, spot_ledger, fixed_frame, spot_frame, test_start
    )
    test_monthly = _test_monthly_attribution(
        ledger, spot_ledger, fixed_frame, spot_frame, test_start
    )
    monthly = _monthly_attribution(
        ledger,
        spot_ledger,
        fixed_frame,
        spot_frame,
        reconciliation["collar_initial_capital"],
        reconciliation["spot_initial_capital"],
    )
    leave_full = _leave_best_months(
        monthly,
        fixed_frame,
        spot_frame,
        reconciliation["collar_initial_capital"],
        reconciliation["spot_initial_capital"],
    )
    leave_test = _leave_best_months(
        monthly,
        fixed_frame,
        spot_frame,
        reconciliation["collar_initial_capital"],
        reconciliation["spot_initial_capital"],
        test_start,
    )
    output_dir = output_root / code
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(result_rows).to_csv(output_dir / "plateau_results.csv", index=False)
    fixed_frame.to_csv(output_dir / "nav_fixed.csv", index=False)
    spot_frame.to_csv(output_dir / "nav_spot.csv", index=False)
    ledger.to_csv(output_dir / "daily_attribution.csv", index=False)
    monthly.to_csv(output_dir / "monthly_attribution.csv", index=False)
    pd.DataFrame([*leave_full["results"], *leave_test["results"]]).to_csv(
        output_dir / "leave_best_months.csv", index=False
    )
    monthly.sort_values("relative_pnl_pct", ascending=False).to_csv(
        output_dir / "monthly_attribution_ranked.csv", index=False
    )
    test_monthly.to_csv(output_dir / "monthly_test_attribution.csv", index=False)
    test_monthly.sort_values(
        "relative_month_return_pct", ascending=False
    ).to_csv(output_dir / "monthly_test_attribution_ranked.csv", index=False)
    (output_dir / "rolls_fixed.json").write_text(
        json.dumps(
            [
                {
                    "roll_date": item.roll_date,
                    "expiry_date": item.expiry_date,
                    "contract_month": item.contract_month,
                    "put": item.put.__dict__,
                    "call": item.call.__dict__,
                    "underlying_close": item.underlying_close,
                    "put_entry": item.put_entry,
                    "call_entry": item.call_entry,
                }
                for item in fixed_rolls
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    fixed_full = _metrics(
        fixed_frame, initial_capital=fixed_frame.attrs["initial_capital"]
    )
    spot_full = _metrics(
        spot_frame, initial_capital=spot_frame.attrs["initial_capital"]
    )
    plateau = _plateau_summary(result_rows, spot_test)
    summary = {
        "code": code,
        "period": {
            "start": start,
            "end": end,
            "trading_days": len(underlying),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_days": test_days,
        },
        "fixed_parameters": {
            "target_put": 1.0,
            "target_call": 1.05,
            "cost_rate": transaction_cost_rate,
        },
        "execution": {
            "buy": "ask when available, otherwise daily high",
            "sell": "bid when available, otherwise daily low",
            "sina_historical_bid_ask": False,
            "daily_mark": "Sina close; expiry settlement at intrinsic",
        },
        "fixed_full": fixed_full,
        "fixed_train": fixed_train,
        "fixed_test": fixed_test,
        "spot_full": spot_full,
        "spot_train": spot_train,
        "spot_test": spot_test,
        "plateau": plateau,
        "attribution": {
            **reconciliation,
            "test_window": test_attribution,
            "monthly_relative_pnl_sum_pct": float(monthly["relative_pnl_pct"].sum()),
            "relative_total_return_pct": float(
                fixed_full["total_return_pct"] - spot_full["total_return_pct"]
            ),
            "relative_reconciliation_error_pct": float(
                monthly["relative_pnl_pct"].sum()
                - (
                    fixed_full["total_return_pct"]
                    - spot_full["total_return_pct"]
                )
            ),
            "leave_best_months_full": leave_full,
            "leave_best_months_test": leave_test,
        },
        "data": {
            "sse_catalog_dates": len(catalog),
            "sina_option_contracts": len(daily),
            "successful_grid_candidates": sum(
                item.get("status") == "ok" for item in result_rows
            ),
            "failed_grid_candidates": sum(
                item.get("status") != "ok" for item in result_rows
            ),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    args = parse_args()
    output_root = Path(args.output_dir)
    summaries = [
        _run_code(
            code,
            args.end,
            args.test_days,
            args.cost_rate,
            args.workers,
            output_root,
        )
        for code in args.codes
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
