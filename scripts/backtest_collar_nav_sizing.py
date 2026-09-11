"""Audit NAV sizing and crash-month neutralization for the ETF collar.

The original collar runner keeps one option package and approximately 10,000
ETF shares through time.  This research runner adds a continuous-sizing model:
at every monthly roll the ETF notional is reset to the current close-mark NAV,
and fractional option contracts are used so that put/call coverage remains 1x.
It also starts a separate test-window portfolio with the same initial capital
as the naked ETF benchmark, so a train-period drawdown cannot create hidden
leverage in the test slice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Mapping, Sequence

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
    _catalog_dates,
    _load_catalog,
    _load_daily,
    _load_underlying,
    _metrics,
    _option_execution_price,
    _option_price,
    _spot_benchmark,
    _underlying_execution_price,
    _with_entry_prices,
)

LISTING_STARTS = {
    "510300": "2019-12-23",
    "510500": "2022-09-19",
}
COMPONENTS = (
    "underlying_pnl",
    "put_claim_pnl",
    "call_loss_pnl",
    "net_premium_pnl",
    "transaction_cost_pnl",
)


def _underlying_path(code: str) -> Path:
    if code == "510500":
        return Path("cache/analysis/option_collar_510500/underlying_yahoo_ohlc.csv")
    return Path(f"cache/data/{code}.csv")


def _cache_path(code: str) -> Path:
    return Path(f"cache/analysis/option_collar_{code}_hl")


def _window_catalog(
    underlying: pd.DataFrame,
    catalog: Mapping[str, Sequence[Contract]],
) -> dict[str, Sequence[Contract]]:
    """Map each window roll date to the latest available SSE catalog date."""
    catalog_dates = sorted(pd.Timestamp(key).normalize() for key in catalog)
    result: dict[str, Sequence[Contract]] = {}
    for date in _catalog_dates(underlying):
        available = [item for item in catalog_dates if item <= date]
        if not available:
            raise RuntimeError(f"No SSE catalog before {date.date()}")
        result[date.strftime("%Y-%m-%d")] = catalog[available[-1].strftime("%Y-%m-%d")]
    return result


def _empty_ledger(underlying: pd.DataFrame) -> pd.DataFrame:
    ledger = pd.DataFrame({"date": underlying["date"].copy()})
    for column in COMPONENTS:
        ledger[column] = 0.0
    return ledger


def _simulate_nav_resized(
    underlying: pd.DataFrame,
    rolls: Sequence[Roll],
    daily: Mapping[str, pd.DataFrame],
    transaction_cost_rate: float,
    initial_capital: float = 1.0,
    validate: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulate a continuously resized 1x collar and its cash-flow ledger."""
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if not rolls:
        raise RuntimeError("Cannot simulate an empty roll schedule")

    ledger = _empty_ledger(underlying)
    underlying_by_date = underlying.set_index("date")["close"]
    positions: dict[int, tuple[float, float]] = {}
    settled = set()
    closed = set()
    cash = None
    underlying_units = 0.0
    previous_index = -1
    previous_close = None
    rebalance_checks: list[dict[str, float]] = []
    records = []

    for row_number, (_, row) in enumerate(underlying.iterrows()):
        date = pd.Timestamp(row["date"]).normalize()
        roll_index = _active_roll_index(rolls, date)
        roll = rolls[roll_index]
        close = float(row["close"])

        # Mark the ETF position's ordinary day-to-day price return before any
        # roll-day resizing.  The separate roll entry below then records only
        # the execution-vs-close slippage on the unit delta.
        if previous_close is not None:
            ledger.loc[row_number, "underlying_pnl"] += underlying_units * (
                close - previous_close
            )

        # Settle only positions that are still open.  A roll exit must not be
        # paid again when that contract later reaches its expiry date.
        for expired_index in range(roll_index + 1):
            expired = rolls[expired_index]
            expiry = pd.Timestamp(expired.expiry_date)
            if (
                date <= expiry
                or expired_index in settled
                or expired_index in closed
                or expired_index not in positions
            ):
                continue
            expiry_close = float(underlying_by_date.loc[expiry])
            put_qty, call_qty = positions[expired_index]
            put_settlement = max(expired.put.strike - expiry_close, 0.0)
            call_settlement = max(expiry_close - expired.call.strike, 0.0)
            cash = (cash or 0.0) + (
                put_settlement * put_qty * expired.put.contract_unit
                - call_settlement * call_qty * expired.call.contract_unit
            )
            ledger.loc[row_number, "put_claim_pnl"] += (
                put_settlement * put_qty * expired.put.contract_unit
            )
            ledger.loc[row_number, "call_loss_pnl"] -= (
                call_settlement * call_qty * expired.call.contract_unit
            )
            settled.add(expired_index)

        if cash is None:
            entry_price = _underlying_execution_price(row, "buy")
            # Strict 1x means the ETF notional is the pre-trade NAV itself,
            # not NAV less the net option premium.  The cash account absorbs
            # the execution-price slippage, premium and transaction cost.
            # It may be slightly negative: that is the financing required to
            # keep the underlying at exactly 1x while buying the put.
            underlying_units = initial_capital / close
            put_qty = underlying_units / roll.put.contract_unit
            call_qty = underlying_units / roll.call.contract_unit
            put_total = roll.put_entry * roll.put.contract_unit * put_qty
            call_total = roll.call_entry * roll.call.contract_unit * call_qty
            initial_cost = transaction_cost_rate * (
                entry_price * underlying_units + put_total + call_total
            )
            cash = (
                initial_capital
                - entry_price * underlying_units
                - put_total
                + call_total
                - initial_cost
            )
            positions[roll_index] = (put_qty, call_qty)
            ledger.loc[row_number, "underlying_pnl"] += underlying_units * (
                close - entry_price
            )
            ledger.loc[row_number, "net_premium_pnl"] += call_total - put_total
            ledger.loc[row_number, "transaction_cost_pnl"] -= initial_cost
            target_nav = initial_capital
            rebalance_checks.append(
                {
                    "date": date.value,
                    "target_nav": target_nav,
                    "target_underlying_notional": underlying_units * close,
                    "target_units": underlying_units,
                    "option_coverage_ratio": 1.0,
                }
            )
        elif roll_index != previous_index:
            previous = rolls[previous_index]
            previous_expiry = pd.Timestamp(previous.expiry_date)
            old_put_qty, old_call_qty = positions[previous_index]
            old_put_mark = _option_price(
                daily[previous.put.security_id],
                date,
                previous_expiry,
                close,
                previous.put,
            )
            old_call_mark = _option_price(
                daily[previous.call.security_id],
                date,
                previous_expiry,
                close,
                previous.call,
            )
            pretrade_nav = (
                underlying_units * close
                + (cash or 0.0)
                + old_put_mark * previous.put.contract_unit * old_put_qty
                - old_call_mark * previous.call.contract_unit * old_call_qty
            )
            if pretrade_nav <= 0:
                raise RuntimeError(
                    f"Non-positive NAV before roll on {date.date()}: {pretrade_nav}"
                )

            # Target notional is defined at the close mark.  The actual
            # rebalance trade uses H for buys and L for sells.
            target_units = pretrade_nav / close
            unit_delta = underlying_units - target_units
            if unit_delta > 1e-12:
                underlying_trade_price = _underlying_execution_price(row, "sell")
            elif unit_delta < -1e-12:
                underlying_trade_price = _underlying_execution_price(row, "buy")
            else:
                unit_delta = 0.0
                underlying_trade_price = 0.0
            new_put_qty = target_units / roll.put.contract_unit
            new_call_qty = target_units / roll.call.contract_unit
            old_put = _option_execution_price(
                daily[previous.put.security_id],
                date,
                previous_expiry,
                close,
                previous.put,
                "sell",
            )
            old_call = _option_execution_price(
                daily[previous.call.security_id],
                date,
                previous_expiry,
                close,
                previous.call,
                "buy",
            )
            old_put_total = old_put * previous.put.contract_unit * old_put_qty
            old_call_total = old_call * previous.call.contract_unit * old_call_qty
            new_put_total = roll.put_entry * roll.put.contract_unit * new_put_qty
            new_call_total = roll.call_entry * roll.call.contract_unit * new_call_qty
            turnover = (
                old_put_total
                + old_call_total
                + new_put_total
                + new_call_total
                + abs(unit_delta) * underlying_trade_price
            )
            cash += (
                old_put_total
                - old_call_total
                - new_put_total
                + new_call_total
                + unit_delta * underlying_trade_price
                - transaction_cost_rate * turnover
            )
            ledger.loc[row_number, "underlying_pnl"] += unit_delta * (
                underlying_trade_price - close
            )
            ledger.loc[row_number, "put_claim_pnl"] += old_put_total
            ledger.loc[row_number, "call_loss_pnl"] -= old_call_total
            ledger.loc[row_number, "net_premium_pnl"] += new_call_total - new_put_total
            ledger.loc[row_number, "transaction_cost_pnl"] -= (
                transaction_cost_rate * turnover
            )
            positions[roll_index] = (new_put_qty, new_call_qty)
            closed.add(previous_index)
            underlying_units = target_units
            rebalance_checks.append(
                {
                    "date": date.value,
                    "target_nav": pretrade_nav,
                    "target_underlying_notional": target_units * close,
                    "target_units": target_units,
                    "option_coverage_ratio": (
                        new_put_qty * roll.put.contract_unit / target_units
                    ),
                }
            )

        put_qty, call_qty = positions[roll_index]
        expiry = pd.Timestamp(roll.expiry_date)
        put_price = _option_price(
            daily[roll.put.security_id], date, expiry, close, roll.put
        )
        call_price = _option_price(
            daily[roll.call.security_id], date, expiry, close, roll.call
        )
        option_net_mark = (
            put_price * roll.put.contract_unit * put_qty
            - call_price * roll.call.contract_unit * call_qty
        )
        nav = underlying_units * close + (cash or 0.0) + option_net_mark
        records.append(
            {
                "date": date,
                "underlying_close": close,
                "nav": nav,
                "cash": cash,
                "option_net_mark": option_net_mark,
                "roll_index": roll_index,
                "contract_month": roll.contract_month,
                "put_security_id": roll.put.security_id,
                "call_security_id": roll.call.security_id,
                "put_strike": roll.put.strike,
                "call_strike": roll.call.strike,
                "put_price": put_price,
                "call_price": call_price,
                "put_unit": roll.put.contract_unit,
                "call_unit": roll.call.contract_unit,
                "put_quantity": put_qty,
                "call_quantity": call_qty,
                "underlying_units": underlying_units,
                "underlying_notional": underlying_units * close,
                "underlying_exposure_pct": (
                    underlying_units * close / nav * 100.0 if nav else np.nan
                ),
            }
        )
        previous_index = roll_index
        previous_close = close

    # An active option expiring exactly on the last observation is marked at
    # intrinsic on that date; later expiries are marked at the final close.
    last_row = underlying.iloc[-1]
    last_date = pd.Timestamp(last_row["date"]).normalize()
    last_index = _active_roll_index(rolls, last_date)
    if (
        last_index in positions
        and last_index not in settled
        and last_index not in closed
    ):
        last_roll = rolls[last_index]
        put_qty, call_qty = positions[last_index]
        put_mark = _option_price(
            daily[last_roll.put.security_id],
            last_date,
            pd.Timestamp(last_roll.expiry_date),
            float(last_row["close"]),
            last_roll.put,
        )
        call_mark = _option_price(
            daily[last_roll.call.security_id],
            last_date,
            pd.Timestamp(last_roll.expiry_date),
            float(last_row["close"]),
            last_roll.call,
        )
        ledger.loc[len(ledger) - 1, "put_claim_pnl"] += (
            put_mark * last_roll.put.contract_unit * put_qty
        )
        ledger.loc[len(ledger) - 1, "call_loss_pnl"] -= (
            call_mark * last_roll.call.contract_unit * call_qty
        )

    frame = pd.DataFrame(records)
    frame["return"] = frame["nav"].pct_change()
    frame.attrs["initial_capital"] = initial_capital
    frame.attrs["rebalance_checks"] = rebalance_checks
    ledger["collar_pnl"] = ledger[list(COMPONENTS)].sum(axis=1)
    expected = float(frame["nav"].iloc[-1] - initial_capital)
    actual = float(ledger["collar_pnl"].sum())
    if validate and abs(actual - expected) > max(1e-8, abs(expected) * 1e-8):
        raise RuntimeError(
            f"Resized collar attribution does not reconcile: "
            f"ledger={actual}, nav_delta={expected}"
        )
    return frame, ledger


