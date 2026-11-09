"""Sourced personal-position quotes and screenshot validation helpers.

Notifications are owned exclusively by the managed report-trigger pipeline.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import pytz

SHANGHAI = pytz.timezone("Asia/Shanghai")


def aware_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must include a timezone")
    return value


def instrument_identity(code: str) -> tuple[str, str, str, str]:
    """Return crawler code, currency, exchange and timezone without name matching."""
    code = str(code).strip().upper()
    if re.fullmatch(r"(?:HK)?\d{5}", code):
        return code.removeprefix("HK"), "HKD", "XHKG", "Asia/Hong_Kong"
    if re.fullmatch(r"\d{1,5}\.HK", code):
        return code[:-3].zfill(5), "HKD", "XHKG", "Asia/Hong_Kong"
    if re.fullmatch(r"(?:SH|SZ)?\d{6}(?:\.(?:SS|SH|SZ))?", code):
        numeric = re.search(r"\d{6}", code).group()
        if numeric.startswith(("2", "4", "8", "9")):
            raise ValueError("unsupported exchange or share-class currency")
        return numeric, "CNY", "XSHG", "Asia/Shanghai"
    if re.fullmatch(r"(?:US:)?[A-Z][A-Z0-9.\-]{0,9}", code):
        return code.removeprefix("US:"), "USD", "XNYS", "America/New_York"
    raise ValueError("unsupported instrument identity")


def quote_timestamp(raw: str, market_timezone: str) -> datetime:
    """Parse the provider's timestamp; the crawler's locally generated date is ignored."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("quote provider timestamp is missing")
    raw = raw.strip()
    for format_string in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            value = datetime.strptime(raw, format_string)  # noqa: DTZ007 -- provider's named market timezone is applied below
            return pytz.timezone(market_timezone).localize(value, is_dst=None)
        except ValueError:
            continue
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return aware_time(value)


@lru_cache(maxsize=12)
def _calendar(exchange: str, year: int):
    import exchange_calendars

    return exchange_calendars.get_calendar(
        exchange, start=f"{year - 1}-01-01", end=f"{year}-12-31"
    )


def exchange_state(exchange: str, now: datetime) -> dict:
    """Use exchange sessions, including holidays and DST; fail closed on missing calendars."""
    import pandas as pd

    now = aware_time(now)
    calendar = _calendar(exchange, now.year)
    instant = pd.Timestamp(now).tz_convert("UTC")
    schedule = calendar.schedule
    completed = schedule[schedule["close"] <= instant]
    if completed.empty:
        raise ValueError("latest completed exchange session unavailable")
    current = schedule[(schedule["open"] <= instant) & (schedule["close"] > instant)]
    future = schedule[schedule["open"] > instant]
    last = completed.iloc[-1]
    return {
        "market_open": not current.empty,
        "latest_completed_session": completed.index[-1].date().isoformat(),
        "session_close": last["close"].to_pydatetime(),
        "current_session_close": (
            current.iloc[-1]["close"].to_pydatetime() if not current.empty else None
        ),
        "next_open": future.iloc[0]["open"].to_pydatetime()
        if not future.empty
        else None,
    }


