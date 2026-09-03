"""Backtest a monthly SSE ETF collar from real SSE/Sina data.

The SSE risk-indicator archive is used only as a historical contract master:
it returns the security ID, contract code, side and strike for a chosen date.
Sina then supplies the historical daily option closes for those IDs.

This is intentionally a small research runner rather than a trading engine. It
uses closing marks, configurable transaction costs/slippage, no ETF distributions, and
normalizes the portfolio to one ETF share. Those assumptions are printed with
the result so the number is not mistaken for an executable performance series.
"""

import argparse
import calendar
import itertools
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.option_data import SinaOptionDataSource


SSE_RISK_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_RISK_SQL_ID = "SSE_ZQPZ_YSP_GGQQZSXT_YSHQ_QQFXZB_DATE_L"
SSE_REFERER = "https://star.sse.com.cn/assortment/options/risk/"
SSE_ADJUSTMENT_SQL_ID = "SSE_ZQPZ_YSP_OPTZSXT_ADJUST_INFO_HYTZ_SEARCH_L"
SSE_ADJUSTMENT_DATES_BY_CODE = {
    "510300": (
        "20210118",
        "20220119",
        "20230116",
        "20240118",
        "20250618",
        "20260119",
    ),
    "510500": (
        "20240517",
        "20250116",
        "20260116",
        "20260715",
    ),
}
OPTION_LISTING_STARTS = {
    "510300": "2019-12-23",
    "510500": "2022-09-19",
}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contract:
    security_id: str
    contract_id: str
    option_type: str
    strike: float
    contract_month: str
    contract_unit: float = 10000.0


@dataclass(frozen=True)
class Roll:
    roll_date: str
    expiry_date: str
    contract_month: str
    put: Contract
    call: Contract
    underlying_close: float
    put_entry: float
    call_entry: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying-code", default="510300")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--target-put", type=float, default=0.95)
    parser.add_argument("--target-call", type=float, default=1.05)
    parser.add_argument("--cost-rate", type=float, default=0.002)
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--test-days", type=int, default=756)
    parser.add_argument("--put-grid", default="0.90,0.95,1.00")
    parser.add_argument("--call-grid", default="1.05,1.10,1.15")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--underlying-file", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    args.underlying_code = str(args.underlying_code).strip()
    if args.underlying_code not in OPTION_LISTING_STARTS:
        raise ValueError(
            "--underlying-code must be one of: "
            + ", ".join(sorted(OPTION_LISTING_STARTS))
        )
    args.start = args.start or OPTION_LISTING_STARTS[args.underlying_code]
    args.underlying_file = args.underlying_file or (
        f"cache/data/{args.underlying_code}.csv"
    )
    args.output_dir = args.output_dir or (
        f"cache/analysis/option_collar_{args.underlying_code}"
    )
    return args


def _parse_grid(value: str) -> List[float]:
    values = [float(item.strip()) for item in str(value).split(",") if item.strip()]
    if not values:
        raise ValueError("Optimization grid cannot be empty")
    return sorted(set(values))


def _month_key(value: str) -> Tuple[int, int]:
    return int(value[:2]) + 2000, int(value[2:])


def _month_distance(value: str) -> int:
    year, month = _month_key(value)
    return year * 12 + month


def _fourth_wednesday(year: int, month: int) -> pd.Timestamp:
    dates = pd.date_range(
        start=f"{year:04d}-{month:02d}-01",
        end=f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}",
        freq="W-WED",
    )
    return pd.Timestamp(dates[3]).normalize()


def _strike_from_contract_id(
    contract_id: str, underlying_code: str = "510300"
) -> Optional[float]:
    match = re.fullmatch(
        rf"{re.escape(underlying_code)}[CP]\d{{4}}[A-Z](\d{{5}})",
        contract_id,
    )
    if not match:
        return None
    return int(match.group(1)) / 1000.0


def _contract_from_row(
    row: Mapping[str, str], underlying_code: str = "510300"
) -> Optional[Contract]:
    contract_id = str(row.get("CONTRACT_ID", "")).strip()
    match = re.fullmatch(
        rf"{re.escape(underlying_code)}([CP])(\d{{4}})[A-Z](\d{{5}})",
        contract_id,
    )
    if not match:
        return None
    strike = _strike_from_contract_id(contract_id, underlying_code)
    if strike is None:
        return None
    side = "call" if match.group(1) == "C" else "put"
    return Contract(
        security_id=str(row["SECURITY_ID"]).strip(),
        contract_id=contract_id,
        option_type=side,
        strike=strike,
        contract_month=match.group(2),
        contract_unit=10000.0,
    )