def _spot_ledger(
    underlying: pd.DataFrame, transaction_cost_rate: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = _spot_benchmark(underlying, transaction_cost_rate)
    ledger = _empty_ledger(underlying)
    first = underlying.iloc[0]
    entry_price = _underlying_execution_price(first, "buy")
    units = 1.0 / (entry_price * (1.0 + transaction_cost_rate))
    ledger.loc[0, "underlying_pnl"] = units * (float(first["close"]) - entry_price)
    ledger.loc[0, "transaction_cost_pnl"] = -transaction_cost_rate * entry_price * units
    previous_close = float(first["close"])
    for index, (_, row) in enumerate(underlying.iloc[1:].iterrows(), start=1):
        close = float(row["close"])
        ledger.loc[index, "underlying_pnl"] = units * (close - previous_close)
        previous_close = close
    ledger["collar_pnl"] = ledger[list(COMPONENTS)].sum(axis=1)
    expected = float(frame["nav"].iloc[-1] - 1.0)
    actual = float(ledger["collar_pnl"].sum())
    if abs(actual - expected) > 1e-8:
        raise RuntimeError("Spot ledger does not reconcile")
    return frame, ledger


def _monthly_returns(frame: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    rows = []
    previous_nav = initial_capital
    selected = frame.sort_values("date").copy()
    selected["month"] = selected["date"].dt.to_period("M").astype(str)
    for month, group in selected.groupby("month", sort=True):
        end_nav = float(group["nav"].iloc[-1])
        rows.append(
            {
                "month": month,
                "month_end": group["date"].iloc[-1],
                "month_return": end_nav / previous_nav - 1.0,
                "end_nav": end_nav,
                "trading_days": len(group),
            }
        )
        previous_nav = end_nav
    return pd.DataFrame(rows)


def _monthly_attribution(
    ledger: pd.DataFrame,
    spot_ledger: pd.DataFrame,
    collar_frame: pd.DataFrame,
    spot_frame: pd.DataFrame,
    initial_capital: float,
) -> pd.DataFrame:
    collar = ledger.copy()
    collar["month"] = collar["date"].dt.to_period("M").astype(str)
    collar = collar.groupby("month", as_index=False)[
        list(COMPONENTS) + ["collar_pnl"]
    ].sum()
    spot = spot_ledger.copy()
    spot["month"] = spot["date"].dt.to_period("M").astype(str)
    spot = spot.groupby("month", as_index=False)[list(COMPONENTS)].sum()
    spot = spot.rename(columns={column: f"spot_{column}" for column in COMPONENTS})
    monthly = collar.merge(spot, on="month", how="left").fillna(0.0)
    collar_returns = _monthly_returns(collar_frame, initial_capital).rename(
        columns={"month_return": "collar_month_return"}
    )
    spot_returns = _monthly_returns(spot_frame, 1.0).rename(
        columns={"month_return": "spot_month_return"}
    )
    monthly = monthly.merge(
        collar_returns[["month", "month_end", "collar_month_return", "trading_days"]],
        on="month",
        how="left",
    )
    monthly = monthly.merge(
        spot_returns[["month", "spot_month_return"]],
        on="month",
        how="left",
    )
    monthly["collar_pnl_pct"] = monthly["collar_pnl"] / initial_capital * 100.0
    monthly["spot_pnl"] = monthly[[f"spot_{column}" for column in COMPONENTS]].sum(
        axis=1
    )
    monthly["spot_pnl_pct"] = monthly["spot_pnl"] * 100.0
    monthly["relative_pnl_pct"] = monthly["collar_pnl_pct"] - monthly["spot_pnl_pct"]
    monthly["relative_factor_return_pct"] = (
        (1.0 + monthly["collar_month_return"]) / (1.0 + monthly["spot_month_return"])
        - 1.0
    ) * 100.0
    monthly["relative_month_return_pct"] = (
        monthly["collar_month_return"] - monthly["spot_month_return"]
    ) * 100.0
    for column in COMPONENTS:
        monthly[f"{column}_pct"] = monthly[column] / initial_capital * 100.0
        monthly[f"cumulative_{column}_pct"] = monthly[f"{column}_pct"].cumsum()
    monthly["cumulative_relative_pnl_pct"] = monthly["relative_pnl_pct"].cumsum()
    monthly["cumulative_relative_factor_pct"] = (
        np.cumprod(1.0 + monthly["relative_factor_return_pct"] / 100.0) - 1.0
    ) * 100.0
    monthly["relative_rank"] = (
        monthly["relative_factor_return_pct"]
        .rank(method="first", ascending=False)
        .astype(int)
    )
    return monthly.sort_values("month").reset_index(drop=True)


def _neutralize_relative_months(monthly: pd.DataFrame) -> dict[str, object]:
    """Neutralize the best relative months while keeping all months present."""
    ranked = monthly.sort_values(
        "relative_factor_return_pct", ascending=False
    ).reset_index(drop=True)
    top_months = [str(item) for item in ranked["month"].head(3)]
    results = []
    working = monthly.copy()
    for remove_count in range(4):
        removed = top_months[:remove_count]
        adjusted = working["collar_month_return"].to_numpy(dtype=float).copy()
        spot_returns = working["spot_month_return"].to_numpy(dtype=float)
        for index, month in enumerate(working["month"]):
            if month in removed:
                adjusted[index] = spot_returns[index]
        growth = float(np.prod(1.0 + adjusted))
        spot_growth = float(
            np.prod(1.0 + working["spot_month_return"].to_numpy(dtype=float))
        )
        std = float(np.std(adjusted, ddof=1))
        results.append(
            {
                "neutralized_count": remove_count,
                "neutralized_months": removed,
                "collar_total_return_pct": (growth - 1.0) * 100.0,
                "spot_total_return_pct": (spot_growth - 1.0) * 100.0,
                "relative_compounded_return_pct": (growth / spot_growth - 1.0) * 100.0,
                "cagr_pct": (
                    (
                        growth
                        ** (252.0 / max(int(working["trading_days"].sum()) - 1, 1))
                        - 1.0
                    )
                    * 100.0
                ),
                "monthly_sharpe": (
                    float(np.mean(adjusted) / std * np.sqrt(12.0))
                    if std > 1e-12
                    else float("nan")
                ),
            }
        )
        working[f"collar_return_neutralized_{remove_count}"] = adjusted
        working[f"cumulative_neutralized_{remove_count}_pct"] = (
            np.cumprod(1.0 + adjusted) - 1.0
        ) * 100.0
    return {
        "ranking_metric": "compounded monthly collar/spot relative factor",
        "top_relative_months": [
            {
                "month": str(row["month"]),
                "relative_factor_return_pct": float(row["relative_factor_return_pct"]),
            }
            for _, row in ranked.head(3).iterrows()
        ],
        "results": results,
        "monthly_with_neutralized_paths": working.sort_values("month").reset_index(
            drop=True
        ),
    }


def _load_rolls_from_json(path: Path) -> list[Roll]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Roll(
            roll_date=item["roll_date"],
            expiry_date=item["expiry_date"],
            contract_month=item["contract_month"],
            put=Contract(**item["put"]),
            call=Contract(**item["call"]),
            underlying_close=item["underlying_close"],
            put_entry=item["put_entry"],
            call_entry=item["call_entry"],
        )
        for item in raw
    ]


def _run_code(
    code: str, end: str, test_days: int, transaction_cost_rate: float, workers: int
) -> dict[str, object]:
    cache_dir = _cache_path(code)
    full_underlying = _load_underlying(
        _underlying_path(code), LISTING_STARTS[code], end, code
    )
    catalog = _load_catalog(full_underlying, cache_dir / "sse_catalog.json", code)
    full_rolls = _build_rolls(full_underlying, catalog, 1.0, 1.05, code)
    split_index = len(full_underlying) - test_days
    if split_index <= 5:
        raise RuntimeError("Test window leaves too few training observations")
    test_start = pd.Timestamp(full_underlying["date"].iloc[split_index])
    oos_underlying = full_underlying[full_underlying["date"] >= test_start].reset_index(
        drop=True
    )
    oos_catalog = _window_catalog(oos_underlying, catalog)
    oos_rolls = _build_rolls(oos_underlying, oos_catalog, 1.0, 1.05, code)
    security_ids = sorted(
        {
            contract.security_id
            for rolls in (full_rolls, oos_rolls)
            for roll in rolls
            for contract in (roll.put, roll.call)
        }
    )
    daily = _load_daily(cache_dir, security_ids, workers)
    full_rolls = _with_entry_prices(full_rolls, daily)
    oos_rolls = _with_entry_prices(oos_rolls, daily)
    full_frame, _full_ledger = _simulate_nav_resized(
        full_underlying,
        full_rolls,
        daily,
        transaction_cost_rate,
        initial_capital=1.0,
    )
    oos_frame, oos_ledger = _simulate_nav_resized(
        oos_underlying,
        oos_rolls,
        daily,
        transaction_cost_rate,
        initial_capital=1.0,
    )
    spot_full, _ = _spot_ledger(full_underlying, transaction_cost_rate)
    spot_oos, spot_ledger = _spot_ledger(oos_underlying, transaction_cost_rate)
    monthly = _monthly_attribution(oos_ledger, spot_ledger, oos_frame, spot_oos, 1.0)
    neutralized = _neutralize_relative_months(monthly)
    neutralized_monthly = neutralized.pop("monthly_with_neutralized_paths")

    full_metrics = _metrics(full_frame, initial_capital=1.0)
    continuous_test = _metrics(
        full_frame.iloc[split_index:],
        initial_capital=float(full_frame["nav"].iloc[split_index]),
    )
    oos_metrics = _metrics(oos_frame, initial_capital=1.0)
    spot_full_metrics = _metrics(spot_full, initial_capital=1.0)
    spot_oos_metrics = _metrics(spot_oos, initial_capital=1.0)
    legacy_path = Path(f"cache/analysis/collar_robustness/{code}/nav_fixed.csv")
    legacy_audit = None
    if legacy_path.exists():
        legacy = pd.read_csv(legacy_path, parse_dates=["date"])
        row = legacy.loc[legacy["date"] == test_start]
        if not row.empty:
            item = row.iloc[0]
            legacy_notional = float(item["underlying_units"] * item["underlying_close"])
            legacy_audit = {
                "test_start_nav": float(item["nav"]),
                "underlying_units": float(item["underlying_units"]),
                "underlying_notional": legacy_notional,
                "underlying_exposure_pct_of_nav": (
                    legacy_notional / float(item["nav"]) * 100.0
                ),
            }

    output_dir = Path("cache/analysis/collar_nav_resized") / code
    output_dir.mkdir(parents=True, exist_ok=True)
    full_frame.to_csv(output_dir / "nav_resized_continuous.csv", index=False)
    oos_frame.to_csv(output_dir / "nav_resized_oos.csv", index=False)
    spot_oos.to_csv(output_dir / "nav_spot_oos.csv", index=False)
    oos_ledger.to_csv(output_dir / "daily_attribution_oos.csv", index=False)
    monthly.to_csv(output_dir / "monthly_attribution_oos.csv", index=False)
    monthly.sort_values("relative_factor_return_pct", ascending=False).to_csv(
        output_dir / "monthly_attribution_oos_ranked.csv", index=False
    )
    neutralized_monthly.to_csv(
        output_dir / "monthly_neutralized_paths_oos.csv", index=False
    )
    (output_dir / "rolls_oos.json").write_text(
        json.dumps(
            [
                {
                    "roll_date": roll.roll_date,
                    "expiry_date": roll.expiry_date,
                    "contract_month": roll.contract_month,
                    "put": roll.put.__dict__,
                    "call": roll.call.__dict__,
                    "underlying_close": roll.underlying_close,
                    "put_entry": roll.put_entry,
                    "call_entry": roll.call_entry,
                }
                for roll in oos_rolls
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    checks = full_frame.attrs["rebalance_checks"]
    max_target_error = max(
        abs(item["target_underlying_notional"] - item["target_nav"]) for item in checks
    )
    summary = {
        "code": code,
        "period": {
            "full_start": LISTING_STARTS[code],
            "end": end,
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_days": test_days,
        },
        "execution": {
            "buy": "ask when available, otherwise daily high",
            "sell": "bid when available, otherwise daily low",
            "sina_historical_bid_ask": False,
            "daily_mark": "close; expiry settlement at intrinsic",
            "transaction_cost_rate": transaction_cost_rate,
        },
        "sizing": {
            "model": "continuous fractional NAV-resized collar",
            "underlying_target": "close-mark NAV at each monthly roll",
            "option_coverage": "put and call each cover the resized ETF shares",
            "max_rebalance_target_error": max_target_error,
            "max_option_coverage_error": max(
                abs(item["option_coverage_ratio"] - 1.0) for item in checks
            ),
            "same_initial_capital_oos": 1.0,
        },
        "legacy_fixed_unit_audit": legacy_audit,
        "resized_continuous_full": full_metrics,
        "resized_continuous_test": continuous_test,
        "resized_reset_oos": oos_metrics,
        "spot_full": spot_full_metrics,
        "spot_reset_oos": spot_oos_metrics,
        "oos_relative": {
            "collar_minus_spot_return_pct_points": (
                oos_metrics["total_return_pct"] - spot_oos_metrics["total_return_pct"]
            ),
            "collar_minus_spot_sharpe": (
                oos_metrics["sharpe"] - spot_oos_metrics["sharpe"]
            ),
            "neutralized": neutralized,
        },
        "oos_attribution": {
            "component_pct_of_initial_capital": {
                column: float(oos_ledger[column].sum() * 100.0) for column in COMPONENTS
            },
            "total_return_pct": float(oos_ledger["collar_pnl"].sum() * 100.0),
            "reconciliation_error": float(
                oos_ledger["collar_pnl"].sum() - (oos_frame["nav"].iloc[-1] - 1.0)
            ),
        },
        "data": {
            "sse_catalog_dates": len(catalog),
            "sina_option_contracts": len(daily),
            "continuous_rolls": len(full_rolls),
            "oos_rolls": len(oos_rolls),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    summaries = [
        _run_code(code, "2026-09-01", 756, 0.002, 8) for code in ("510300", "510500")
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
