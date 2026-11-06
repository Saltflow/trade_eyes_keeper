"""One source-timed quote snapshot for reports, valuation and delivery receipts.

Daily bars remain historical inputs. They are never substituted for a failed
current quote, and a fetch clock never supplies a missing source clock.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from ..data.market_calendar import resolve_market_data_cutoff, resolve_market_session
from ..instruments.classifier import detect_market
from ..models.schemas import StockPriceData

STATUSES = ("realtime", "delayed", "last_close", "unavailable")
ZONES = {
    "a_share": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
    "sg": "Asia/Singapore",
}
PRICE_FIELDS = ("open", "close", "high", "low", "prev_close")
# Tencent's source clock is publication time: production returns A-share
# closing snapshots at 16:14 and US regular closes at 16:00:02 / 16:04:49.
# Bound that source-specific publication window; never accept arbitrary
# same-day bars or extended-hours quotes as the last regular close.
TENCENT_CLOSE_PUBLICATION_MINUTES = {"a_share": 75, "hk": 10, "us": 5}


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _nonnegative(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clock(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("source clock must include timezone")
    return value.astimezone(timezone.utc)


def assess_quote(code, quote, as_of):
    """Classify an unadjusted quote using its exchange session, not date age."""
    observed = _clock(as_of)
    result = {
        "quote_status": "unavailable",
        "quote_as_of": None,
        "quote_observed_at": observed.isoformat(),
        "quote_reason": "报价获取失败",
        "quote_missing_fields": [],
    }
    if not isinstance(quote, dict):
        return result
    identity = str(quote.get("stock_code", code)).upper().replace(".", "-")
    if identity != str(code).upper().replace(".", "-"):
        result["quote_reason"] = "报价标的不匹配"
        return result
    try:
        stamp = _clock(quote.get("quote_time"))
        result["quote_as_of"] = stamp.isoformat()
    except (ValueError, TypeError, OverflowError):
        result["quote_reason"] = "缺少带时区的报价源时间"
        return result
    if stamp > observed:
        result["quote_reason"] = "报价源时间在未来"
        return result
    prices = {key: positive(quote.get(key)) for key in PRICE_FIELDS}
    if prices["close"] is None:
        result["quote_reason"] = "价格缺失或无效"
        return result
    low, high = prices["low"], prices["high"]
    if (
        low is not None
        and high is not None
        and (
            low > high
            or not low <= prices["close"] <= high
            or (prices["open"] is not None and not low <= prices["open"] <= high)
        )
    ):
        result["quote_reason"] = "报价高低价关系无效"
        return result
    try:
        market = detect_market(code)
        local = observed.astimezone(ZoneInfo(ZONES[market]))
        session = resolve_market_session(code, as_of=observed)
        # The morning snapshot remains applicable through the lunch break.
        lunch = bool(
            session
            and session.break_start
            and session.break_end
            and session.break_start <= observed < session.break_end
        )
        if session and (session.is_open(observed) or lunch):
            reference = session.break_start if lunch else observed
            age = (reference - stamp).total_seconds()
            in_session = session.is_open(stamp) or (
                lunch and stamp == session.break_start
            )
            if not in_session or not 0 <= age <= 1200:
                result["quote_reason"] = "报价超出本交易时段或延迟超过20分钟"
                return result
            declared_delayed = quote.get(
                "timestamp_kind"
            ) == "delayed_market_snapshot" or bool(quote.get("delay_seconds"))
            status = "delayed" if lunch or age > 120 or declared_delayed else "realtime"
            reason = "午间休市，采用上午报价" if lunch else ""
        else:
            cutoff = resolve_market_data_cutoff(code, local.date(), as_of=observed)
            # A recent intraday trade is not evidence of a completed close.
            grace = (
                TENCENT_CLOSE_PUBLICATION_MINUTES.get(market, 0)
                if quote.get("source") == "tencent"
                else 10 if market == "hk" else 0
            )
            upper = cutoff.session_close + timedelta(minutes=grace)
            if not cutoff.session_close <= stamp <= upper:
                result["quote_reason"] = "缺少最近已完成交易日的收盘报价"
                return result
            status, reason = "last_close", ""
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        result["quote_reason"] = "交易日历或时效无法确认：" + type(exc).__name__
        return result
    missing = [field for field in ("open", "prev_close") if prices[field] is None]
    if missing:
        labels = {"open": "开盘", "prev_close": "前收"}
        reason = "；".join(
            filter(None, [reason, "缺少" + "、".join(labels[k] for k in missing)])
        )
    result.update(
        quote_status=status, quote_reason=reason, quote_missing_fields=missing
    )
    return result


def prepare_report_quotes(session, fetch_quote, *, clock=None):
    """Fetch once per configured symbol and freeze one assessment for all sinks.

    Returns a memoized, validated adapter for reference holdings outside the pool.
    Configured failures cannot be retried through the valuation fallback.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    codes = list(
        dict.fromkeys(
            str(item.get("code", "") if isinstance(item, dict) else item).strip()
            for item in session.config.get("stocks", [])
        )
    )
    raw, fetched = {}, {}
    for code in codes:
        try:
            raw[code] = fetch_quote(code)
        except Exception:  # noqa: BLE001 - Isolate each provider failure.
            raw[code] = None
        fetched[code] = _clock(now()).isoformat()
    observed = _clock(now())
    assessments, validated = {}, {}
    for code in codes:
        quote = raw[code]
        quality = assess_quote(code, quote, observed)
        quality["quote_fetched_at"] = fetched[code]
        assessments[code] = quality
        valid = quality["quote_status"] != "unavailable"
        validated[code] = quote if valid else None
        previous = session.stocks_data.get(code)
        row = previous.dict() if isinstance(previous, StockPriceData) else {}
        # Replace the entire raw-price tuple. No old adjusted field survives.
        row.update(
            {
                field: positive(quote.get(field)) if valid else None
                for field in PRICE_FIELDS
            }
        )
        row.update(
            stock_code=code,
            date=quality["quote_as_of"] if valid else None,
            quote_time=quality["quote_as_of"],
            data_source=None,
            adjustment_type="none",
            quote_source=(quote.get("source") or "provider_quote")
            if isinstance(quote, dict)
            else None,
            volume=_nonnegative(quote.get("volume")) if valid else None,
            amount=_nonnegative(quote.get("amount")) if valid else None,
            turnover=_nonnegative(quote.get("turnover")) if valid else None,
            change=None,
            change_pct=None,
            amplitude=None,
            dividend_yield=None,
            **quality,
        )
        if valid and quote.get("name"):
            row["stock_name"] = quote["name"]
        if valid and row["prev_close"] is not None:
            row["change"] = row["close"] - row["prev_close"]
            row["change_pct"] = row["change"] / row["prev_close"] * 100
        dividend = positive(row.get("dividend_per_share"))
        if valid and dividend is not None:
            row["dividend_yield"] = dividend / row["close"] * 100
        # Direct report assignment intentionally permits missing-price rows.
        # The historical SessionManager still validates complete OHLC bars.
        session.stocks_data[code] = StockPriceData(**row)
    counts = {
        status: sum(q["quote_status"] == status for q in assessments.values())
        for status in STATUSES
    }
    object.__setattr__(
        session,
        "report_quote_quality",
        {
            "as_of": observed.isoformat(),
            "counts": counts,
            "quotes": assessments,
        },
    )

    def valuation_quote(code):
        if code not in validated:
            try:
                value = fetch_quote(code)
                checked = assess_quote(code, value, now())
                validated[code] = (
                    value if checked["quote_status"] != "unavailable" else None
                )
            except Exception:  # noqa: BLE001 - Keep missing valuation explicit.
                validated[code] = None
        return validated[code]

    return valuation_quote