def _finite(value, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    return float(value)


def bound_cashflow_return(binding: dict, price: float, currency: str) -> float:
    """Solve a versioned ten-year equity CF scenario at the current equity price.

    Cash flows and share counts must use matching absolute units, or an explicit
    per-share cash flow. No total-profit/yield/required-return substitution.
    """
    if binding.get("model") != "equity_cashflow_dcf_v1":
        raise ValueError("unsupported bound return model")
    if not binding.get("version") or not binding.get("scenario"):
        raise ValueError("research version and scenario must be bound")
    assumptions = binding.get("assumptions") or {}
    if binding.get("currency") != currency:
        raise ValueError("research currency does not match quote")
    if assumptions.get("currency", currency) != currency:
        raise ValueError("cashflow currency does not match quote")
    price = _finite(price, "price")
    if price <= 0:
        raise ValueError("nonpositive price")
    if "cashflow_per_share" in assumptions:
        cashflow = _finite(assumptions["cashflow_per_share"], "cashflow per share")
        market_value = price
    else:
        cashflow = _finite(assumptions.get("cashflow"), "cashflow")
        shares = _finite(assumptions.get("shares_outstanding"), "shares outstanding")
        if shares <= 0:
            raise ValueError("nonpositive shares outstanding")
        market_value = price * shares
    growth_first = _finite(assumptions.get("growth_first"), "first growth")
    growth_second = _finite(assumptions.get("growth_second"), "second growth")
    terminal = _finite(assumptions.get("terminal_growth"), "terminal growth")
    if cashflow <= 0 or min(growth_first, growth_second, terminal) <= -1:
        raise ValueError("unsupported cashflow assumptions")

    def present_value(rate: float) -> float:
        annual, value = cashflow, 0.0
        for year in range(1, 11):
            annual *= 1 + (growth_first if year <= 5 else growth_second)
            value += annual / (1 + rate) ** year
        return value + annual * (1 + terminal) / (rate - terminal) / (1 + rate) ** 10

    low, high = terminal + 1e-9, max(1.0, terminal + 1)
    while present_value(high) > market_value and high < 100:
        high *= 2
    if present_value(high) > market_value:
        raise ValueError("implied return cannot be bracketed")
    for _ in range(100):
        middle = (low + high) / 2
        if present_value(middle) > market_value:
            low = middle
        else:
            high = middle
    result = (low + high) / 2
    if not math.isfinite(result):
        raise ValueError("nonfinite implied return")
    return result


class SourcedObservationProvider:
    """Fetch personal positions outside the global pool using the existing QQ crawler."""

    def __init__(
        self,
        config: dict,
        *,
        quote_fetcher=None,
        calendar_resolver=None,
        history_fetcher=None,
        clock=None,
    ):
        self.config = config
        if quote_fetcher is None:
            quote_fetcher = self._realtime_quote
        self.quote_fetcher = quote_fetcher
        self.calendar_resolver = calendar_resolver or exchange_state
        self.history_fetcher = history_fetcher or self._raw_history_quote
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _realtime_quote(self, code):
        # Construct only when there is actual work; service startup has no crawler side effects.
        from .data.web_crawler import StockWebCrawler

        return StockWebCrawler(self.config).fetch_realtime_quote(code)

    @staticmethod
    def _raw_history_quote(code: str, session_date: str) -> dict:
        """Read raw daily close, never a forward-adjusted k-line or adjclose field."""
        import requests

        canonical, currency, _exchange, tz = instrument_identity(code)
        if currency in {"CNY", "HKD"}:
            if currency == "HKD":
                symbol = f"hk{canonical}"
            else:
                symbol = (
                    "sh" if canonical.startswith(("5", "6", "9")) else "sz"
                ) + canonical
            response = requests.get(
                "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
                params={"param": f"{symbol},day,{session_date},{session_date},1"},
                timeout=10,
            )
            response.raise_for_status()
            rows = response.json().get("data", {}).get(symbol, {}).get("day", [])
            matches = [row for row in rows if len(row) >= 3 and row[0] == session_date]
            if len(matches) != 1:
                raise ValueError("raw historical daily close unavailable")
            return {
                "price": float(matches[0][2]),
                "currency": currency,
                "source": "qq_raw_daily",
            }
        from urllib.parse import quote

        zone = pytz.timezone(tz)
        midnight = zone.localize(
            datetime.combine(date.fromisoformat(session_date), datetime.min.time())
        )
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(canonical, safe='')}",
            params={
                "period1": int(midnight.timestamp()),
                "period2": int((midnight + timedelta(days=1)).timestamp()),
                "interval": "1d",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()["chart"]["result"][0]
        if payload.get("meta", {}).get("currency") != currency:
            raise ValueError("historical quote currency mismatch")
        closes = payload["indicators"]["quote"][0]["close"]
        selected = [
            close
            for timestamp, close in zip(payload["timestamp"], closes)
            if datetime.fromtimestamp(timestamp, zone).date().isoformat()
            == session_date
        ]
        if len(selected) != 1:
            raise ValueError("raw historical daily close unavailable")
        return {"price": selected[0], "currency": currency, "source": "yahoo_raw_daily"}

    def screenshot_quote(self, code: str, as_of) -> dict:
        """Return timestamped plausibility evidence for an image's actual data time.

        Historical intraday timestamps require minute data and fail closed here.
        Date-only and after-close screenshots can use the completed raw daily bar.
        """
        canonical, currency, exchange, tz = instrument_identity(code)
        zone = pytz.timezone(tz)
        date_only = isinstance(as_of, str) and bool(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of)
        )
        if date_only:
            selected_date = date.fromisoformat(as_of)
            snapshot = zone.localize(
                datetime.combine(selected_date, datetime.max.time())
            )
        else:
            snapshot = (
                datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                if isinstance(as_of, str)
                else as_of
            )
            snapshot = aware_time(snapshot)
        now = aware_time(self.clock())
        if snapshot > now + timedelta(minutes=1) and not date_only:
            raise ValueError("screenshot data time is in the future")
        calendar_as_of = min(snapshot, now)
        state = self.calendar_resolver(exchange, calendar_as_of)
        if not date_only and abs((now - snapshot).total_seconds()) <= 900:
            raw = self.quote_fetcher(canonical)
            quoted_at = quote_timestamp((raw or {}).get("time"), tz)
            price = _finite((raw or {}).get("close"), "quote price")
            if price <= 0 or quoted_at > now + timedelta(minutes=1):
                raise ValueError("invalid current screenshot quote")
            if state["market_open"]:
                if abs((snapshot - quoted_at).total_seconds()) > 900:
                    raise ValueError("quote cannot verify screenshot intraday time")
            elif (
                quoted_at.astimezone(zone).date().isoformat()
                != state["latest_completed_session"]
            ):
                raise ValueError("quote is not latest completed session")
            elif quoted_at < aware_time(state["session_close"]) - timedelta(minutes=15):
                raise ValueError("quote predates the completed session close")
            return {
                "price": price,
                "currency": currency,
                "provider_time": quoted_at.isoformat(),
                "source": "qq_realtime",
                "market_closed": not state["market_open"],
                "latest_trading_date": (
                    quoted_at.astimezone(zone).date().isoformat()
                    if state["market_open"]
                    else state["latest_completed_session"]
                ),
                "market_timezone": tz,
                "snapshot_date": snapshot.astimezone(zone).date().isoformat(),
                "session_date": (
                    quoted_at.astimezone(zone).date().isoformat()
                    if state["market_open"]
                    else state["latest_completed_session"]
                ),
                "session_close": (
                    state.get("current_session_close")
                    if state["market_open"]
                    else state["session_close"]
                ).isoformat(),
                "calendar_as_of": calendar_as_of.isoformat(),
                "session_verified": True,
            }
        historical_state = self.calendar_resolver(exchange, min(snapshot, now))
        if historical_state["market_open"]:
            raise ValueError(
                "historical intraday quote unavailable; provide unambiguous manual information"
            )
        session = historical_state["latest_completed_session"]
        close_time = aware_time(historical_state["session_close"])
        if date_only and date.fromisoformat(as_of) > now.astimezone(zone).date():
            raise ValueError("screenshot date is in the future")
        historical = self.history_fetcher(canonical, session)
        price = _finite(historical.get("price"), "historical price")
        if price <= 0 or historical.get("currency") != currency:
            raise ValueError("invalid historical raw quote")
        return {
            "price": price,
            "currency": currency,
            "provider_time": close_time.isoformat(),
            "source": historical["source"],
            "market_closed": True,
            "latest_trading_date": session,
            "market_timezone": tz,
            "snapshot_date": snapshot.astimezone(zone).date().isoformat(),
            "session_date": session,
            "session_close": close_time.isoformat(),
            "calendar_as_of": calendar_as_of.isoformat(),
            "session_verified": True,
        }

    def __call__(self, position: dict, binding: dict, now: datetime) -> dict:
        try:
            aware_time(now)
            code, currency, exchange, tz = instrument_identity(position["instrument"])
            if position.get("currency", currency) != currency:
                raise ValueError("position currency does not match instrument")
            quote = self.quote_fetcher(code)
            if not isinstance(quote, dict):
                raise TypeError("quote unavailable")
            quoted_at = quote_timestamp(quote.get("time"), tz)
            price = _finite(quote.get("close"), "quote price")
            if price <= 0:
                raise ValueError("nonpositive quote price")
            state = self.calendar_resolver(exchange, now)
            if not state["market_open"]:
                closing = aware_time(state["session_close"])
                if quoted_at < closing - timedelta(minutes=15):
                    raise ValueError(
                        "quote is not a completed-session closing observation"
                    )
                if state.get("next_open") is None:
                    raise ValueError("next exchange opening is unavailable")
            research = {
                key: binding.get(key) for key in ("model", "version", "scenario")
            }
            observation = {
                "ready": True,
                "metrics": {"price": price},
                "quote": {
                    "as_of": quoted_at.isoformat(),
                    "price": price,
                    "currency": currency,
                    "market_timezone": tz,
                    "source": "qq_realtime",
                    "market_open": state["market_open"],
                    "latest_completed_session": state["latest_completed_session"],
                },
                "research": research,
            }
            if not state["market_open"]:
                observation["quote"]["valid_until"] = aware_time(
                    state["next_open"]
                ).isoformat()
            try:
                value = bound_cashflow_return(binding, price, currency)
                observation["expected_return"] = value
                observation["metrics"]["expected_return"] = value
            except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
                # Valid prices remain usable by price rules even without research.
                observation["reason"] = str(exc)
            return observation
        except Exception as exc:  # noqa: BLE001 -- any source failure must leave rules not ready
            return {"ready": False, "reason": str(exc)}