def _catalog_for_date(
    date: pd.Timestamp, underlying_code: str = "510300"
) -> List[Contract]:
    response = requests.post(
        SSE_RISK_URL,
        data={
            "isPagination": "false",
            "trade_date": date.strftime("%Y%m%d"),
            "sqlId": SSE_RISK_SQL_ID,
            "contractSymbol": "",
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": SSE_REFERER},
        verify=False,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("result", [])
    contracts = []
    for row in rows:
        if not str(row.get("CONTRACT_ID", "")).startswith(underlying_code):
            continue
        contract = _contract_from_row(row, underlying_code)
        if contract is not None:
            contracts.append(contract)
    if not contracts:
        raise RuntimeError(
            "SSE historical risk archive returned no "
            f"{underlying_code} contracts for {date.date()}"
        )
    return contracts


def _adjustment_units(underlying_code: str = "510300") -> Dict[str, float]:
    """Load adjusted contract units from the SSE adjustment archive."""
    units: Dict[str, float] = {}
    for date in SSE_ADJUSTMENT_DATES_BY_CODE[underlying_code]:
        response = requests.post(
            SSE_RISK_URL,
            data={
                "isPagination": "false",
                "sqlId": SSE_ADJUSTMENT_SQL_ID,
                "adjustDate": date,
                "securityCode": underlying_code,
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": SSE_REFERER},
            verify=False,
            timeout=30,
        )
        response.raise_for_status()
        for row in response.json().get("result", []):
            contract_id = str(row.get("CONTRACT_ID", "")).strip()
            if not contract_id.startswith(underlying_code):
                continue
            unit = pd.to_numeric(row.get("CONTRACT_UNIT"), errors="coerce")
            if pd.notna(unit):
                units[contract_id] = float(unit)
    return units


def _apply_units(
    catalog: Mapping[str, Sequence[Contract]], units: Mapping[str, float]
) -> Dict[str, List[Contract]]:
    adjusted: Dict[str, List[Contract]] = {}
    for date, contracts in catalog.items():
        adjusted[date] = []
        for contract in contracts:
            unit = units.get(contract.contract_id, contract.contract_unit)
            if contract.contract_id[11:12] == "A" and contract.contract_id not in units:
                raise RuntimeError(
                    f"Missing SSE contract unit for adjusted contract "
                    f"{contract.contract_id}"
                )
            adjusted[date].append(replace(contract, contract_unit=float(unit)))
    return adjusted


def _first_trading_dates(underlying: pd.DataFrame) -> List[pd.Timestamp]:
    month = underlying["date"].dt.to_period("M")
    first_dates = underlying.groupby(month, sort=True)["date"].min()
    return [pd.Timestamp(item).normalize() for item in first_dates.tolist()]


def _load_underlying(path: Path, start: str, end: str) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["date"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame = frame[(frame["date"] >= start) & (frame["date"] <= end)]
    frame = frame.sort_values("date").drop_duplicates("date")
    if len(frame) < 20:
        raise RuntimeError("510300 underlying history is too short")
    return frame[["date", "close"]].reset_index(drop=True)


def _select_contracts(
    contracts: Sequence[Contract],
    roll_date: pd.Timestamp,
    underlying_close: float,
    target_put: float,
    target_call: float,
    underlying_code: str = "510300",
) -> Tuple[Contract, Contract]:
    current_key = roll_date.year * 12 + roll_date.month
    eligible = [
        item
        for item in contracts
        if _month_distance(item.contract_month) >= current_key
    ]
    if not eligible:
        raise RuntimeError(
            f"No unexpired {underlying_code} contract on {roll_date.date()}"
        )
    expiry_month = min(
        _month_distance(item.contract_month) for item in eligible
    )
    same_month = [
        item
        for item in eligible
        if _month_distance(item.contract_month) == expiry_month
    ]
    puts = [item for item in same_month if item.option_type == "put"]
    calls = [item for item in same_month if item.option_type == "call"]
    if not puts or not calls:
        raise RuntimeError(f"Missing call/put pair on {roll_date.date()}")
    put = min(puts, key=lambda item: abs(item.strike - underlying_close * target_put))
    call = min(
        calls, key=lambda item: abs(item.strike - underlying_close * target_call)
    )
    return put, call


def _fetch_daily(security_id: str) -> pd.DataFrame:
    source = SinaOptionDataSource()
    frame = source.fetch_etf_option_daily(security_id)
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    return frame[["date", "close"]].drop_duplicates("date").sort_values("date")


def _option_price(
    daily: pd.DataFrame,
    date: pd.Timestamp,
    expiry: pd.Timestamp,
    underlying_close: float,
    contract: Contract,
) -> float:
    if date > expiry:
        return 0.0
    if date == expiry:
        intrinsic = (
            max(underlying_close - contract.strike, 0.0)
            if contract.option_type == "call"
            else max(contract.strike - underlying_close, 0.0)
        )
        return float(intrinsic)
    available = daily[daily["date"] <= date]
    if available.empty:
        raise RuntimeError(
            f"No Sina close before {date.date()} for {contract.security_id}"
        )
    return float(available.iloc[-1]["close"])


def _catalog_dates(underlying: pd.DataFrame) -> List[pd.Timestamp]:
    return _first_trading_dates(underlying)


def _build_rolls(
    underlying: pd.DataFrame,
    catalog: Mapping[str, Sequence[Contract]],
    target_put: float,
    target_call: float,
    underlying_code: str = "510300",
) -> List[Roll]:
    rolls: List[Roll] = []
    underlying_dates = set(underlying["date"].tolist())
    last_underlying_date = max(underlying_dates)
    for date in _catalog_dates(underlying):
        row = underlying.loc[underlying["date"] == date].iloc[0]
        put, call = _select_contracts(
            catalog[date.strftime("%Y-%m-%d")],
            date,
            float(row["close"]),
            target_put,
            target_call,
            underlying_code,
        )
        expiry_year = 2000 + int(put.contract_month[:2])
        expiry_month = int(put.contract_month[2:])
        scheduled_expiry = _fourth_wednesday(expiry_year, expiry_month)
        if scheduled_expiry <= last_underlying_date:
            expiry = max(
                item for item in underlying_dates if item <= scheduled_expiry
            )
        else:
            expiry = scheduled_expiry
        rolls.append(
            Roll(
                roll_date=date.strftime("%Y-%m-%d"),
                expiry_date=expiry.strftime("%Y-%m-%d"),
                contract_month=put.contract_month,
                put=put,
                call=call,
                underlying_close=float(row["close"]),
                put_entry=np.nan,
                call_entry=np.nan,
            )
        )
    return rolls


def _with_entry_prices(
    rolls: Sequence[Roll], daily: Mapping[str, pd.DataFrame]
) -> List[Roll]:
    result: List[Roll] = []
    for roll in rolls:
        roll_date = pd.Timestamp(roll.roll_date)
        expiry = pd.Timestamp(roll.expiry_date)
        put_entry = _option_price(
            daily[roll.put.security_id],
            roll_date,
            expiry,
            roll.underlying_close,
            roll.put,
        )
        call_entry = _option_price(
            daily[roll.call.security_id],
            roll_date,
            expiry,
            roll.underlying_close,
            roll.call,
        )
        if put_entry <= 0 or call_entry <= 0:
            raise RuntimeError(
                f"Non-positive entry premium on {roll.roll_date}: "
                f"put={put_entry}, call={call_entry}"
            )
        result.append(
            replace(roll, put_entry=put_entry, call_entry=call_entry)
        )
    return result


def _active_roll_index(rolls: Sequence[Roll], date: pd.Timestamp) -> int:
    indexes = [
        i for i, roll in enumerate(rolls) if pd.Timestamp(roll.roll_date) <= date
    ]
    if not indexes:
        raise RuntimeError(f"No collar roll before {date.date()}")
    return indexes[-1]


def _simulate(
    underlying: pd.DataFrame,
    rolls: Sequence[Roll],
    daily: Mapping[str, pd.DataFrame],
    transaction_cost_rate: float = 0.0,
) -> pd.DataFrame:
    cash: Optional[float] = None
    underlying_units: Optional[float] = None
    previous_roll_index = -1
    settled_rolls = set()
    underlying_by_date = underlying.set_index("date")["close"]
    records = []
    for _, row in underlying.iterrows():
        date = pd.Timestamp(row["date"]).normalize()
        roll_index = _active_roll_index(rolls, date)
        roll = rolls[roll_index]
        for expired_index in range(roll_index + 1):
            expired = rolls[expired_index]
            expiry = pd.Timestamp(expired.expiry_date)
            if date <= expiry or expired_index in settled_rolls:
                continue
            if expiry not in underlying_by_date.index:
                raise RuntimeError(
                    f"Underlying history has no expiry close for {expiry.date()}"
                )
            expiry_close = float(underlying_by_date.loc[expiry])
            put_settlement = max(expired.put.strike - expiry_close, 0.0)
            call_settlement = max(expiry_close - expired.call.strike, 0.0)
            cash = (
                (cash or 0.0)
                + put_settlement * expired.put.contract_unit
                - call_settlement * expired.call.contract_unit
            )
            settled_rolls.add(expired_index)
        expiry = pd.Timestamp(roll.expiry_date)
        put_price = _option_price(
            daily[roll.put.security_id], date, expiry, float(row["close"]), roll.put
        )
        call_price = _option_price(
            daily[roll.call.security_id], date, expiry, float(row["close"]), roll.call
        )
        if cash is None:
            underlying_units = roll.call.contract_unit
            initial_cost = transaction_cost_rate * (
                roll.underlying_close * underlying_units
                + roll.put_entry * roll.put.contract_unit
                + roll.call_entry * roll.call.contract_unit
            )
            cash = (
                -roll.put_entry * roll.put.contract_unit
                + roll.call_entry * roll.call.contract_unit
                - initial_cost
            )
            initial_capital = (
                roll.underlying_close * underlying_units
                + roll.put_entry * roll.put.contract_unit
                - roll.call_entry * roll.call.contract_unit
                + initial_cost
            )
        elif roll_index != previous_roll_index:
            previous = rolls[previous_roll_index]
            previous_expiry = pd.Timestamp(previous.expiry_date)
            old_put = _option_price(
                daily[previous.put.security_id],
                date,
                previous_expiry,
                float(row["close"]),
                previous.put,
            )
            old_call = _option_price(
                daily[previous.call.security_id],
                date,
                previous_expiry,
                float(row["close"]),
                previous.call,
            )
            new_underlying_units = roll.call.contract_unit
            transaction_value = (
                old_put * previous.put.contract_unit
                + old_call * previous.call.contract_unit
                + roll.put_entry * roll.put.contract_unit
                + roll.call_entry * roll.call.contract_unit
                + abs((underlying_units or 0.0) - new_underlying_units)
                * float(row["close"])
            )
            cash += (
                old_put * previous.put.contract_unit
                - old_call * previous.call.contract_unit
                - roll.put_entry * roll.put.contract_unit
                + roll.call_entry * roll.call.contract_unit
                + ((underlying_units or 0.0) - new_underlying_units)
                * float(row["close"])
                - transaction_cost_rate * transaction_value
            )
            underlying_units = new_underlying_units
        nav = (
            (underlying_units or 0.0) * float(row["close"])
            + (cash or 0.0)
            + put_price * roll.put.contract_unit
            - call_price * roll.call.contract_unit
        )
        records.append(
            {
                "date": date,
                "underlying_close": float(row["close"]),
                "nav": nav,
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
                "underlying_units": underlying_units,
            }
        )
        previous_roll_index = roll_index
    result = pd.DataFrame(records)
    result["return"] = result["nav"].pct_change()
    result.attrs["initial_capital"] = initial_capital
    return result


def _metrics(
    frame: pd.DataFrame, initial_capital: Optional[float] = None
) -> Dict[str, object]:
    returns = frame["return"].dropna().to_numpy(dtype=float)
    mean = float(np.mean(returns))
    std = float(np.std(returns, ddof=1))
    sharpe = mean / std * np.sqrt(252) if len(returns) > 5 and std > 1e-10 else 0.0
    nav = frame["nav"].to_numpy(dtype=float)
    peak = np.maximum.accumulate(nav)
    max_drawdown = float(np.min(nav / peak - 1.0))
    base_nav = float(initial_capital if initial_capital is not None else nav[0])
    return {
        "start": frame["date"].iloc[0].strftime("%Y-%m-%d"),
        "end": frame["date"].iloc[-1].strftime("%Y-%m-%d"),
        "trading_days": int(len(frame)),
        "rolls": int(frame["roll_index"].nunique()),
        "total_return_pct": float((nav[-1] / base_nav - 1.0) * 100),
        "annualized_return_pct": float(
            ((nav[-1] / base_nav) ** (252.0 / len(returns)) - 1.0) * 100
        ),
        "max_drawdown_pct": max_drawdown * 100,
        "sharpe": float(sharpe),
        "daily_mean_pct": mean * 100,
        "daily_std_pct": std * 100,
    }


def _split_metrics(
    frame: pd.DataFrame, test_days: int
) -> Tuple[pd.Timestamp, Dict[str, object], Dict[str, object]]:
    if test_days <= 5 or len(frame) <= test_days + 5:
        raise RuntimeError("Test window leaves too few training observations")
    split_index = len(frame) - test_days
    split_date = pd.Timestamp(frame["date"].iloc[split_index])
    train = frame.iloc[:split_index]
    test = frame.iloc[split_index:]
    return split_date, _metrics(train), _metrics(test)


def _load_catalog(
    underlying: pd.DataFrame,
    catalog_path: Path,
    underlying_code: str = "510300",
) -> Dict[str, List[Contract]]:
    if catalog_path.exists():
        raw_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog = {
            key: [Contract(**item) for item in value]
            for key, value in raw_catalog.items()
        }
    else:
        catalog = {}
        for date in _catalog_dates(underlying):
            key = date.strftime("%Y-%m-%d")
            LOGGER.info("SSE contract catalog %s", key)
            catalog[key] = _catalog_for_date(date, underlying_code)
        catalog_path.write_text(
            json.dumps(
                {
                    key: [asdict(item) for item in value]
                    for key, value in catalog.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return _apply_units(catalog, _adjustment_units(underlying_code))


def _load_daily(
    output_dir: Path, security_ids: Sequence[str], workers: int
) -> Dict[str, pd.DataFrame]:
    daily: Dict[str, pd.DataFrame] = {}
    daily_dir = output_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    missing_ids = []
    for security_id in security_ids:
        daily_path = daily_dir / f"{security_id}.csv"
        if daily_path.exists():
            daily[security_id] = pd.read_csv(daily_path, parse_dates=["date"])
        else:
            missing_ids.append(security_id)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_fetch_daily, item): item for item in missing_ids}
        for future in as_completed(futures):
            security_id = futures[future]
            daily[security_id] = future.result()
            daily[security_id].to_csv(
                daily_dir / f"{security_id}.csv", index=False
            )
            LOGGER.info("Sina daily %s: %d rows", security_id, len(daily[security_id]))
    return daily


def _save_rolls(rolls: Sequence[Roll], path: Path) -> None:
    path.write_text(
        json.dumps([asdict(item) for item in rolls], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _candidate_metrics(
    frame: pd.DataFrame,
    target_put: float,
    target_call: float,
    test_days: int,
) -> Tuple[Dict[str, object], Dict[str, object], pd.Timestamp]:
    split_date, train, test = _split_metrics(frame, test_days)
    row: Dict[str, object] = {
        "target_put": target_put,
        "target_call": target_call,
        "train_sharpe": train["sharpe"],
        "train_total_return_pct": train["total_return_pct"],
        "train_annualized_return_pct": train["annualized_return_pct"],
        "train_max_drawdown_pct": train["max_drawdown_pct"],
        "test_sharpe": test["sharpe"],
        "test_total_return_pct": test["total_return_pct"],
        "test_annualized_return_pct": test["annualized_return_pct"],
        "test_max_drawdown_pct": test["max_drawdown_pct"],
        "status": "ok",
        "error": "",
    }
    return row, train, test


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cost_rate < 0:
        raise ValueError("--cost-rate must be non-negative")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    underlying = _load_underlying(Path(args.underlying_file), args.start, args.end)

    catalog = _load_catalog(
        underlying, output_dir / "sse_catalog.json", args.underlying_code
    )
    if args.optimize:
        target_pairs = list(
            itertools.product(_parse_grid(args.put_grid), _parse_grid(args.call_grid))
        )
    else:
        target_pairs = [(args.target_put, args.target_call)]

    rolls_by_target = {
        target: _build_rolls(
            underlying, catalog, *target, args.underlying_code
        )
        for target in target_pairs
    }
    security_ids = sorted(
        {
            item.security_id
            for rolls in rolls_by_target.values()
            for roll in rolls
            for item in (roll.put, roll.call)
        }
    )
    daily = _load_daily(output_dir, security_ids, args.workers)

    if args.optimize:
        result_rows: List[Dict[str, object]] = []
        evaluated: Dict[Tuple[float, float], Tuple[List[Roll], pd.DataFrame]] = {}
        for target, base_rolls in rolls_by_target.items():
            try:
                rolls = _with_entry_prices(base_rolls, daily)
                frame = _simulate(
                    underlying,
                    rolls,
                    daily,
                    transaction_cost_rate=args.cost_rate,
                )
                row, _, _ = _candidate_metrics(
                    frame, target[0], target[1], args.test_days
                )
                evaluated[target] = (rolls, frame)
            except Exception as exc:
                row = {
                    "target_put": target[0],
                    "target_call": target[1],
                    "status": "failed",
                    "error": str(exc),
                }
            result_rows.append(row)
        successful = [item for item in result_rows if item["status"] == "ok"]
        if not successful:
            raise RuntimeError("All optimization candidates failed")
        successful.sort(
            key=lambda item: float(item["train_sharpe"]), reverse=True
        )
        best_row = successful[0]
        best_target = (float(best_row["target_put"]), float(best_row["target_call"]))
        best_rolls, best_frame = evaluated[best_target]
        split_index = len(best_frame) - args.test_days
        summary = {
            "method": "exhaustive grid search; select maximum train Sharpe",
            "grid": {
                "put_targets": _parse_grid(args.put_grid),
                "call_targets": _parse_grid(args.call_grid),
            },
            "split": {
                "train_start": best_frame["date"].iloc[0].strftime("%Y-%m-%d"),
                "train_end": best_frame["date"].iloc[split_index - 1].strftime(
                    "%Y-%m-%d"
                ),
                "test_start": best_frame["date"].iloc[split_index].strftime(
                    "%Y-%m-%d"
                ),
                "test_end": best_frame["date"].iloc[-1].strftime("%Y-%m-%d"),
                "test_days": args.test_days,
            },
            "cost_rate": args.cost_rate,
            "best": best_row,
            "data": {
                "sse_catalog_dates": len(catalog),
                "sina_option_contracts": len(daily),
                "successful_candidates": len(successful),
                "failed_candidates": len(result_rows) - len(successful),
            },
        }
        pd.DataFrame(result_rows).to_csv(
            output_dir / "optimization_results.csv", index=False
        )
        (output_dir / "optimization_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        best_frame.to_csv(output_dir / "nav_best.csv", index=False)
        _save_rolls(best_rolls, output_dir / "rolls_best.json")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    rolls = _with_entry_prices(rolls_by_target[target_pairs[0]], daily)
    frame = _simulate(
        underlying,
        rolls,
        daily,
        transaction_cost_rate=args.cost_rate,
    )
    metrics = _metrics(frame, initial_capital=frame.attrs["initial_capital"])
    metrics["assumptions"] = {
        "target_put": args.target_put,
        "target_call": args.target_call,
        "roll_rule": (
            f"first available {args.underlying_code} trading day of each month; "
            "nearest available "
            "option expiring earliest after roll"
        ),
        "mark": "Sina daily close; intrinsic at fourth-Wednesday expiry; no bid/ask",
        "costs": "cost rate applied to ETF/option turnover at each transaction",
        "cost_rate": args.cost_rate,
        "dividends": "not included; underlying series is price-return basis",
        "contract_unit": (
            "official SSE unit for M/A contracts; ETF shares track short-call unit"
        ),
        "sse_catalog_dates": len(catalog),
        "sina_option_contracts": len(daily),
    }
    _save_rolls(rolls, output_dir / "rolls.json")
    frame.to_csv(output_dir / "nav.csv", index=False)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
