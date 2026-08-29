"""Point-in-time market history with explicit adjustment semantics.

The legacy :class:`DataSource` intentionally keeps returning the adjusted
series expected by technical strategies.  This module is the auditable
boundary used by fundamental features: it stores unadjusted OHLC, forward-
adjusted OHLC, the factor relating them, and dated corporate actions together.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests
from pydantic import BaseModel, Field

from src.instruments.classifier import detect_market, normalize_yahoo_symbol

logger = logging.getLogger(__name__)


def _float(value: Any) -> float | None:
    if value in (None, "", "None", "null"):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


class CorporateAction(BaseModel):
    """One dated action known no later than ``published_at``.

    ``share_multiplier`` is the post-action shares divided by pre-action
    shares.  Cash dividends do not change it.  Unknown action composition is
    retained as ``adjustment_factor_change`` instead of being guessed.
    """

    code: str
    action_type: str
    ex_date: date
    published_at: date | None = None
    record_date: date | None = None
    payable_date: date | None = None
    cash_per_share: float | None = None
    share_multiplier: float | None = None
    rights_price: float | None = None
    raw_adjustment_factor: float | None = None
    source: str
    source_url: str | None = None
    currency: str | None = None
    diagnostics: list[str] = Field(default_factory=list)


def corporate_action_issues(
    actions: Iterable[CorporateAction] | None,
) -> tuple[str, ...]:
    """Return unresolved or invalid corporate actions without guessing.

    A source-level adjustment factor is not an executable action: it does not
    tell the simulator whether shares changed, cash was distributed, or a
    rights issue occurred.  Keep that distinction explicit so source routing
    and optimizer preflight can make the same decision as the simulator.
    """
    issues: list[str] = []
    for action in actions or ():
        code = str(getattr(action, "code", ""))
        ex_date = getattr(action, "ex_date", None)
        label = f"{code} {ex_date}"
        if getattr(action, "rights_price", None) is not None:
            issues.append(f"rights issue is not supported: {label}")
            continue
        cash = getattr(action, "cash_per_share", None)
        multiplier = getattr(action, "share_multiplier", None)
        if cash is None and multiplier is None:
            issues.append(f"unresolved corporate action factor: {label}")
            continue
        if cash is not None:
            try:
                cash_value = float(cash)
            except (TypeError, ValueError):
                cash_value = float("nan")
            if not np.isfinite(cash_value) or cash_value < 0.0:
                issues.append(f"invalid cash dividend: {label}")
                continue
        if multiplier is not None:
            try:
                multiplier_value = float(multiplier)
            except (TypeError, ValueError):
                multiplier_value = float("nan")
            if not np.isfinite(multiplier_value) or multiplier_value <= 0.0:
                issues.append(f"invalid share multiplier: {label}")
    return tuple(issues)


def corporate_actions_are_explainable(
    actions: Iterable[CorporateAction] | None,
) -> bool:
    """Return whether every stored action can be applied by the simulator."""
    return not corporate_action_issues(actions)


@dataclass
class PriceHistoryBundle:
    """Aligned raw and forward-adjusted prices for one instrument.

    The canonical identity is ``qfq_price = raw_price * qfq_factor``.  The
    factor is normalized to one on the latest valid observation whenever the
    source permits it.
    """

    code: str
    prices: pd.DataFrame
    actions: list[CorporateAction] = field(default_factory=list)
    source: str = ""
    currency: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    REQUIRED_COLUMNS = (
        "date",
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_close",
        "qfq_open",
        "qfq_high",
        "qfq_low",
        "qfq_close",
        "qfq_factor",
        "volume",
        "tradable",
    )

    def validate(self) -> "PriceHistoryBundle":
        missing = [name for name in self.REQUIRED_COLUMNS if name not in self.prices]
        if missing:
            raise ValueError(f"market history missing columns: {missing}")
        frame = self.prices.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).sort_values("date")
        frame = frame.drop_duplicates("date", keep="last").reset_index(drop=True)
        for name in self.REQUIRED_COLUMNS[1:-1]:
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        frame["tradable"] = frame["tradable"].fillna(False).astype(bool)
        valid = (
            frame["raw_close"].gt(0)
            & frame["qfq_close"].gt(0)
            & frame["qfq_factor"].gt(0)
        )
        frame.loc[~valid, "tradable"] = False
        if valid.any():
            identity = (
                frame.loc[valid, "raw_close"]
                * frame.loc[valid, "qfq_factor"]
            )
            error = np.abs(identity - frame.loc[valid, "qfq_close"])
            scale = np.maximum(np.abs(frame.loc[valid, "qfq_close"]), 1e-12)
            if float((error / scale).max()) > 1e-6:
                raise ValueError("qfq_close != raw_close * qfq_factor")
        self.prices = frame
        self.actions = sorted(
            self.actions,
            key=lambda item: (item.ex_date, item.action_type, item.source),
        )
        return self


_BAOSTOCK_SOCKET_LOCK = threading.RLock()


def baostock_timeout_seconds(config: dict | None) -> float:
    """Return the bounded socket timeout for Baostock's blocking SDK."""
    root = config or {}
    audit = root.get("instrument_audit", {}) or {}
    settings = (root.get("point_in_time_data", {}) or {}).get(
        "market_history", {}
    ) or {}
    value = settings.get(
        "baostock_socket_timeout_seconds",
        audit.get("timeout_seconds", 20),
    )
    return max(1.0, float(value))


@contextmanager
def baostock_session(module: Any, timeout_seconds: float):
    """Bound the SDK's global blocking socket and always restore process state."""
    with _BAOSTOCK_SOCKET_LOCK:
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout_seconds)
        try:
            login = module.login()
            if str(getattr(login, "error_code", "0")) != "0":
                message = getattr(login, "error_msg", "unknown error")
                raise RuntimeError(f"baostock login failed: {message}")
            yield module
        finally:
            try:
                module.logout()
            except Exception as exc:
                logger.debug("Baostock logout failed after request: %s", exc)
            socket.setdefaulttimeout(previous_timeout)


def _result_rows(result: Any) -> list[dict[str, str]]:
    """Convert a Baostock result object without depending on its DataFrame API."""
    if result is None or str(getattr(result, "error_code", "0")) != "0":
        return []
    fields = list(getattr(result, "fields", []) or [])
    rows: list[dict[str, str]] = []
    while result.next():
        values = list(result.get_row_data())
        rows.append(dict(zip(fields, values)))
    return rows


def _baostock_code(code: str) -> str:
    normalized = str(code).strip()
    if re.fullmatch(r"(?:sh|sz)\.\d{6}", normalized, re.IGNORECASE):
        return normalized.lower()
    prefix = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}.{normalized}"


class BaostockMarketHistoryProvider:
    """A-share raw/qfq prices, factors and distributions from Baostock."""

    PRICE_FIELDS = "date,open,high,low,close,volume,amount,tradestatus"

    def __init__(
        self,
        module: Any | None = None,
        config: dict | None = None,
    ):
        self._module = module
        self.socket_timeout_seconds = baostock_timeout_seconds(config)

    def _load(self):
        if self._module is None:
            try:
                import baostock as bs
            except ImportError as exc:
                raise RuntimeError("baostock is required for A-share history") from exc
            self._module = bs
        return self._module

    def fetch(self, code: str, start: date, end: date) -> PriceHistoryBundle:
        bs = self._load()
        with baostock_session(bs, self.socket_timeout_seconds):
            symbol = _baostock_code(code)
            raw = self._prices(bs, symbol, start, end, adjustflag="3")
            qfq = self._prices(bs, symbol, start, end, adjustflag="2")
            if raw.empty:
                raise ValueError(f"empty unadjusted history for {code}")
            frame = self._merge_prices(raw, qfq)
            actions = self._actions(bs, code, symbol, start, end)
            return PriceHistoryBundle(
                code=str(code),
                prices=frame,
                actions=actions,
                source="baostock",
                currency="CNY",
            ).validate()

    def _prices(
        self,
        bs: Any,
        symbol: str,
        start: date,
        end: date,
        *,
        adjustflag: str,
    ) -> pd.DataFrame:
        result = bs.query_history_k_data_plus(
            symbol,
            self.PRICE_FIELDS,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag=adjustflag,
        )
        rows = _result_rows(result)
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["date"])
        for name in ("open", "high", "low", "close", "volume", "amount"):
            frame[name] = pd.to_numeric(frame.get(name), errors="coerce")
        trade_status = (
            frame["tradestatus"]
            if "tradestatus" in frame
            else pd.Series("1", index=frame.index)
        )
        frame["tradable"] = trade_status.astype(str).eq("1")
        return frame

    @staticmethod
    def _merge_prices(raw: pd.DataFrame, qfq: pd.DataFrame) -> pd.DataFrame:
        raw_columns = {
            name: f"raw_{name}" for name in ("open", "high", "low", "close")
        }
        raw_frame = raw.rename(columns=raw_columns)
        if qfq.empty:
            qfq_frame = raw.rename(
                columns={
                    name: f"qfq_{name}"
                    for name in ("open", "high", "low", "close")
                }
            )
        else:
            qfq_frame = qfq.rename(
                columns={
                    name: f"qfq_{name}"
                    for name in ("open", "high", "low", "close")
                }
            )
        keep_raw = [
            "date",
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "volume",
            "amount",
            "tradable",
        ]
        keep_qfq = [
            "date",
            "qfq_open",
            "qfq_high",
            "qfq_low",
            "qfq_close",
        ]
        frame = raw_frame[keep_raw].merge(qfq_frame[keep_qfq], on="date", how="left")
        for name in ("open", "high", "low", "close"):
            frame[f"qfq_{name}"] = frame[f"qfq_{name}"].fillna(
                frame[f"raw_{name}"]
            )
        frame["qfq_factor"] = frame["qfq_close"] / frame["raw_close"]
        return frame

    def _actions(
        self,
        bs: Any,
        code: str,
        symbol: str,
        start: date,
        end: date,
    ) -> list[CorporateAction]:
        actions: list[CorporateAction] = []
        query_factor = getattr(bs, "query_adjust_factor", None)
        if query_factor is not None:
            result = query_factor(
                code=symbol,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
            for row in _result_rows(result):
                ex_date = _date(
                    row.get("dividOperateDate")
                    or row.get("diviDate")
                    or row.get("date")
                )
                if ex_date is None:
                    continue
                factor = _float(
                    row.get("foreAdjustFactor")
                    or row.get("adjustFactor")
                )
                actions.append(
                    CorporateAction(
                        code=str(code),
                        action_type="adjustment_factor_change",
                        ex_date=ex_date,
                        raw_adjustment_factor=factor,
                        source="baostock_adjust_factor",
                        diagnostics=["action_composition_not_in_factor_feed"],
                    )
                )
        query_dividend = getattr(bs, "query_dividend_data", None)
        if query_dividend is not None:
            for year in range(start.year - 1, end.year + 1):
                try:
                    result = query_dividend(
                        code=symbol,
                        year=year,
                        yearType="operate",
                    )
                except TypeError:
                    result = query_dividend(symbol, year, "operate")
                for row in _result_rows(result):
                    ex_date = _date(
                        row.get("dividOperateDate")
                        or row.get("diviDate")
                        or row.get("exDate")
                    )
                    if ex_date is None or not start <= ex_date <= end:
                        continue
                    cash = _float(
                        row.get("dividCashPsBeforeTax")
                        or row.get("diviCashPsBeforeTax")
                    )
                    stock = _float(
                        row.get("dividStocksPs")
                        or row.get("diviStocksPs")
                    )
                    reserve = _float(
                        row.get("dividReserveToStockPs")
                        or row.get("diviReserveToStockPs")
                    )
                    share_increment = (stock or 0.0) + (reserve or 0.0)
                    share_multiplier = (
                        1.0 + share_increment if share_increment > 0 else None
                    )
                    actions.append(
                        CorporateAction(
                            code=str(code),
                            action_type=(
                                "cash_and_stock_dividend"
                                if cash and share_multiplier
                                else "stock_dividend"
                                if share_multiplier
                                else "cash_dividend"
                            ),
                            ex_date=ex_date,
                            published_at=_date(
                                row.get("dividPreNoticeDate")
                                or row.get("dividPlanAnnounceDate")
                                or row.get("dividPlanDate")
                                or row.get("dividAgmPumDate")
                                or row.get("pubDate")
                            ),
                            record_date=_date(row.get("dividRegistDate")),
                            payable_date=_date(row.get("dividPayDate")),
                            cash_per_share=cash,
                            share_multiplier=share_multiplier,
                            source="baostock_dividend",
                            currency="CNY",
                        )
                    )
        return _deduplicate_actions(actions)


class YahooMarketHistoryProvider:
    """Cross-market raw prices, adjusted close, dividends and splits."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, config: dict, http: requests.Session | None = None):
        self.http = http or requests.Session()
        audit = config.get("instrument_audit", {}) or {}
        self.timeout = int(audit.get("timeout_seconds", 20))
        self.user_agent = audit.get(
            "user_agent",
            "Mozilla/5.0 (compatible; trade-eyes-keeper point-in-time-data)",
        )

    def fetch(self, code: str, start: date, end: date) -> PriceHistoryBundle:
        symbol = normalize_yahoo_symbol(code)
        period1 = int(
            datetime.combine(
                start,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )
        period2 = int(
            datetime.combine(
                end + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )
        response = self.http.get(
            self.URL.format(symbol=symbol),
            params={
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "div,splits",
            },
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout,
        )
        response.raise_for_status()
        results = response.json().get("chart", {}).get("result", [])
        if not results:
            raise ValueError(f"empty Yahoo chart for {symbol}")
        chart = results[0]
        timestamps = chart.get("timestamp", []) or []
        indicators = chart.get("indicators", {}) or {}
        quote = (indicators.get("quote") or [{}])[0]
        adjclose = (indicators.get("adjclose") or [{}])[0].get("adjclose", [])
        records = []
        for index, timestamp in enumerate(timestamps):
            raw_close = _at(quote.get("close", []), index)
            adjusted_close = _at(adjclose, index) or raw_close
            if raw_close is None or raw_close <= 0 or adjusted_close is None:
                continue
            factor = adjusted_close / raw_close
            record = {
                "date": datetime.fromtimestamp(
                    int(timestamp), tz=timezone.utc
                ).date(),
                "raw_open": _at(quote.get("open", []), index),
                "raw_high": _at(quote.get("high", []), index),
                "raw_low": _at(quote.get("low", []), index),
                "raw_close": raw_close,
                "qfq_close": adjusted_close,
                "qfq_factor": factor,
                "volume": _at(quote.get("volume", []), index) or 0.0,
                "tradable": bool((_at(quote.get("volume", []), index) or 0) > 0),
            }
            for name in ("open", "high", "low"):
                raw_value = record[f"raw_{name}"]
                record[f"qfq_{name}"] = (
                    raw_value * factor if raw_value is not None else adjusted_close
                )
            records.append(record)
        if not records:
            raise ValueError(f"no valid Yahoo prices for {symbol}")
        currency = (chart.get("meta", {}) or {}).get("currency")
        actions = self._actions(
            code,
            chart.get("events", {}) or {},
            currency,
            response.url,
            start=start,
            end=end,
        )
        return PriceHistoryBundle(
            code=str(code),
            prices=pd.DataFrame(records),
            actions=actions,
            source="yahoo_chart",
            currency=str(currency).upper() if currency else None,
        ).validate()

    @staticmethod
    def _actions(
        code: str,
        events: dict[str, Any],
        currency: str | None,
        source_url: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[CorporateAction]:
        actions: list[CorporateAction] = []
        for event in (events.get("dividends", {}) or {}).values():
            ex_date = _event_date(event)
            amount = _float(event.get("amount"))
            if (
                ex_date
                and amount is not None
                and (start is None or ex_date >= start)
                and (end is None or ex_date <= end)
            ):
                actions.append(
                    CorporateAction(
                        code=str(code),
                        action_type="cash_dividend",
                        ex_date=ex_date,
                        cash_per_share=amount,
                        source="yahoo_chart_dividends",
                        source_url=source_url,
                        currency=currency,
                    )
                )
        for event in (events.get("splits", {}) or {}).values():
            ex_date = _event_date(event)
            numerator = _float(event.get("numerator"))
            denominator = _float(event.get("denominator"))
            ratio = None
            if numerator and denominator and denominator > 0:
                ratio = numerator / denominator
            elif event.get("splitRatio") and "/" in str(event["splitRatio"]):
                left, right = str(event["splitRatio"]).split("/", 1)
                numerator, denominator = _float(left), _float(right)
                if numerator and denominator and denominator > 0:
                    ratio = numerator / denominator
            if ex_date and ratio and ratio > 0:
                if start is not None and ex_date < start:
                    continue
                if end is not None and ex_date > end:
                    continue
                actions.append(
                    CorporateAction(
                        code=str(code),
                        action_type="split",
                        ex_date=ex_date,
                        share_multiplier=ratio,
                        source="yahoo_chart_splits",
                        source_url=source_url,
                        currency=currency,
                    )
                )
        return _deduplicate_actions(actions)


def _at(values: Iterable[Any], index: int) -> float | None:
    values = list(values or [])
    return _float(values[index]) if index < len(values) else None


def _event_date(event: dict[str, Any]) -> date | None:
    value = event.get("date")
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"\d{8}", stripped):
            try:
                return datetime.strptime(stripped, "%Y%m%d").date()
            except ValueError:
                return None
    # Yahoo returns Unix seconds as JSON numbers.  Parse those before the
    # permissive ISO parser: Python also accepts the first eight digits of a
    # timestamp as a basic YYYYMMDD date, which would silently move a real
    # action centuries into the past.
    try:
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        numeric = numeric or (
            isinstance(value, str)
            and bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()))
        )
        if numeric:
            seconds = float(value)
            if not np.isfinite(seconds):
                return None
            if abs(seconds) > 100_000_000_000:
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return _date(value)


def _deduplicate_actions(actions: Iterable[CorporateAction]) -> list[CorporateAction]:
    selected: dict[tuple[Any, ...], CorporateAction] = {}
    for action in actions:
        key = (
            action.action_type,
            action.ex_date,
            action.cash_per_share,
            action.share_multiplier,
            action.source,
        )
        selected[key] = action
    return sorted(
        selected.values(),
        key=lambda item: (item.ex_date, item.action_type, item.source),
    )


class MarketHistoryProvider:
    """Route market history by data coverage instead of HTTP success alone."""

    def __init__(
        self,
        config: dict,
        *,
        baostock_provider: BaostockMarketHistoryProvider | None = None,
        yahoo_provider: YahooMarketHistoryProvider | None = None,
    ):
        self.baostock = baostock_provider or BaostockMarketHistoryProvider(
            config=config
        )
        self.yahoo = yahoo_provider or YahooMarketHistoryProvider(config)
        settings = (config.get("point_in_time_data", {}) or {}).get(
            "market_history", {}
        ) or {}
        self.compare_fallback_on_partial = bool(
            settings.get("compare_fallback_on_partial", True)
        )
        self.prefer_explainable_actions = bool(
            settings.get("prefer_explainable_actions", True)
        )
        self.coverage_tolerance_days = max(
            0, int(settings.get("coverage_tolerance_days", 31))
        )

    @staticmethod
    def _first_date(bundle: PriceHistoryBundle) -> date:
        return pd.Timestamp(bundle.prices["date"].min()).date()

    def _covers_requested_start(
        self, bundle: PriceHistoryBundle, start: date
    ) -> bool:
        return self._first_date(bundle) <= start + timedelta(
            days=self.coverage_tolerance_days
        )

    @classmethod
    def _prefer_coverage(
        cls,
        primary: PriceHistoryBundle,
        fallback: PriceHistoryBundle,
        start: date,
    ) -> PriceHistoryBundle:
        primary_start = cls._first_date(primary)
        fallback_start = cls._first_date(fallback)
        primary_key = (primary_start, -len(primary.prices))
        fallback_key = (fallback_start, -len(fallback.prices))
        chosen, rejected = (
            (fallback, primary)
            if fallback_key < primary_key
            else (primary, fallback)
        )
        chosen.diagnostics.extend(
            [
                (
                    "coverage_comparison:"
                    f"requested_start={start.isoformat()};"
                    f"{primary.source}={primary_start.isoformat()};"
                    f"{fallback.source}={fallback_start.isoformat()}"
                ),
                (
                    "coverage_selected:"
                    f"{chosen.source};rejected={rejected.source}"
                ),
            ]
        )
        return chosen

    @classmethod
    def _prefer_explainable_actions(
        cls,
        primary: PriceHistoryBundle,
        fallback: PriceHistoryBundle,
    ) -> PriceHistoryBundle:
        """Prefer a full-coverage fallback with executable actions."""
        primary_issues = corporate_action_issues(primary.actions)
        fallback_issues = corporate_action_issues(fallback.actions)
        primary_state = "complete" if not primary_issues else "incomplete"
        fallback_state = "complete" if not fallback_issues else "incomplete"
        fallback.diagnostics.extend(
            [
                (
                    "corporate_action_comparison:"
                    f"{primary.source}={primary_state};"
                    f"{fallback.source}={fallback_state}"
                ),
                (
                    "corporate_action_selected:"
                    f"{fallback.source};rejected={primary.source}"
                ),
            ]
        )
        return fallback

    def fetch(self, code: str, start: date, end: date) -> PriceHistoryBundle:
        if detect_market(code) == "a_share":
            try:
                primary = self.baostock.fetch(code, start, end)
            except Exception as exc:
                logger.warning("Baostock history failed for %s: %s", code, exc)
                bundle = self.yahoo.fetch(code, start, end)
                bundle.diagnostics.append(f"baostock_failed:{exc}")
                return bundle
            primary_has_coverage = self._covers_requested_start(primary, start)
            primary_action_issues = corporate_action_issues(primary.actions)
            should_probe_fallback = (
                self.compare_fallback_on_partial and not primary_has_coverage
            ) or (
                self.prefer_explainable_actions and bool(primary_action_issues)
            )
            if not should_probe_fallback:
                return primary
            try:
                fallback = self.yahoo.fetch(code, start, end)
            except Exception as exc:
                if not primary_has_coverage:
                    primary.diagnostics.append(
                        "partial_coverage_fallback_failed:"
                        f"requested_start={start.isoformat()};"
                        f"actual_start={self._first_date(primary).isoformat()};"
                        f"reason={exc}"
                    )
                else:
                    primary.diagnostics.append(
                        "corporate_action_fallback_failed:"
                        f"source={primary.source};reason={exc}"
                    )
                return primary
            fallback_has_coverage = self._covers_requested_start(fallback, start)
            fallback_action_issues = corporate_action_issues(fallback.actions)
            if (
                self.prefer_explainable_actions
                and primary_action_issues
                and fallback_has_coverage
                and not fallback_action_issues
            ):
                return self._prefer_explainable_actions(primary, fallback)
            if not primary_has_coverage:
                return self._prefer_coverage(primary, fallback, start)
            if primary_action_issues and fallback_action_issues:
                primary.diagnostics.append(
                    "corporate_action_fallback_unresolved:"
                    f"source={fallback.source};issues="
                    + "|".join(fallback_action_issues)
                )
            return self._prefer_coverage(primary, fallback, start)
        return self.yahoo.fetch(code, start, end)


class PointInTimeMarketStore:
    """Filesystem store for auditable price bundles."""

    def __init__(self, root: str | Path = "data/point_in_time"):
        self.root = Path(root)
        self.market_dir = self.root / "market"

    @staticmethod
    def _safe_code(code: str) -> str:
        return str(code).replace(".", "-").replace("/", "-")

    def write(self, bundle: PriceHistoryBundle) -> dict[str, Path]:
        bundle.validate()
        self.market_dir.mkdir(parents=True, exist_ok=True)
        stem = self._safe_code(bundle.code)
        price_path = self.market_dir / f"{stem}.csv"
        action_path = self.market_dir / f"{stem}.actions.json"
        metadata_path = self.market_dir / f"{stem}.meta.json"
        price_tmp = price_path.with_suffix(".csv.tmp")
        action_tmp = action_path.with_suffix(".json.tmp")
        metadata_tmp = metadata_path.with_suffix(".json.tmp")
        bundle.prices.to_csv(price_tmp, index=False, encoding="utf-8")
        action_tmp.write_text(
            json.dumps(
                [json.loads(item.json()) for item in bundle.actions],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        metadata_tmp.write_text(
            json.dumps(
                {
                    "code": bundle.code,
                    "source": bundle.source,
                    "currency": bundle.currency,
                    "rows": len(bundle.prices),
                    "start": bundle.prices["date"].min().date().isoformat(),
                    "end": bundle.prices["date"].max().date().isoformat(),
                    "actions": len(bundle.actions),
                    "diagnostics": bundle.diagnostics,
                    "contract": "raw-and-qfq-1",
                    "updated_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        price_tmp.replace(price_path)
        action_tmp.replace(action_path)
        metadata_tmp.replace(metadata_path)
        return {
            "prices": price_path,
            "actions": action_path,
            "metadata": metadata_path,
        }

    def read(self, code: str) -> PriceHistoryBundle | None:
        stem = self._safe_code(code)
        price_path = self.market_dir / f"{stem}.csv"
        action_path = self.market_dir / f"{stem}.actions.json"
        metadata_path = self.market_dir / f"{stem}.meta.json"
        if not price_path.exists():
            return None
        prices = pd.read_csv(price_path, parse_dates=["date"])
        actions = []
        if action_path.exists():
            actions = [
                CorporateAction(**item)
                for item in json.loads(action_path.read_text(encoding="utf-8"))
            ]
        source = ""
        currency: str | None = None
        diagnostics: list[str] = []
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            source = str(metadata.get("source", ""))
            currency_value = metadata.get("currency")
            currency = str(currency_value).upper() if currency_value else None
            diagnostics = list(metadata.get("diagnostics", []) or [])
        return PriceHistoryBundle(
            code=str(code),
            prices=prices,
            actions=actions,
            source=source,
            currency=currency,
            diagnostics=diagnostics,
        ).validate()
